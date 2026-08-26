"""Content-derived cache keys for browser-served static assets."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import settings


STATIC_ROOT = Path(__file__).resolve().parent / "static"


def build_static_asset_version(
    static_root: Path = STATIC_ROOT,
    app_version: str = settings.APP_VERSION,
) -> str:
    """Return a stable version that changes whenever a static file changes."""
    digest = hashlib.sha256()
    for path in sorted(item for item in static_root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(static_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"{app_version}-{digest.hexdigest()[:12]}"


STATIC_ASSET_VERSION = build_static_asset_version()
