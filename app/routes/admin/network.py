"""Admin API: public download URL and HTTPS ingress settings."""

import ipaddress
import urllib.parse
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.models.user import User
from app.models.download_settings import DownloadSettings
from app.schemas.download_settings import DownloadSettingsOut, DownloadSettingsUpdate
from app.config import settings
from app.ingress_settings import (
    MAX_AGENT_CA_BYTES,
    MAX_CERTIFICATE_BYTES,
    MAX_PRIVATE_KEY_BYTES,
    IngressApplyError,
    IngressConfigurationError,
    ingress_manager,
    normalize_https_hostname,
)

router = APIRouter()


def _download_settings_out(record: DownloadSettings) -> DownloadSettingsOut:
    base_url = record.public_base_url.rstrip("/")
    return DownloadSettingsOut(
        public_base_url=base_url,
        worker_fallback_ipv4=record.worker_fallback_ipv4,
        https_enabled=record.https_enabled,
        https_hostname=record.https_hostname,
        http_fallback_enabled=record.http_fallback_enabled,
        certificate_uploaded=bool(record.certificate_sha256),
        agent_ca_uploaded=ingress_manager.agent_ca_path.is_file(),
        certificate_subject=record.certificate_subject,
        certificate_not_after=record.certificate_not_after,
        certificate_sha256=record.certificate_sha256,
    )


async def _get_or_create_download_settings(db: AsyncSession) -> DownloadSettings:
    record = await db.get(DownloadSettings, 1)
    if record:
        return record
    record = DownloadSettings(
        id=1,
        public_base_url=settings.DOWNLOAD_PUBLIC_BASE_URL,
        https_hostname=settings.HTTPS_DEFAULT_HOSTNAME,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


@router.get("/download-settings", response_model=DownloadSettingsOut)
async def get_download_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _download_settings_out(await _get_or_create_download_settings(db))


@router.put("/download-settings", response_model=DownloadSettingsOut)
async def update_download_settings(
    update: DownloadSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await _get_or_create_download_settings(db)
    record.public_base_url = update.public_base_url
    record.worker_fallback_ipv4 = update.worker_fallback_ipv4
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _download_settings_out(record)


async def _read_upload(upload: UploadFile | None, maximum: int, label: str) -> bytes | None:
    if upload is None or not upload.filename:
        return None
    content = await upload.read(maximum + 1)
    if len(content) > maximum:
        raise HTTPException(
            status_code=413,
            detail=f"{label} izin verilen dosya boyutunu aşıyor.",
        )
    if not content:
        raise HTTPException(status_code=422, detail=f"{label} boş olamaz.")
    return content


@router.post("/download-settings/https", response_model=DownloadSettingsOut)
async def apply_https_settings(
    https_enabled: Annotated[bool, Form()],
    https_hostname: Annotated[str, Form()],
    http_fallback_enabled: Annotated[bool, Form()],
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    certificate: Annotated[UploadFile | None, File()] = None,
    private_key: Annotated[UploadFile | None, File()] = None,
    agent_ca: Annotated[UploadFile | None, File()] = None,
):
    record = await _get_or_create_download_settings(db)
    certificate_pem = await _read_upload(
        certificate, MAX_CERTIFICATE_BYTES, "Sertifika"
    )
    private_key_pem = await _read_upload(
        private_key, MAX_PRIVATE_KEY_BYTES, "Private key"
    )
    agent_ca_pem = await _read_upload(
        agent_ca, MAX_AGENT_CA_BYTES, "Worker CA bundle"
    )
    try:
        hostname = normalize_https_hostname(https_hostname)
        effective_http_fallback = (
            http_fallback_enabled if https_enabled else True
        )
        info = await ingress_manager.apply(
            https_enabled=https_enabled,
            hostname=hostname,
            http_fallback_enabled=effective_http_fallback,
            certificate_pem=certificate_pem,
            private_key_pem=private_key_pem,
            agent_ca_pem=agent_ca_pem,
        )
    except IngressConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IngressApplyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"HTTPS ayar dosyaları yazılamadı: {exc}",
        ) from exc

    if https_enabled and not record.worker_fallback_ipv4:
        previous_host = urllib.parse.urlsplit(record.public_base_url).hostname or ""
        try:
            previous_address = ipaddress.ip_address(previous_host)
        except ValueError:
            previous_address = None
        if (
            previous_address is not None
            and previous_address.version == 4
            and not previous_address.is_loopback
            and not previous_address.is_unspecified
            and not previous_address.is_multicast
        ):
            record.worker_fallback_ipv4 = str(previous_address)

    record.https_enabled = https_enabled
    record.https_hostname = hostname
    record.http_fallback_enabled = effective_http_fallback
    record.public_base_url = (
        f"{'https' if https_enabled else 'http'}://{hostname}"
    )
    if info:
        record.certificate_subject = info.subject
        record.certificate_not_after = info.not_after
        record.certificate_sha256 = info.sha256
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _download_settings_out(record)
