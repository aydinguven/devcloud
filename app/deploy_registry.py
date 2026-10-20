"""Deploy registry (Nexus) that Kubernetes image releases are copied to.

Unlike the Model Container Registry, which is the intermediate store for every
model build, this registry is only written by the controller and never cleaned
up: the platform team re-tags and deploys what lands here.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deploy_registry_settings import DeployRegistrySettings
from app.schemas.kubernetes import (
    DeployRegistrySettingsOut,
    DeployRegistrySettingsUpdate,
    normalize_deploy_registry_host,
)
from app.security.secrets import SecretDecryptionError, decrypt_secret

MANIFEST_FORMATS = {"": "Kaynağı koru", "v2s2": "Docker v2 schema 2"}


class DeployRegistryConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class DeployRegistryConfig:
    managed: bool
    enabled: bool
    registry_url: str
    username: str
    password: str
    tls_verify: bool = True
    manifest_format: str = ""


def validate_deploy_registry_config(
    config: DeployRegistryConfig,
    *,
    require_enabled: bool = False,
) -> None:
    if require_enabled and not config.enabled:
        raise DeployRegistryConfigurationError(
            "Dağıtım registry'si (Nexus) yapılandırılmamış veya etkin değil."
        )
    if config.enabled and not config.registry_url:
        raise DeployRegistryConfigurationError(
            "Dağıtım registry'si etkinleştirildiğinde adres zorunludur."
        )
    if config.registry_url:
        try:
            normalized = normalize_deploy_registry_host(config.registry_url)
        except ValueError as exc:
            raise DeployRegistryConfigurationError(str(exc)) from exc
        if normalized != config.registry_url:
            raise DeployRegistryConfigurationError("Registry adresi host:port olmalıdır.")
    if bool(config.username) != bool(config.password):
        raise DeployRegistryConfigurationError(
            "Registry kullanıcı adı ve parolası birlikte girilmelidir."
        )
    if config.manifest_format not in MANIFEST_FORMATS:
        raise DeployRegistryConfigurationError("Desteklenmeyen manifest biçimi.")
    for value in (config.username, config.password):
        if any(ord(character) < 32 for character in value):
            raise DeployRegistryConfigurationError(
                "Registry ayarları kontrol karakteri içeremez."
            )


def config_from_record(record: DeployRegistrySettings) -> DeployRegistryConfig:
    try:
        password = decrypt_secret(record.encrypted_password)
    except SecretDecryptionError as exc:
        raise DeployRegistryConfigurationError(
            "Kayıtlı dağıtım registry parolası çözülemedi; yeniden kaydedin."
        ) from exc
    config = DeployRegistryConfig(
        managed=True,
        enabled=record.enabled,
        registry_url=record.registry_url.strip(),
        username=record.username.strip(),
        password=password,
        tls_verify=record.tls_verify,
        manifest_format=record.manifest_format or "",
    )
    validate_deploy_registry_config(config)
    return config


async def effective_deploy_registry_config(db: AsyncSession) -> DeployRegistryConfig:
    record = await db.get(DeployRegistrySettings, 1)
    if record is None:
        return DeployRegistryConfig(
            managed=False, enabled=False, registry_url="", username="", password=""
        )
    return config_from_record(record)


def config_from_update(
    update: DeployRegistrySettingsUpdate,
    current: DeployRegistryConfig,
) -> DeployRegistryConfig:
    config = DeployRegistryConfig(
        managed=True,
        enabled=update.enabled,
        registry_url=update.registry_url,
        username=update.username,
        password=current.password if update.password is None else update.password,
        tls_verify=update.tls_verify,
        manifest_format=update.manifest_format,
    )
    validate_deploy_registry_config(config)
    return config


def deploy_registry_settings_out(
    config: DeployRegistryConfig,
    record: DeployRegistrySettings | None,
) -> DeployRegistrySettingsOut:
    return DeployRegistrySettingsOut(
        managed=record is not None,
        enabled=config.enabled,
        registry_url=config.registry_url,
        username=config.username,
        has_password=bool(config.password),
        tls_verify=config.tls_verify,
        manifest_format=config.manifest_format,
        updated_at=record.updated_at if record else None,
    )


_CHALLENGE_PARAMETER = re.compile(r'(\w+)="([^"]*)"')


def _bearer_challenge(response: httpx.Response) -> dict[str, str] | None:
    header = response.headers.get("www-authenticate", "")
    scheme, _, parameters = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    values = dict(_CHALLENGE_PARAMETER.findall(parameters))
    realm = values.get("realm", "")
    return values if realm.startswith(("https://", "http://")) else None


async def _probe(
    client: httpx.AsyncClient,
    base_url: str,
    auth: tuple[str, str] | None,
) -> tuple[bool, int, str]:
    response = await client.get(f"{base_url}/v2/", auth=auth)
    if response.status_code == 200:
        return True, 200, "Registry v2 API erişimi ve kimlik doğrulama başarılı."
    challenge = _bearer_challenge(response) if response.status_code == 401 else None
    if challenge is None:
        return False, response.status_code, f"Registry v2 API HTTP {response.status_code} döndürdü."
    # Docker Bearer Token Realm (Nexus): the credentials are checked at the
    # token endpoint the registry names in its challenge.
    params = {"service": challenge["service"]} if challenge.get("service") else None
    token = await client.get(challenge["realm"], params=params, auth=auth)
    if token.status_code == 200:
        return True, 200, "Registry token kimlik doğrulaması başarılı."
    return False, token.status_code, f"Registry token isteği HTTP {token.status_code} döndürdü."


async def test_deploy_registry(config: DeployRegistryConfig) -> dict[str, object]:
    """Check v2 access and the credentials from the controller.

    Push rights are only proven by a real release.
    """
    validate_deploy_registry_config(config)
    if not config.registry_url:
        raise DeployRegistryConfigurationError("Registry bağlantı testi için adres zorunludur.")
    auth = (config.username, config.password) if config.username else None
    # skopeo falls back to plain HTTP when TLS verification is off; mirror that.
    schemes = ["https"] if config.tls_verify else ["https", "http"]
    started = time.monotonic()
    last_error = ""
    async with httpx.AsyncClient(
        timeout=10.0, follow_redirects=False, verify=config.tls_verify
    ) as client:
        for scheme in schemes:
            try:
                ok, status_code, message = await _probe(
                    client, f"{scheme}://{config.registry_url}", auth
                )
            except httpx.HTTPError as exc:
                last_error = f"{scheme.upper()}: {exc}"
                continue
            if scheme == "http":
                message += " (TLS yok, düz HTTP)"
            return {
                "ok": ok,
                "status_code": status_code,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "message": message,
            }
    return {
        "ok": False,
        "status_code": None,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "message": f"Registry bağlantısı kurulamadı: {last_error}",
    }
