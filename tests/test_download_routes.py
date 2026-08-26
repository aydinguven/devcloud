import hashlib
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.download_settings import DownloadSettings


@pytest.mark.asyncio
async def test_download_listing_file_and_range_support(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch,
):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    filename = "devcloud-offline-v2.0.0-20260825-abcdef123456.tar.gz"
    content = b"0123456789abcdef"
    archive = download_root / filename
    archive.write_bytes(content)
    (download_root / f"{filename}.sha256").write_text(
        f"{hashlib.sha256(content).hexdigest()}  {filename}\n",
        encoding="ascii",
    )
    worker_filename = "devcloud-worker-offline-v2.0.0-20260825-fedcba654321.tar.gz"
    worker_content = b"worker-bundle"
    worker_archive = download_root / worker_filename
    worker_archive.write_bytes(worker_content)
    (download_root / f"{worker_filename}.sha256").write_text(
        f"{hashlib.sha256(worker_content).hexdigest()}  {worker_filename}\n",
        encoding="ascii",
    )
    legacy_filename = "devcloud-offline-v2.0.0-20260824-111111111111.tar"
    legacy_archive = download_root / legacy_filename
    legacy_archive.write_bytes(b"legacy-plain-tar-bundle")
    (download_root / f"{legacy_filename}.sha256").write_text(
        f"{hashlib.sha256(b'legacy-plain-tar-bundle').hexdigest()}  {legacy_filename}\n",
        encoding="ascii",
    )
    monkeypatch.setattr(settings, "DOWNLOADS_ENABLED", True)
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(download_root))
    monkeypatch.setattr(settings, "DOWNLOAD_PUBLIC_BASE_URL", "")

    listing = await client.get("/download/")
    assert listing.status_code == 200
    assert filename in listing.text
    assert f"{filename}.sha256" in listing.text
    assert worker_filename in listing.text
    assert legacy_filename in listing.text
    assert "CPU Worker" in listing.text
    assert "http://test/download/install-worker.sh" in listing.text
    assert listing.headers["cache-control"] == "private, no-store"

    bootstrap = await client.get("/download/install-worker.sh")
    assert bootstrap.status_code == 200
    assert bootstrap.headers["content-type"].startswith("text/plain")
    assert bootstrap.headers["cache-control"] == "private, no-store"
    assert f"http://test/download/{worker_filename}" in bootstrap.text
    assert f"http://test/download/{worker_filename}.sha256" in bootstrap.text
    assert "read -r -s NODE_TOKEN" in bootstrap.text
    assert 'NODE_ID="${DEVCLOUD_NODE_ID:-}"' in bootstrap.text
    assert "__BUNDLE_URL__" not in bootstrap.text

    response = await client.get(f"/download/{filename}")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "application/gzip"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["accept-ranges"] == "bytes"

    partial = await client.get(
        f"/download/{filename}", headers={"Range": "bytes=2-5"}
    )
    assert partial.status_code == 206
    assert partial.content == content[2:6]
    assert partial.headers["content-range"] == f"bytes 2-5/{len(content)}"

    worker_response = await client.get(f"/download/{worker_filename}")
    assert worker_response.status_code == 200
    assert worker_response.content == worker_content

    legacy_response = await client.get(f"/download/{legacy_filename}")
    assert legacy_response.status_code == 200
    assert legacy_response.headers["content-type"] == "application/x-tar"

    invalid = await client.get("/download/not-an-allowed-file.zip")
    assert invalid.status_code == 404


@pytest.mark.asyncio
async def test_downloads_are_disabled_by_default(
    client: AsyncClient,
    monkeypatch,
):
    monkeypatch.setattr(settings, "DOWNLOADS_ENABLED", False)
    assert (await client.get("/download/")).status_code == 404
    assert (
        await client.get("/download/devcloud-offline-abcdef123456.tar")
    ).status_code == 404
    assert (await client.get("/download/install-worker.sh")).status_code == 404


@pytest.mark.asyncio
async def test_worker_bootstrap_uses_configured_public_base_url(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch,
):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    filename = "devcloud-worker-offline-v2.0.0-20260825-fedcba654321.tar.gz"
    archive = download_root / filename
    archive.write_bytes(b"worker")
    (download_root / f"{filename}.sha256").write_text(
        f"{hashlib.sha256(b'worker').hexdigest()}  {filename}\n",
        encoding="ascii",
    )
    monkeypatch.setattr(settings, "DOWNLOADS_ENABLED", True)
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(download_root))
    db_session.add(
        DownloadSettings(id=1, public_base_url="https://devcloud.example.com")
    )
    await db_session.commit()
    monkeypatch.setattr(settings, "DOWNLOAD_PUBLIC_BASE_URL", "https://fallback.invalid")

    response = await client.get(
        "/download/install-worker.sh",
        headers={"Host": "attacker.invalid"},
    )

    assert response.status_code == 200
    assert f"https://devcloud.example.com/download/{filename}" in response.text
    assert "attacker.invalid" not in response.text
