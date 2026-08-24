from pathlib import Path
import pytest
from app.orchestrator.podman_service import PodmanService


@pytest.mark.asyncio
async def test_offline_wheels_directory_exists():
    """Verify that offline wheels directory is populated with packages."""
    wheels_dir = Path(__file__).resolve().parent.parent / "offline" / "wheels"
    assert wheels_dir.exists(), "Offline wheels folder must exist."
    wheel_files = list(wheels_dir.glob("*.whl"))
    assert len(wheel_files) > 10, f"Expected cached wheels, found {len(wheel_files)}"


@pytest.mark.asyncio
async def test_podman_load_offline_images():
    """Test loading container image archives using PodmanService."""
    svc = PodmanService()
    svc._mock_mode = True

    # Create test images folder in ./data/test_offline_tmp
    test_img_dir = Path("./data/test_offline_images")
    test_img_dir.mkdir(parents=True, exist_ok=True)
    try:
        (test_img_dir / "devcloud-vscode-empty.tar").write_bytes(b"dummy tar content")
        (test_img_dir / "devcloud-vscode-python.tar").write_bytes(b"dummy tar content")

        loaded = await svc.load_offline_images(test_img_dir)
        assert len(loaded) == 2
        assert "devcloud-vscode-empty.tar" in loaded
        assert "devcloud-vscode-python.tar" in loaded
    finally:
        # Cleanup
        import shutil
        shutil.rmtree(test_img_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_list_images():
    """Test listing available container images."""
    svc = PodmanService()
    svc._mock_mode = True

    images = await svc.list_images()
    assert len(images) == 4
    assert any("vscode-empty" in img for img in images)
    assert any("vscode-python" in img for img in images)
    assert any("jupyter-python" in img for img in images)
    assert any("vscode-java" in img for img in images)
