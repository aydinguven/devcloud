import hashlib
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.config import settings


@pytest.mark.asyncio
async def test_download_listing_file_and_range_support(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch,
):
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    filename = "devcloud-offline-abcdef123456.tar.gz"
    content = b"0123456789abcdef"
    archive = download_root / filename
    archive.write_bytes(content)
    (download_root / f"{filename}.sha256").write_text(
        f"{hashlib.sha256(content).hexdigest()}  {filename}\n",
        encoding="ascii",
    )
    monkeypatch.setattr(settings, "DOWNLOADS_ENABLED", True)
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(download_root))

    listing = await client.get("/download/")
    assert listing.status_code == 200
    assert filename in listing.text
    assert f"{filename}.sha256" in listing.text
    assert listing.headers["cache-control"] == "private, no-store"

    response = await client.get(f"/download/{filename}")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["accept-ranges"] == "bytes"

    partial = await client.get(
        f"/download/{filename}", headers={"Range": "bytes=2-5"}
    )
    assert partial.status_code == 206
    assert partial.content == content[2:6]
    assert partial.headers["content-range"] == f"bytes 2-5/{len(content)}"

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
        await client.get("/download/devcloud-offline-abcdef123456.tar.gz")
    ).status_code == 404
