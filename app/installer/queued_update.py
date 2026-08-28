"""Root-owned systemd worker for controller release uploads."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> int:
    root = Path(settings.UPDATE_QUEUE_ROOT).resolve()
    pending = root / "pending.json"
    running = root / "running.json"
    status = root / "status.json"
    if running.is_file():
        # Resume the same idempotent release request after a reboot or abrupt
        # termination. Immutable release staging makes replay safe.
        pass
    elif pending.is_file():
        os.replace(pending, running)
    else:
        return 0
    try:
        request = json.loads(running.read_text(encoding="utf-8"))
        bundle = Path(str(request.get("bundle") or "")).resolve()
        uploads = (root / "uploads").resolve()
        if bundle.parent != uploads or not bundle.is_file() or bundle.is_symlink():
            raise RuntimeError("Queued release path is outside the upload directory")
        setup = Path(__file__).resolve().parents[2] / "deploy" / "devcloud-setup.sh"
        if not setup.is_file():
            raise RuntimeError(f"Active release installer is missing: {setup}")
        command = [
            "bash",
            str(setup),
            "--yes",
            "update",
            "--bundle",
            str(bundle),
        ]
        if request.get("allow_unsigned"):
            command.append("--allow-unsigned")
        _write_json(
            status,
            {
                "state": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "filename": request.get("filename"),
            },
        )
        result = subprocess.run(command, text=True, capture_output=True)
        _write_json(
            status,
            {
                "state": "succeeded" if result.returncode == 0 else "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "filename": request.get("filename"),
                "return_code": result.returncode,
                "output": (result.stdout + "\n" + result.stderr)[-20000:],
            },
        )
        return result.returncode
    except Exception as exc:
        _write_json(
            status,
            {
                "state": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            },
        )
        return 1
    finally:
        running.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
