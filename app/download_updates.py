"""Admin-triggered builder and publisher for air-gap download bundles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings


logger = logging.getLogger(__name__)

BUNDLE_PATTERN = re.compile(r"^devcloud-offline-([0-9a-f]{12})\.tar\.gz$")
MAX_LOG_LINES = 120
ACTIVE_TASKS: set[asyncio.Task[None]] = set()


class DownloadUpdateError(RuntimeError):
    """Base class for an update request that cannot be started."""


class DownloadUpdateDisabled(DownloadUpdateError):
    """Download updates are not enabled in configuration."""


class DownloadUpdateInProgress(DownloadUpdateError):
    """Another worker already owns the cross-process update lock."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class DownloadUpdateManager:
    """Coordinate one durable-status update job across Uvicorn workers."""

    @property
    def project_root(self) -> Path:
        return Path(settings.BASE_DIR).resolve()

    @property
    def download_root(self) -> Path:
        return Path(settings.DOWNLOADS_ROOT).expanduser().resolve()

    @property
    def build_root(self) -> Path:
        return Path(settings.DOWNLOAD_BUILD_ROOT).expanduser().resolve()

    @property
    def status_path(self) -> Path:
        return self.project_root / "data" / "download_update_status.json"

    @property
    def lock_path(self) -> Path:
        return self.project_root / "data" / "download_update.lock"

    def current_bundle(self) -> dict[str, Any] | None:
        root = self.download_root
        if not root.is_dir():
            return None
        candidates = [
            path
            for path in root.glob("devcloud-offline-*.tar.gz")
            if path.is_file() and BUNDLE_PATTERN.fullmatch(path.name)
            and not path.is_symlink()
        ]
        if not candidates:
            return None
        bundle = max(candidates, key=lambda path: path.stat().st_mtime)
        checksum = bundle.with_name(bundle.name + ".sha256")
        checksum_available = checksum.is_file() and not checksum.is_symlink()
        return {
            "filename": bundle.name,
            "checksum_filename": checksum.name if checksum_available else None,
            "size": bundle.stat().st_size,
            "size_display": _format_size(bundle.stat().st_size),
            "download_url": f"/download/{bundle.name}",
            "checksum_url": f"/download/{checksum.name}" if checksum_available else None,
            "modified_at": datetime.fromtimestamp(
                bundle.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        }

    def get_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "state": "idle",
            "message": "Henüz indirme güncellemesi başlatılmadı.",
            "logs": [],
        }
        if self.status_path.is_file():
            try:
                loaded = json.loads(self.status_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    status.update(loaded)
            except (OSError, json.JSONDecodeError):
                logger.warning("Could not read download update status", exc_info=True)
        if status.get("state") in {"queued", "running"} and self._remove_stale_lock():
            status["state"] = "failed"
            status["message"] = (
                "Önceki indirme güncellemesi kesildi. Yeni bir güncelleme başlatın."
            )
            status["finished_at"] = _utc_now()
            logs = status.setdefault("logs", [])
            logs.append("HATA: Güncelleme worker işlemi artık çalışmıyor.")
            del logs[:-MAX_LOG_LINES]
            self._write_status(status)
        status["enabled"] = (
            settings.DOWNLOADS_ENABLED and settings.DOWNLOAD_UPDATES_ENABLED
        )
        status["current"] = self.current_bundle()
        status["target_python_version"] = (
            settings.DOWNLOAD_TARGET_PYTHON_VERSION
            or f"{sys.version_info.major}.{sys.version_info.minor}"
        )
        return status

    def start(self) -> dict[str, Any]:
        if not settings.DOWNLOADS_ENABLED or not settings.DOWNLOAD_UPDATES_ENABLED:
            raise DownloadUpdateDisabled(
                "İndirme yayını devre dışı. DOWNLOADS_ENABLED=True ve "
                "DOWNLOAD_UPDATES_ENABLED=True olarak ayarlayın."
            )
        self._acquire_lock()
        try:
            status = {
                "state": "queued",
                "message": "İndirme paketi güncellemesi sıraya alındı.",
                "started_at": _utc_now(),
                "finished_at": None,
                "logs": ["Güncelleme yönetim API tarafından kabul edildi."],
            }
            self._write_status(status)
            task = asyncio.create_task(self._run(status))
        except Exception:
            self._release_lock()
            raise
        ACTIVE_TASKS.add(task)
        task.add_done_callback(ACTIVE_TASKS.discard)
        return self.get_status()

    def _acquire_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    handle.write(str(os.getpid()))
                return
            except FileExistsError:
                if attempt == 0 and self._remove_stale_lock():
                    continue
                raise DownloadUpdateInProgress(
                    "Başka bir indirme güncellemesi zaten çalışıyor."
                )

    def _remove_stale_lock(self) -> bool:
        try:
            owner_pid = int(self.lock_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            owner_pid = -1
        if owner_pid > 0:
            try:
                os.kill(owner_pid, 0)
                return False
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
        try:
            self.lock_path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _release_lock(self) -> None:
        try:
            owner_pid = int(self.lock_path.read_text(encoding="ascii").strip())
            if owner_pid == os.getpid():
                self.lock_path.unlink(missing_ok=True)
        except (OSError, ValueError):
            logger.warning("Could not release download update lock", exc_info=True)

    def _write_status(self, status: dict[str, Any]) -> None:
        status["updated_at"] = _utc_now()
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_name(
            f".{self.status_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.status_path)

    def _append_log(self, status: dict[str, Any], text: str) -> None:
        line = text.strip().replace("\x00", "")
        if not line:
            return
        logs = status.setdefault("logs", [])
        logs.append(line[-4000:])
        del logs[:-MAX_LOG_LINES]
        self._write_status(status)

    async def _run(self, status: dict[str, Any]) -> None:
        try:
            status["state"] = "running"
            status["message"] = "Güncel çevrim dışı paket hazırlanıyor..."
            self._write_status(status)
            result = await self._build_and_publish(status)
            status.update(result)
            status["state"] = "success"
            status["finished_at"] = _utc_now()
            self._append_log(status, result["message"])
        except Exception as exc:  # The background task must persist its failure state.
            logger.exception("Download bundle update failed")
            status["state"] = "failed"
            status["message"] = str(exc)
            status["finished_at"] = _utc_now()
            self._append_log(status, f"ERROR: {exc}")
        finally:
            self._release_lock()

    async def _build_and_publish(self, status: dict[str, Any]) -> dict[str, Any]:
        commit = await self._git_commit()
        status["source_commit"] = commit
        existing = self.download_root / f"devcloud-offline-{commit[:12]}.tar.gz"
        if existing.is_file() and existing.with_name(existing.name + ".sha256").is_file():
            try:
                await asyncio.to_thread(
                    self._verify_pair, existing, existing.with_name(existing.name + ".sha256")
                )
            except RuntimeError as exc:
                self._append_log(status, f"Mevcut paket geçersiz; yeniden oluşturuluyor: {exc}")
            else:
                return {
                    "message": f"Yayımlanan paket zaten {commit[:12]} commit sürümünde güncel.",
                    "published_filename": existing.name,
                }

        self.build_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="download-update-", dir=self.build_root
        ) as job_dir_text:
            job_dir = Path(job_dir_text)
            output_dir = job_dir / "output"
            temp_dir = job_dir / "tmp"
            temp_dir.mkdir()
            target_python = (
                settings.DOWNLOAD_TARGET_PYTHON_VERSION
                or f"{sys.version_info.major}.{sys.version_info.minor}"
            )
            command = [
                sys.executable,
                str(self.project_root / "deploy" / "package_offline.py"),
                "--python-version",
                target_python,
                "--output-dir",
                str(output_dir),
            ]
            environment = os.environ.copy()
            environment["TMPDIR"] = str(temp_dir)
            self._append_log(
                status,
                f"{commit[:12]} commit sürümü CPython {target_python} için oluşturuluyor.",
            )
            await self._run_process(command, status, environment)

            archives = list(output_dir.glob("devcloud-offline-*.tar.gz"))
            if len(archives) != 1:
                raise RuntimeError(
                    f"Bir arşiv bekleniyordu, {len(archives)} arşiv bulundu."
                )
            archive = archives[0]
            checksum = archive.with_name(archive.name + ".sha256")
            await asyncio.to_thread(self._verify_pair, archive, checksum)
            await asyncio.to_thread(self._publish_pair, archive, checksum)
            return {
                "message": f"{archive.name} başarıyla yayımlandı.",
                "published_filename": archive.name,
            }

    async def _git_commit(self) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=self.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"Güncel Git commit belirlenemedi: {stderr.decode().strip()}"
            )
        commit = stdout.decode("ascii").strip().lower()
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise RuntimeError("Git geçersiz bir kaynak commit döndürdü.")
        return commit

    async def _run_process(
        self,
        command: list[str],
        status: dict[str, Any],
        environment: dict[str, str],
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.project_root,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        status["builder_pid"] = process.pid
        self._write_status(status)
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            self._append_log(status, line.decode("utf-8", errors="replace"))
        return_code = await process.wait()
        if return_code != 0:
            raise RuntimeError(f"Çevrim dışı paketleyici {return_code} koduyla kapandı.")

    def _verify_pair(
        self,
        archive: Path,
        checksum: Path,
        expected_filename: str | None = None,
    ) -> None:
        if (
            not archive.is_file()
            or not checksum.is_file()
            or archive.is_symlink()
            or checksum.is_symlink()
        ):
            raise RuntimeError("Oluşturulan arşiv veya checksum dosyası eksik.")
        fields = checksum.read_text(encoding="ascii").strip().split("  ", maxsplit=1)
        if len(fields) != 2 or fields[1] != (expected_filename or archive.name):
            raise RuntimeError("Oluşturulan checksum dosyasının formatı geçersiz.")
        if _sha256_file(archive) != fields[0].lower():
            raise RuntimeError("Oluşturulan arşivin checksum doğrulaması başarısız.")

    def _publish_pair(self, archive: Path, checksum: Path) -> None:
        root = self.download_root
        if root == Path(root.anchor) or root == self.project_root:
            raise RuntimeError(
                "DOWNLOADS_ROOT dosya sistemi kökü veya proje kökü olamaz."
            )
        root.mkdir(parents=True, exist_ok=True)
        temporary_archive = root / f".{archive.name}.{uuid.uuid4().hex}.uploading"
        temporary_checksum = root / f".{checksum.name}.{uuid.uuid4().hex}.uploading"
        published_archive = root / archive.name
        published_checksum = root / checksum.name
        try:
            shutil.copy2(archive, temporary_archive)
            shutil.copy2(checksum, temporary_checksum)
            os.chmod(temporary_archive, 0o644)
            os.chmod(temporary_checksum, 0o644)
            self._verify_pair(
                temporary_archive, temporary_checksum, expected_filename=archive.name
            )
            os.replace(temporary_archive, published_archive)
            os.replace(temporary_checksum, published_checksum)

            for old_archive in root.glob("devcloud-offline-*.tar.gz"):
                if (
                    old_archive != published_archive
                    and old_archive.is_file()
                    and BUNDLE_PATTERN.fullmatch(old_archive.name)
                ):
                    old_checksum = old_archive.with_name(old_archive.name + ".sha256")
                    old_archive.unlink()
                    old_checksum.unlink(missing_ok=True)
        finally:
            temporary_archive.unlink(missing_ok=True)
            temporary_checksum.unlink(missing_ok=True)


download_update_manager = DownloadUpdateManager()
