"""Public, read-only delivery of verified DevCloud air-gap bundles."""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user_optional
from app.config import settings
from app.database import get_db
from app.download_config import normalize_public_base_url
from app.models.download_settings import DownloadSettings
from app.models.user import User


DOWNLOAD_NAME_PATTERN = re.compile(
    r"^devcloud-(?:worker-)?offline-(?:v?[0-9]+\.[0-9]+\.[0-9]+-[0-9]{8}-)?[0-9a-f]{12}\.tar\.gz(?:\.sha256)?$"
)
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)
templates.env.globals["app_version"] = settings.APP_VERSION
download_router = APIRouter(include_in_schema=False)
worker_bootstrap_template = (
    Path(__file__).resolve().parent.parent / "templates" / "install_worker.sh"
)


def _download_root() -> Path:
    return Path(settings.DOWNLOADS_ROOT).expanduser().resolve()


def _require_downloads_enabled() -> Path:
    if not settings.DOWNLOADS_ENABLED:
        raise HTTPException(status_code=404, detail="İndirmeler etkin değil.")
    root = _download_root()
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="Kullanılabilir indirme bulunamadı.")
    return root


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _public_base_url(
    request: Request,
    download_settings: DownloadSettings | None = None,
) -> str:
    configured = (
        download_settings.public_base_url
        if download_settings and download_settings.public_base_url
        else settings.DOWNLOAD_PUBLIC_BASE_URL
    )
    value = configured.strip() or str(request.base_url).strip()
    try:
        return normalize_public_base_url(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail="Geçerli public Master URL ayarlanmamış.",
        ) from exc


def _latest_worker_bundle(root: Path) -> tuple[Path, Path]:
    candidates = [
        path
        for path in root.glob("devcloud-worker-offline-*.tar.gz")
        if path.is_file()
        and not path.is_symlink()
        and DOWNLOAD_NAME_PATTERN.fullmatch(path.name)
        and path.with_name(path.name + ".sha256").is_file()
        and not path.with_name(path.name + ".sha256").is_symlink()
    ]
    if not candidates:
        raise HTTPException(status_code=404, detail="Yayımlanmış Worker paketi bulunamadı.")
    archive = max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
    return archive, archive.with_name(archive.name + ".sha256")


@download_router.get("/download/", response_class=HTMLResponse)
async def download_index(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List published air-gap bundles and their checksum files."""
    root = _require_downloads_enabled()
    bundles = []
    candidates = [
        path
        for path in root.glob("devcloud*-offline-*.tar.gz")
        if path.is_file()
        and not path.is_symlink()
        and DOWNLOAD_NAME_PATTERN.fullmatch(path.name)
    ]
    for archive in sorted(
        candidates,
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        checksum = archive.with_name(archive.name + ".sha256")
        checksum_available = checksum.is_file() and not checksum.is_symlink()
        bundles.append(
            {
                "filename": archive.name,
                "bundle_role": (
                    "worker" if archive.name.startswith("devcloud-worker-") else "server"
                ),
                "url": f"/download/{archive.name}",
                "size_display": _format_size(archive.stat().st_size),
                "modified_at": datetime.fromtimestamp(
                    archive.stat().st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
                "checksum_filename": checksum.name if checksum_available else None,
                "checksum_url": f"/download/{checksum.name}" if checksum_available else None,
            }
        )
    worker_bootstrap_url = None
    if any(
        bundle["bundle_role"] == "worker" and bundle["checksum_url"]
        for bundle in bundles
    ):
        download_settings = await db.get(DownloadSettings, 1)
        worker_bootstrap_url = (
            f"{_public_base_url(request, download_settings)}/download/install-worker.sh"
        )
    return templates.TemplateResponse(
        request=request,
        name="downloads.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "bundles": bundles,
            "worker_bootstrap_url": worker_bootstrap_url,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@download_router.get("/download/install-worker.sh", response_class=PlainTextResponse)
async def download_worker_bootstrap(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Render a thin bootstrapper for the latest verified worker bundle."""
    root = _require_downloads_enabled()
    archive, checksum = _latest_worker_bundle(root)
    download_settings = await db.get(DownloadSettings, 1)
    base_url = _public_base_url(request, download_settings)
    values = {
        "__MASTER_URL__": shlex.quote(base_url),
        "__BUNDLE_URL__": shlex.quote(f"{base_url}/download/{archive.name}"),
        "__CHECKSUM_URL__": shlex.quote(f"{base_url}/download/{checksum.name}"),
        "__BUNDLE_FILENAME__": shlex.quote(archive.name),
        "__CHECKSUM_FILENAME__": shlex.quote(checksum.name),
    }
    script = worker_bootstrap_template.read_text(encoding="utf-8")
    for placeholder, value in values.items():
        script = script.replace(placeholder, value)
    return PlainTextResponse(
        script,
        media_type="text/plain",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'inline; filename="install-worker.sh"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@download_router.api_route("/download/{filename}", methods=["GET", "HEAD"])
async def download_file(filename: str):
    """Stream one allow-listed bundle file with byte-range support."""
    root = _require_downloads_enabled()
    if not DOWNLOAD_NAME_PATTERN.fullmatch(filename):
        raise HTTPException(status_code=404, detail="İndirme bulunamadı.")
    candidate = root / filename
    path = candidate.resolve()
    if candidate.is_symlink() or path.parent != root or not path.is_file():
        raise HTTPException(status_code=404, detail="İndirme bulunamadı.")
    media_type = "text/plain" if filename.endswith(".sha256") else "application/gzip"
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
