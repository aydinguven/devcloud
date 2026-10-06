"""Self-service LiteLLM users and API keys for devcloud users."""

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.litellm import (
    LiteLLMClient,
    LiteLLMConfigurationError,
    LiteLLMConnectionError,
    config_from_record,
)
from app.models.genai_account import GenAiAccount
from app.models.genai_settings import GenAiSettings
from app.models.user import User
from app.schemas.genai import (
    GenAiAccountStatus,
    GenAiDailyUsage,
    GenAiIssuedKey,
    GenAiUsage,
    GenAiUsageHistory,
)

logger = logging.getLogger("devcloud.genai")

_USER_ID = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,127}$")
_locks: dict[int, asyncio.Lock] = {}


class GenAiUnavailable(RuntimeError):
    """GenAI is disabled or not fully configured by an administrator."""


class GenAiConflict(RuntimeError):
    pass


def litellm_user_id(user: User) -> str:
    value = (user.username or "").strip().lower()
    if not _USER_ID.fullmatch(value):
        raise GenAiConflict(
            "Kullanıcı adınız LiteLLM kullanıcı kimliği olarak kullanılamıyor."
        )
    return value


def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = _locks[user_id] = asyncio.Lock()
    return lock


async def _settings(db: AsyncSession) -> GenAiSettings | None:
    return await db.get(GenAiSettings, 1)


def is_configured(record: GenAiSettings | None) -> bool:
    return bool(
        record and record.enabled and record.base_url and record.encrypted_admin_key
    )


async def _client(db: AsyncSession) -> tuple[LiteLLMClient, GenAiSettings]:
    record = await _settings(db)
    if not is_configured(record):
        raise GenAiUnavailable("GenAI erişimi henüz yönetici tarafından yapılandırılmadı.")
    try:
        return LiteLLMClient(config_from_record(record)), record
    except LiteLLMConfigurationError as exc:
        raise GenAiUnavailable(str(exc)) from exc


async def _account(db: AsyncSession, user: User) -> GenAiAccount | None:
    return (
        await db.execute(select(GenAiAccount).where(GenAiAccount.user_id == user.id))
    ).scalar_one_or_none()


