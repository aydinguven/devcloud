import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from app.installer.platform import CommandRunner, InstallerError
from app.installer.update_source import (
    read_channel,
    resolve_update_bundle,
    validate_git_source,
    write_channel,
)
from app.platform_release import load_platform_release, publish_platform_bundle
from app.release_catalog import latest_release


def _platform_root(root: Path) -> Path:
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text(
        '__version__ = "3.4.0"\n', encoding="utf-8"
    )
    images = {}
    for role, content in (("controller", b"controller-image"), ("worker", b"worker-image")):
        archive = root / f"offline/{role}-images/devcloud-{role}.tar"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(content)
        images[role] = {
            "image": f"localhost/devcloud-{role}:3.4.0",
            "source": f"quay.io/example/devcloud:{role}-3.4.0",
            "digest": "sha256:" + ("a" if role == "controller" else "b") * 64,
            "archive": archive.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    (root / "platform-release.json").write_text(
        json.dumps(
            {
                "format": 1,
                "version": "3.4.0",
                "source_commit": "abcdef123456",
                "workspace_images_included": False,
                "images": images,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_platform_manifest_requires_verified_images_and_excludes_workspaces(tmp_path):
    root = _platform_root(tmp_path / "release")

    release = load_platform_release(root)

    assert release.controller.archive.endswith("devcloud-controller.tar")
    assert release.worker.archive.endswith("devcloud-worker.tar")
    manifest = json.loads((root / "platform-release.json").read_text(encoding="utf-8"))
    manifest["workspace_images_included"] = True
    (root / "platform-release.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InstallerError, match="must not contain workspace images"):
        load_platform_release(root)


def test_platform_bundle_publication_is_visible_to_workers(tmp_path):
    bundle = tmp_path / "devcloud-platform-update-v3.4.0-abcdef1.tar.gz"
    bundle.write_bytes(b"signed-platform-bundle")
    downloads = tmp_path / "downloads"

    published = publish_platform_bundle(bundle, downloads)
    latest = latest_release(downloads)

    assert published.read_bytes() == bundle.read_bytes()
    assert latest is not None
    assert latest.path == published
    assert latest.version == "3.4.0"
    assert published.with_name(published.name + ".sha256").is_file()


def test_git_channel_resolves_a_checksum_pinned_relative_bundle(tmp_path):
    repository = tmp_path / "channel"
    repository.mkdir()
    bundle = repository / "devcloud-platform-update-v3.4.0-abcdef1.tar.gz"
    bundle.write_bytes(b"immutable-release")
    channel_path = write_channel(bundle, repository / "devcloud-update-channel.json", url=bundle.name)
    assert read_channel(repository).sha256 == hashlib.sha256(bundle.read_bytes()).hexdigest()
    subprocess.run(["git", "init", "-q", "-b", "stable"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "add", bundle.name, channel_path.name], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "release channel"], cwd=repository, check=True)

    with resolve_update_bundle(
        source_type="git",
        location=repository.as_uri(),
        ref="stable",
        runner=CommandRunner(),
    ) as resolved:
        assert resolved.read_bytes() == b"immutable-release"


@pytest.mark.parametrize(
    ("location", "ref"),
    [
        ("ext::sh -c evil", "stable"),
        ("http://insecure.example/devcloud.git", "stable"),
        ("https://example.com/devcloud.git", "--upload-pack=evil"),
        ("https://example.com/devcloud.git", "refs/heads/../evil"),
    ],
)
def test_git_channel_rejects_unsafe_transport_or_ref(location, ref):
    with pytest.raises(InstallerError):
        validate_git_source(location, ref)
