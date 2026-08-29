from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.download_config import normalize_public_base_url
from app.models.download_settings import DownloadSettings
from app.models.worker_bootstrap_ticket import WorkerBootstrapTicket
from app.release_catalog import PublishedRelease, latest_release


WORKER_BOOTSTRAP_TTL_SECONDS = 10 * 60


def ticket_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_ticket_token() -> str:
    return secrets.token_urlsafe(32)


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def controller_base_url(request: Request, db: AsyncSession) -> str:
    record = await db.get(DownloadSettings, 1)
    configured = (
        record.public_base_url
        if record and record.public_base_url
        else settings.DOWNLOAD_PUBLIC_BASE_URL
    )
    value = configured.strip() or str(request.base_url).strip()
    try:
        return normalize_public_base_url(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail="Geçerli public Controller URL ayarlanmamış.",
        ) from exc


def current_platform_release() -> PublishedRelease:
    release = latest_release(Path(settings.DOWNLOADS_ROOT))
    if release is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Worker kurulumu için yayımlanmış platform release bulunamadı. "
                "Önce controller platform paketini yayımlayın."
            ),
        )
    return release


async def active_ticket(token: str, db: AsyncSession) -> WorkerBootstrapTicket:
    if not token or len(token) > 256 or any(character.isspace() for character in token):
        raise HTTPException(status_code=404, detail="Worker kurulum bileti bulunamadı.")
    record = (
        await db.execute(
            select(WorkerBootstrapTicket).where(
                WorkerBootstrapTicket.token_hash == ticket_hash(token)
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Worker kurulum bileti bulunamadı.")
    if record.used_at is not None:
        raise HTTPException(status_code=410, detail="Worker kurulum bileti kullanılmış.")
    if utc_datetime(record.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Worker kurulum biletinin süresi dolmuş.")
    return record
