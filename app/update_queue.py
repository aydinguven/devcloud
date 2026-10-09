"""Read the platform update queue shared with the root-owned updater.

The web process only writes requests into this directory; the queued updater
(app.installer.queued_update) moves them through pending -> running -> status.
"""

import json
from pathlib import Path

from app.config import settings
from app.installer.progress import live_details


def update_queue_root() -> Path:
    return Path(settings.UPDATE_QUEUE_ROOT).resolve()


def read_update_status() -> dict:
    root = update_queue_root()
    for name in ("running.json", "pending.json", "status.json"):
        path = root / name
        if path.is_file() and not path.is_symlink():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    if name == "pending.json":
                        value.setdefault("state", "queued")
                    elif name == "running.json":
                        # running.json is the moved request, which still says
                        # "queued" when an older updater wrote it.
                        value["state"] = "running"
                    value.update(live_details(root, path, value))
                    return value
            except (OSError, json.JSONDecodeError):
                return {
                    "state": "unknown",
                    "error": (
                        f"Güncelleme durum dosyası okunamadı ({name}). "
                        "Updater servisinin dosya izinlerini kontrol edin."
                    ),
                }
    return {"state": "idle"}