def _key_alias(user_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"devcloud-{user_id}-{stamp}"


def _token_of(payload: dict) -> str:
    return str(payload.get("token_id") or payload.get("token") or "")


def _usage(info: dict) -> GenAiUsage:
    def number(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return GenAiUsage(
        spend=number(info.get("spend")) or 0.0,
        max_budget=number(info.get("max_budget")),
        budget_duration=info.get("budget_duration") or None,
        budget_reset_at=str(info["budget_reset_at"]) if info.get("budget_reset_at") else None,
    )


def _key_active(payload: dict, token: str) -> bool | None:
    keys = payload.get("keys")
    if not token or not isinstance(keys, list):
        return None
    for entry in keys:
        if isinstance(entry, dict) and token in {
            str(entry.get("token") or ""),
            str(entry.get("token_id") or ""),
        }:
            return True
    return False


async def account_status(db: AsyncSession, user: User) -> GenAiAccountStatus:
    record = await _settings(db)
    if not is_configured(record):
        return GenAiAccountStatus(configured=False)
    account = await _account(db, user)
    status = GenAiAccountStatus(
        configured=True,
        base_url=record.base_url,
        provisioned=bool(account and account.personal_key_alias),
        key_alias=account.personal_key_alias if account else "",
        created_at=account.created_at if account else None,
        rotated_at=account.rotated_at if account else None,
    )
    try:
        status.litellm_user_id = (
            account.litellm_user_id if account else litellm_user_id(user)
        )
        client, _record = await _client(db)
        payload = await client.get_user(status.litellm_user_id)
    except (GenAiUnavailable, GenAiConflict, LiteLLMConnectionError) as exc:
        status.error = str(exc)
        return status
    status.litellm_user_exists = payload is not None
    if payload is not None:
        status.usage = _usage(payload.get("user_info") or {})
        if account:
            status.key_active = _key_active(payload, account.personal_key_token)
    return status


async def _ensure_litellm_user(
    client: LiteLLMClient, user_id: str, email: str | None
) -> None:
    if await client.get_user(user_id) is not None:
        return
    try:
        await client.create_user(user_id, email)
    except LiteLLMConnectionError as exc:
        # A concurrent or manual creation is fine; adopt the existing user.
        if await client.get_user(user_id) is not None:
            return
        if email and "email" in str(exc).lower():
            # The e-mail belongs to another LiteLLM user (e.g. a UI login).
            await client.create_user(user_id, None)
            return
        raise


async def provision(db: AsyncSession, user: User) -> GenAiIssuedKey:
    """Create (or adopt) the LiteLLM user and issue a personal key once."""
    async with _lock_for(user.id):
        client, record = await _client(db)
        account = await _account(db, user)
        if account and account.personal_key_alias:
            raise GenAiConflict(
                "GenAI erişiminiz zaten etkin. Yeni anahtar için anahtarı yenileyin."
            )
        user_id = account.litellm_user_id if account else litellm_user_id(user)
        await _ensure_litellm_user(client, user_id, user.email)
        alias = _key_alias(user_id)
        issued = await client.generate_key(user_id, alias, "personal")
        token = _token_of(issued)
        if account is None:
            account = GenAiAccount(user_id=user.id, litellm_user_id=user_id)
        account.personal_key_alias = str(issued.get("key_alias") or alias)
        account.personal_key_token = token
        db.add(account)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            await _discard_key(client, token or issued["key"])
            raise GenAiConflict("GenAI erişiminiz başka bir istekte oluşturuldu.") from exc
        except Exception:
            await db.rollback()
            await _discard_key(client, token or issued["key"])
            raise
        logger.info("Issued LiteLLM personal key %s for %s", account.personal_key_alias, user_id)
        return GenAiIssuedKey(
            api_key=issued["key"],
            key_alias=account.personal_key_alias,
            base_url=record.base_url,
            litellm_user_id=user_id,
        )


async def rotate(db: AsyncSession, user: User) -> GenAiIssuedKey:
    """Issue a new personal key, then delete the previous one."""
    async with _lock_for(user.id):
        client, record = await _client(db)
        account = await _account(db, user)
        if account is None:
            raise GenAiConflict("Önce GenAI erişimi oluşturun.")
        user_id = account.litellm_user_id
        # The LiteLLM user may have been removed manually since provisioning.
        await _ensure_litellm_user(client, user_id, user.email)
        previous_token = account.personal_key_token
        previous_alias = account.personal_key_alias
        alias = _key_alias(user_id)
        issued = await client.generate_key(user_id, alias, "personal")
        token = _token_of(issued)
        account.personal_key_alias = str(issued.get("key_alias") or alias)
        account.personal_key_token = token
        account.rotated_at = datetime.now(timezone.utc)
        db.add(account)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            await _discard_key(client, token or issued["key"])
            raise
        warning = None
        if not await _discard_key(client, previous_token, previous_alias):
            warning = (
                "Eski anahtar LiteLLM'de silinemedi; bir yöneticiden silmesini isteyin."
            )
        logger.info("Rotated LiteLLM personal key for %s", user_id)
        return GenAiIssuedKey(
            api_key=issued["key"],
            key_alias=account.personal_key_alias,
            base_url=record.base_url,
            litellm_user_id=user_id,
            warning=warning,
        )


async def usage_history(db: AsyncSession, user: User, days: int = 30) -> GenAiUsageHistory:
    account = await _account(db, user)
    if account is None:
        return GenAiUsageHistory(available=False)
    try:
        client, _record = await _client(db)
        end = date.today()
        entries = await client.daily_activity(
            account.litellm_user_id, end - timedelta(days=days - 1), end
        )
    except (GenAiUnavailable, LiteLLMConnectionError, ValueError, TypeError) as exc:
        logger.info("LiteLLM daily activity unavailable: %s", exc)
        return GenAiUsageHistory(available=False)
    return GenAiUsageHistory(
        available=True, days=[GenAiDailyUsage(**entry) for entry in entries]
    )


async def _discard_key(client: LiteLLMClient, token: str, alias: str = "") -> bool:
    if not token and not alias:
        return False
    try:
        await client.delete_key(token=token, alias=alias)
        return True
    except (LiteLLMConnectionError, LiteLLMConfigurationError) as exc:
        logger.warning("Could not delete LiteLLM key: %s", exc)
        return False
