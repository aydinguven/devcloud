import importlib.util
from pathlib import Path

import pytest

from app.orchestrator.podman_service import PodmanService


ROOT_DIR = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "package_offline", ROOT_DIR / "deploy" / "package_offline.py"
)
assert SPEC and SPEC.loader
package_offline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_offline)


def test_offline_source_directories_are_tracked_without_generated_artifacts():
    assert (ROOT_DIR / "offline" / "wheels" / ".gitkeep").is_file()
    assert (ROOT_DIR / "offline" / "images" / ".gitkeep").is_file()
    assert (ROOT_DIR / "deploy" / "package_offline.py").is_file()


def test_copy_tracked_source_rejects_secrets_and_runtime_data(tmp_path: Path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / ".env").write_text("SECRET=value", encoding="utf-8")

    with pytest.raises(package_offline.PackageError, match="Refusing to package"):
        package_offline.copy_tracked_source(source, destination, [".env"])


def test_manifest_verification_detects_tampered_artifact(tmp_path: Path):
    bundle_root = tmp_path / "devcloud"
    wheels_dir = bundle_root / "offline" / "wheels"
    images_dir = bundle_root / "offline" / "images"
    wheels_dir.mkdir(parents=True)
    images_dir.mkdir(parents=True)
    (bundle_root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (wheels_dir / "fastapi-1-py3-none-any.whl").write_bytes(b"wheel")
    for name, _, _ in package_offline.IMAGES:
        (images_dir / f"{name}.tar").write_bytes(name.encode("utf-8"))

    package_offline.write_manifest(
        bundle_root,
        git_commit="a" * 40,
        python_versions=["3.12"],
    )
    manifest = package_offline.verify_staged_bundle(bundle_root)
    assert manifest["source_commit"] == "a" * 40
    assert len(manifest["artifacts"]) == 6

    (images_dir / "devcloud-vscode-react.tar").write_bytes(b"tampered")
    with pytest.raises(package_offline.PackageError, match="does not match"):
        package_offline.verify_staged_bundle(bundle_root)

    package_offline.write_manifest(
        bundle_root, git_commit="a" * 40, python_versions=["3.12"]
    )
    (images_dir / "unlisted.tar").write_bytes(b"unlisted")
    with pytest.raises(package_offline.PackageError, match="unlisted"):
        package_offline.verify_staged_bundle(bundle_root)

def test_outer_checksum_uses_portable_sha256sum_format(tmp_path: Path):
    bundle = tmp_path / "devcloud-offline-test.tar.gz"
    bundle.write_bytes(b"bundle")
    checksum = package_offline.write_outer_checksum(bundle)
    digest, filename = checksum.read_text(encoding="ascii").strip().split("  ")
    assert digest == package_offline.sha256_file(bundle)
    assert filename == bundle.name


@pytest.mark.asyncio
async def test_podman_load_offline_images(tmp_path: Path):
    svc = PodmanService()
    svc._mock_mode = True
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "devcloud-vscode-empty.tar").write_bytes(b"dummy tar content")
    (images_dir / "devcloud-vscode-python.tar").write_bytes(b"dummy tar content")

    loaded = await svc.load_offline_images(images_dir)
    assert len(loaded) == 2
    assert "devcloud-vscode-empty.tar" in loaded
    assert "devcloud-vscode-python.tar" in loaded


@pytest.mark.asyncio
async def test_list_images():
    svc = PodmanService()
    svc._mock_mode = True

    images = await svc.list_images()
    assert len(images) == 5
    assert any("vscode-empty" in img for img in images)
    assert any("vscode-python" in img for img in images)
    assert any("vscode-react" in img for img in images)
    assert any("jupyter-python" in img for img in images)
    assert any("vscode-java" in img for img in images)
