import asyncio
import logging
import re
import secrets
import ssl
import time
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.base import AuthProvider
from app.auth.internal import InternalAuthProvider, hash_password
from app.config import settings
from app.models.directory_settings import DirectorySettings
from app.models.user import User, UserRole
from app.schemas.directory import DirectorySettingsUpdate
from app.schemas.user import UserCreate
from app.security.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret

logger = logging.getLogger("devcloud.auth.ldap")


class DirectoryConfigurationError(ValueError):
    pass


class DirectoryConnectionError(RuntimeError):
    pass


class DirectoryUnavailableError(RuntimeError):
    """The directory could not be queried (network, TLS or service-bind failure)."""


class DirectoryLoginRejected(RuntimeError):
    """The directory recognised the user but refused the login for a known reason."""


# Active Directory reports the reason of a failed simple bind as an
# "AcceptSecurityContext error, data <code>" sub-code.
_AD_BIND_REASONS = {
    "530": "Bu saatte oturum açma izniniz yok.",
    "531": "Bu bilgisayardan oturum açma izniniz yok.",
    "532": "Kurumsal dizin parolanızın süresi dolmuş. Parolanızı yenileyip tekrar deneyin.",
    "533": "Kurumsal dizin hesabınız devre dışı.",
    "701": "Kurumsal dizin hesabınızın süresi dolmuş.",
    "773": "Kurumsal dizin parolanızı değiştirmeniz gerekiyor. Parolanızı yenileyip tekrar deneyin.",
    "775": "Kurumsal dizin hesabınız kilitlendi. Kilit açıldıktan sonra yeni parolanızla deneyin.",
}
_AD_DATA_CODE = re.compile(r"\bdata\s+([0-9a-fA-F]{3,4})\b")


def ad_bind_reason(message: str) -> tuple[str, str | None]:
    """Return the AD sub-code and user-facing reason embedded in a bind error."""
    match = _AD_DATA_CODE.search(message or "")
    if not match:
        return "", None
    code = match.group(1).lower()
    return code, _AD_BIND_REASONS.get(code)


def _safe_unbind(connection) -> None:
    try:
        connection.unbind()
    except Exception:  # noqa: BLE001 - closing a broken socket must not mask the bind error
        logger.debug("Ignoring LDAP unbind failure", exc_info=True)


@dataclass(frozen=True)
class DirectoryConfig:
    server_host: str
    server_port: int
    use_ssl: bool
    validate_tls: bool
    ca_cert_file: str
    connect_timeout_seconds: int
    bind_dn: str
    bind_password: str
    user_base_dn: str
    user_filter: str
    username_attribute: str
    email_attribute: str
    display_name_attribute: str
    team_attribute: str
    directorate_attribute: str
    group_membership_attribute: str
    required_group_dn: str
    admin_group_dn: str
    nested_group_search: bool
    organization_unit_attribute: str = ""


@dataclass(frozen=True)
class DirectoryIdentity:
    username: str
    email: str
    full_name: str
    team: str
    directorate: str
    user_dn: str
    groups: tuple[str, ...]
    is_admin: bool
    organization_unit: str = ""


def encrypt_directory_secret(secret: str) -> str:
    return encrypt_secret(secret)


def decrypt_directory_secret(token: str) -> str:
    try:
        return decrypt_secret(token)
    except SecretDecryptionError as exc:
        raise DirectoryConfigurationError(
            "Kayıtlı bind parolası çözülemedi. Parolayı yeniden kaydedin."
        ) from exc


def config_from_record(record: DirectorySettings) -> DirectoryConfig:
    return DirectoryConfig(
        server_host=record.server_host,
        server_port=record.server_port,
        use_ssl=record.use_ssl,
        validate_tls=record.validate_tls,
        ca_cert_file=record.ca_cert_file,
        connect_timeout_seconds=record.connect_timeout_seconds,
        bind_dn=record.bind_dn,
        bind_password=decrypt_directory_secret(record.encrypted_bind_password),
        user_base_dn=record.user_base_dn,
        user_filter=record.user_filter,
        username_attribute=record.username_attribute,
        email_attribute=record.email_attribute,
        display_name_attribute=record.display_name_attribute,
        team_attribute=record.team_attribute,
        directorate_attribute=record.directorate_attribute,
        organization_unit_attribute=record.organization_unit_attribute,
        group_membership_attribute=record.group_membership_attribute,
        required_group_dn=record.required_group_dn,
        admin_group_dn=record.admin_group_dn,
        nested_group_search=record.nested_group_search,
    )


