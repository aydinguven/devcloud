"""Root-owned systemd worker for controller release uploads."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.installer.update_source import validate_git_source


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main() -> int:
    root = Path(
        os.environ.get("UPDATE_QUEUE_ROOT", "/var/lib/devcloud/update-queue")
    ).resolve()
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
        setup = Path(__file__).resolve().parents[2] / "deploy" / "devcloud-setup.sh"
        if not setup.is_file():
            raise RuntimeError(f"Active release installer is missing: {setup}")
        source_type = str(request.get("source_type") or "bundle")
        if source_type == "git":
            repository, ref = validate_git_source(
                str(request.get("repository") or ""),
                str(request.get("ref") or ""),
            )
            command = [
                "bash", str(setup), "--yes", "update",
                "--source-type", "git", "--repository", repository,
                "--ref", ref,
            ]
        elif source_type == "bundle":
            bundle = Path(str(request.get("bundle") or "")).resolve()
            uploads = (root / "uploads").resolve()
            if bundle.parent != uploads or not bundle.is_file() or bundle.is_symlink():
                raise RuntimeError("Queued release path is outside the upload directory")
            command = [
                "bash", str(setup), "--yes", "update", "--bundle", str(bundle)
            ]
        else:
            raise RuntimeError("Queued update source type is unsupported")
        if request.get("allow_unsigned") is True:
            command.append("--allow-unsigned")
        _write_json(
            status,
            {
                "state": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "filename": request.get("filename"),
                "source_type": source_type,
                "target_version": request.get("target_version"),
            },
        )
        result = subprocess.run(command, text=True, capture_output=True)
        _write_json(
            status,
            {
                "state": "succeeded" if result.returncode == 0 else "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "filename": request.get("filename"),
                "source_type": source_type,
                "target_version": request.get("target_version"),
                "return_code": result.returncode,
                "output": (result.stdout + "\n" + result.stderr)[-20000:],
            },
        )
        if result.returncode == 0 and source_type == "bundle":
            bundle.unlink(missing_ok=True)
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
