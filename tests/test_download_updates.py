import hashlib
from pathlib import Path

import pytest

from app.config import settings
from app.download_updates import (
    DownloadUpdateDisabled,
    DownloadUpdateManager,
)


def _write_bundle_pair(root: Path, revision: str, content: bytes) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"devcloud-offline-{revision}.tar.gz"
    archive.write_bytes(content)
    checksum = archive.with_name(archive.name + ".sha256")
    checksum.write_text(
        f"{hashlib.sha256(content).hexdigest()}  {archive.name}\n",
        encoding="ascii",
    )
    return archive, checksum


def test_publish_replaces_only_older_bundle_pairs(tmp_path: Path, monkeypatch):
    download_root = tmp_path / "downloads"
    build_root = tmp_path / "build"
    source_root = tmp_path / "source"
    source_root.mkdir()
    monkeypatch.setattr(settings, "BASE_DIR", source_root)
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(download_root))
    monkeypatch.setattr(settings, "DOWNLOAD_BUILD_ROOT", str(build_root))

    old_archive, old_checksum = _write_bundle_pair(
        download_root, "111111111111", b"old bundle"
    )
    unrelated = download_root / "keep-me.txt"
    unrelated.write_text("keep", encoding="utf-8")
    staged_root = tmp_path / "staged"
    new_archive, new_checksum = _write_bundle_pair(
        staged_root, "222222222222", b"new bundle"
    )

    manager = DownloadUpdateManager()
    manager._publish_pair(new_archive, new_checksum)

    published = download_root / new_archive.name
    assert published.read_bytes() == b"new bundle"
    assert published.with_name(published.name + ".sha256").is_file()
    assert not old_archive.exists()
    assert not old_checksum.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert manager.current_bundle()["filename"] == new_archive.name


def test_publish_rejects_tampered_checksum(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(tmp_path / "downloads"))
    archive, checksum = _write_bundle_pair(tmp_path / "staged", "333333333333", b"ok")
    checksum.write_text(f"{'0' * 64}  {archive.name}\n", encoding="ascii")

    manager = DownloadUpdateManager()
    with pytest.raises(RuntimeError, match="checksum verification failed"):
        manager._verify_pair(archive, checksum)


def test_start_rejects_disabled_updates(monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOADS_ENABLED", True)
    monkeypatch.setattr(settings, "DOWNLOAD_UPDATES_ENABLED", False)
    with pytest.raises(DownloadUpdateDisabled):
        DownloadUpdateManager().start()

    monkeypatch.setattr(settings, "DOWNLOADS_ENABLED", False)
    monkeypatch.setattr(settings, "DOWNLOAD_UPDATES_ENABLED", True)
    with pytest.raises(DownloadUpdateDisabled):
        DownloadUpdateManager().start()