def config_from_update(
    update: DirectorySettingsUpdate,
    saved_record: DirectorySettings | None = None,
) -> DirectoryConfig:
    bind_password = update.bind_password or ""
    if not bind_password and saved_record:
        bind_password = decrypt_directory_secret(saved_record.encrypted_bind_password)
    return DirectoryConfig(
        server_host=update.server_host,
        server_port=update.server_port,
        use_ssl=update.use_ssl,
        validate_tls=update.validate_tls,
        ca_cert_file=update.ca_cert_file,
        connect_timeout_seconds=update.connect_timeout_seconds,
        bind_dn=update.bind_dn,
        bind_password=bind_password,
        user_base_dn=update.user_base_dn,
        user_filter=update.user_filter,
        username_attribute=update.username_attribute,
        email_attribute=update.email_attribute,
        display_name_attribute=update.display_name_attribute,
        team_attribute=update.team_attribute,
        directorate_attribute=update.directorate_attribute,
        organization_unit_attribute=update.organization_unit_attribute,
        group_membership_attribute=update.group_membership_attribute,
        required_group_dn=update.required_group_dn,
        admin_group_dn=update.admin_group_dn,
        nested_group_search=update.nested_group_search,
    )


def validate_directory_config(config: DirectoryConfig) -> None:
    missing = []
    if not config.server_host:
        missing.append("sunucu")
    if not config.bind_dn:
        missing.append("bind DN")
    if not config.bind_password:
        missing.append("bind parolası")
    if not config.user_base_dn:
        missing.append("kullanıcı base DN")
    if missing:
        raise DirectoryConfigurationError(
            "Eksik LDAP ayarı: " + ", ".join(missing) + "."
        )


def _ldap3():
    try:
        import ldap3
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars
    except ImportError as exc:
        raise DirectoryConnectionError(
            "LDAP desteği yüklü değil; ldap3 paketini kurun."
        ) from exc
    return ldap3, LDAPException, escape_filter_chars


def _server_for(config: DirectoryConfig):
    ldap3, _, _ = _ldap3()
    tls = ldap3.Tls(
        validate=ssl.CERT_REQUIRED if config.validate_tls else ssl.CERT_NONE,
        ca_certs_file=config.ca_cert_file or None,
    )
    return ldap3.Server(
        config.server_host,
        port=config.server_port,
        use_ssl=config.use_ssl,
        tls=tls,
        connect_timeout=config.connect_timeout_seconds,
        get_info=ldap3.NONE,
    )


def _bound_connection(config: DirectoryConfig, user: str, password: str):
    ldap3, LDAPException, _ = _ldap3()
    try:
        connection = ldap3.Connection(
            _server_for(config),
            user=user,
            password=password,
            receive_timeout=config.connect_timeout_seconds,
            raise_exceptions=True,
        )
    except (LDAPException, OSError, ValueError) as exc:
        raise DirectoryConnectionError(str(exc)) from exc
    try:
        connection.open()
        connection.bind()
        return connection
    except (LDAPException, OSError) as exc:
        _safe_unbind(connection)
        raise DirectoryConnectionError(str(exc)) from exc


def test_directory_configuration(config: DirectoryConfig) -> tuple[str, int]:
    """Bind and perform a minimal base search without exposing credentials."""
    validate_directory_config(config)
    ldap3, LDAPException, _ = _ldap3()
    started = time.monotonic()
    connection = _bound_connection(config, config.bind_dn, config.bind_password)
    try:
        connection.search(
            search_base=config.user_base_dn,
            search_filter="(objectClass=*)",
            search_scope=ldap3.SUBTREE,
            attributes=[config.username_attribute],
            size_limit=1,
        )
    except LDAPException as exc:
        raise DirectoryConnectionError(
            f"Bind başarılı ancak kullanıcı tabanı aranamadı: {exc}"
        ) from exc
    finally:
        connection.unbind()
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return "Bind ve kullanıcı tabanı araması başarılı.", elapsed_ms


