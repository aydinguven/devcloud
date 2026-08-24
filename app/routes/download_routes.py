"""Public, read-only delivery of verified DevCloud air-gap bundles."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import get_current_user_optional
from app.config import settings
from app.models.user import User


DOWNLOAD_NAME_PATTERN = re.compile(
    r"^devcloud-offline-[0-9a-f]{12}\.tar\.gz(?:\.sha256)?$"
)
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)
download_router = APIRouter(include_in_schema=False)


def _download_root() -> Path:
    return Path(settings.DOWNLOADS_ROOT).expanduser().resolve()


def _require_downloads_enabled() -> Path:
    if not settings.DOWNLOADS_ENABLED:
        raise HTTPException(status_code=404, detail="Downloads are not enabled.")
    root = _download_root()
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="No downloads are available.")
    return root


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


@download_router.get("/download/", response_class=HTMLResponse)
async def download_index(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """List published air-gap bundles and their checksum files."""
    root = _require_downloads_enabled()
    bundles = []
    candidates = [
        path
        for path in root.glob("devcloud-offline-*.tar.gz")
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
                "url": f"/download/{archive.name}",
                "size_display": _format_size(archive.stat().st_size),
                "checksum_filename": checksum.name if checksum_available else None,
                "checksum_url": f"/download/{checksum.name}" if checksum_available else None,
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="downloads.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "bundles": bundles,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@download_router.api_route("/download/{filename}", methods=["GET", "HEAD"])
async def download_file(filename: str):
    """Stream one allow-listed bundle file with byte-range support."""
    root = _require_downloads_enabled()
    if not DOWNLOAD_NAME_PATTERN.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Download not found.")
    candidate = root / filename
    path = candidate.resolve()
    if candidate.is_symlink() or path.parent != root or not path.is_file():
        raise HTTPException(status_code=404, detail="Download not found.")
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