def _entry_values(entry, attribute: str) -> list[str]:
    if not attribute or attribute not in entry.entry_attributes:
        return []
    values = entry[attribute].values
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        return [str(values)]
    return [str(value) for value in values]


def _entry_value(entry, attribute: str) -> str:
    values = _entry_values(entry, attribute)
    return values[0].strip() if values else ""


def _dn_equal(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def _is_group_member(
    connection,
    config: DirectoryConfig,
    user_dn: str,
    direct_groups: list[str],
    target_group_dn: str,
) -> bool:
    if not target_group_dn:
        return False
    if any(_dn_equal(group, target_group_dn) for group in direct_groups):
        return True
    if not config.nested_group_search:
        return False

    ldap3, LDAPException, escape_filter_chars = _ldap3()
    try:
        # Active Directory's matching-rule-in-chain supports transitive membership.
        connection.search(
            search_base=target_group_dn,
            search_filter=(
                "(member:1.2.840.113556.1.4.1941:="
                f"{escape_filter_chars(user_dn)})"
            ),
            search_scope=ldap3.BASE,
            attributes=["distinguishedName"],
            size_limit=1,
        )
        return bool(connection.entries)
    except LDAPException:
        logger.info("Nested LDAP group lookup failed for %s", target_group_dn)
        return False


def authenticate_directory_user(
    config: DirectoryConfig, username: str, password: str
) -> DirectoryIdentity | None:
    validate_directory_config(config)
    if not username.strip() or not password:
        return None

    ldap3, LDAPException, escape_filter_chars = _ldap3()
    try:
        service_connection = _bound_connection(
            config, config.bind_dn, config.bind_password
        )
    except DirectoryConnectionError as exc:
        code, _ = ad_bind_reason(str(exc))
        logger.error(
            "LDAP service bind as %s failed (AD code %s): %s. If this is a "
            "personal account whose password changed, update the bind password "
            "in Admin > Directory; every login retries the stale password and "
            "can lock the account.",
            config.bind_dn,
            code or "-",
            exc,
        )
        raise DirectoryUnavailableError(str(exc)) from exc
    try:
        search_filter = config.user_filter.replace(
            "{username}", escape_filter_chars(username.strip())
        )
        attributes = list(
            dict.fromkeys(
                attribute
                for attribute in [
                    config.username_attribute,
                    config.email_attribute,
                    config.display_name_attribute,
                    config.team_attribute,
                    config.directorate_attribute,
                    config.organization_unit_attribute,
                    config.group_membership_attribute,
                ]
                if attribute
            )
        )
        service_connection.search(
            search_base=config.user_base_dn,
            search_filter=search_filter,
            search_scope=ldap3.SUBTREE,
            attributes=attributes,
            size_limit=2,
        )
        if len(service_connection.entries) != 1:
            return None

        entry = service_connection.entries[0]
        user_dn = entry.entry_dn
        directory_username = _entry_value(entry, config.username_attribute) or username.strip()
        email = _entry_value(entry, config.email_attribute)
        full_name = _entry_value(entry, config.display_name_attribute) or directory_username
        team = _entry_value(entry, config.team_attribute)
        directorate = _entry_value(entry, config.directorate_attribute)
        organization_unit = _entry_value(entry, config.organization_unit_attribute)
        groups = _entry_values(entry, config.group_membership_attribute)

        if config.required_group_dn and not _is_group_member(
            service_connection,
            config,
            user_dn,
            groups,
            config.required_group_dn,
        ):
            return None

        is_admin = _is_group_member(
            service_connection,
            config,
            user_dn,
            groups,
            config.admin_group_dn,
        )
    except (LDAPException, OSError) as exc:
        logger.error("LDAP user lookup failed: %s", exc)
        raise DirectoryUnavailableError(str(exc)) from exc
    finally:
        _safe_unbind(service_connection)

    try:
        user_connection = _bound_connection(config, user_dn, password)
    except DirectoryConnectionError as exc:
        code, reason = ad_bind_reason(str(exc))
        logger.info(
            "LDAP bind rejected for %s (AD code %s)", user_dn, code or "-"
        )
        if reason:
            raise DirectoryLoginRejected(reason) from exc
        return None
    _safe_unbind(user_connection)

    return DirectoryIdentity(
        username=directory_username,
        email=email,
        full_name=full_name,
        team=team,
        directorate=directorate,
        organization_unit=organization_unit,
        user_dn=user_dn,
        groups=tuple(groups),
        is_admin=is_admin,
    )


def _fit_identity_to_columns(identity: DirectoryIdentity) -> DirectoryIdentity:
    """Clip directory attributes to the user columns; PostgreSQL rejects overflow."""
    columns = User.__table__.columns

    def clip(value: str, column: str) -> str:
        return (value or "")[: columns[column].type.length]

    return replace(
        identity,
        username=clip(identity.username, "username"),
        email=clip(identity.email, "email"),
        full_name=clip(identity.full_name, "full_name"),
        team=clip(identity.team, "team"),
        directorate=clip(identity.directorate, "directorate"),
        organization_unit=clip(identity.organization_unit, "organization_unit"),
    )


class HybridAuthProvider(AuthProvider):
    """Internal auth fallback plus runtime-configured LDAP authentication."""

    def __init__(self) -> None:
        self.internal = InternalAuthProvider()

    @property
    def provider_name(self) -> str:
        return "hybrid"

    async def authenticate(
        self, username: str, password: str, db: AsyncSession
    ) -> User | None:
        local_user = await self.internal.authenticate(username, password, db)
        if local_user:
            return local_user

        record = await db.get(DirectorySettings, 1)
        if not record or not record.enabled:
            return None

        try:
            config = config_from_record(record)
            identity = await asyncio.to_thread(
                authenticate_directory_user, config, username, password
            )
        except DirectoryLoginRejected:
            raise
        except (
            DirectoryConfigurationError,
            DirectoryConnectionError,
            DirectoryUnavailableError,
        ) as exc:
            logger.error("LDAP authentication unavailable: %s", exc)
            raise DirectoryUnavailableError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - never turn a login into a bare 500
            logger.exception("Unexpected LDAP authentication failure")
            raise DirectoryUnavailableError(str(exc)) from exc
        if not identity:
            return None
        identity = _fit_identity_to_columns(identity)

        existing = (
            await db.execute(
                select(User).where(User.username == identity.username)
            )
        ).scalar_one_or_none()
        if existing and existing.auth_source != "active_directory":
            logger.warning(
                "LDAP login rejected because username %s belongs to a local account",
                identity.username,
            )
            return None

        role = UserRole.ADMIN if identity.is_admin else UserRole.USER
        if existing:
            existing.full_name = identity.full_name
            directory_email = identity.email.strip().lower()
            if directory_email and directory_email != existing.email:
                email_owner = (
                    await db.execute(
                        select(User).where(
                            User.email == directory_email,
                            User.id != existing.id,
                        )
                    )
                ).scalar_one_or_none()
                if not email_owner:
                    existing.email = directory_email
            existing.team = identity.team
            existing.directorate = identity.directorate
            existing.organization_unit = identity.organization_unit
            existing.role = role
            existing.is_active = True
            user = existing
        else:
            email = identity.email.strip().lower()
            if not email:
                email = f"{identity.username.lower()}@directory.local"
            email_owner = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if email_owner:
                email = f"{identity.username.lower()}@directory.local"
            user = User(
                username=identity.username,
                email=email,
                hashed_password=hash_password(secrets.token_urlsafe(48)),
                full_name=identity.full_name,
                team=identity.team,
                directorate=identity.directorate,
                organization_unit=identity.organization_unit,
                role=role,
                auth_source="active_directory",
                is_active=True,
            )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    async def create_user(
        self, user_in: UserCreate, db: AsyncSession, is_admin: bool = False
    ) -> User:
        return await self.internal.create_user(user_in, db, is_admin=is_admin)

    async def change_password(
        self, user: User, new_password: str, db: AsyncSession
    ) -> bool:
        if user.auth_source != "internal":
            return False
        return await self.internal.change_password(user, new_password, db)
