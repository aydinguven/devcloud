import importlib.util
import tarfile
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


def test_worker_manifest_role_is_verified(tmp_path: Path):
    bundle_root = tmp_path / "devcloud-worker"
    wheels_dir = bundle_root / "offline" / "wheels"
    images_dir = bundle_root / "offline" / "images"
    wheels_dir.mkdir(parents=True)
    images_dir.mkdir(parents=True)
    (bundle_root / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    (wheels_dir / "httpx-1-py3-none-any.whl").write_bytes(b"wheel")
    for name, _, _ in package_offline.IMAGES:
        (images_dir / f"{name}.tar").write_bytes(name.encode("utf-8"))

    package_offline.write_manifest(
        bundle_root,
        git_commit="b" * 40,
        python_versions=["3.12"],
        bundle_role="worker",
    )

    manifest = package_offline.verify_staged_bundle(
        bundle_root,
        expected_role="worker",
    )
    assert manifest["bundle_role"] == "worker"
    with pytest.raises(package_offline.PackageError, match="server bundle was expected"):
        package_offline.verify_staged_bundle(bundle_root, expected_role="server")


def test_worker_bundle_cli_uses_explicit_role():
    args = package_offline.parse_args(["--bundle-role", "worker"])
    assert args.bundle_role == "worker"


@pytest.mark.parametrize(
    ("distribution_id", "version_id"),
    (("rocky", "10.2"), ("rhel", "10.1")),
)
def test_system_package_profile_targets_el10_distribution(
    tmp_path: Path,
    distribution_id: str,
    version_id: str,
):
    os_release = tmp_path / "os-release"
    os_release.write_text(
        f'ID="{distribution_id}"\nVERSION_ID="{version_id}"\n',
        encoding="utf-8",
    )

    profile = package_offline.detect_system_package_profile(
        os_release,
        system_name="Linux",
        machine="x86_64",
    )

    assert profile["profile"] == f"{distribution_id}-10-x86_64"
    assert "podman" in profile["requested_packages"]
    assert "crun" in profile["requested_packages"]
    assert "subscription-manager" in profile["requested_packages"]
    assert "postgresql-server" in profile["requested_packages"]
    assert "postgresql" in profile["requested_packages"]


def test_worker_system_package_profile_omits_controller_only_packages(
    tmp_path: Path,
):
    os_release = tmp_path / "os-release"
    os_release.write_text(
        'ID="rocky"\nVERSION_ID="10.2"\n',
        encoding="utf-8",
    )

    profile = package_offline.detect_system_package_profile(
        os_release,
        bundle_role="worker",
        system_name="Linux",
        machine="x86_64",
    )

    assert profile["bundle_role"] == "worker"
    assert "podman" in profile["requested_packages"]
    assert "subscription-manager" in profile["requested_packages"]
    assert "nginx" not in profile["requested_packages"]
    assert "postgresql-server" not in profile["requested_packages"]


def test_system_package_profile_rejects_unsupported_release(tmp_path: Path):
    os_release = tmp_path / "os-release"
    os_release.write_text('ID="rocky"\nVERSION_ID="9.6"\n', encoding="utf-8")

    with pytest.raises(package_offline.PackageError, match="major version 10"):
        package_offline.detect_system_package_profile(
            os_release,
            system_name="Linux",
            machine="x86_64",
        )


def test_system_rpm_download_collects_dependency_closure_and_checksums(
    tmp_path: Path, monkeypatch
):
    os_release = tmp_path / "os-release"
    os_release.write_text('ID="rocky"\nVERSION_ID="10.2"\n', encoding="utf-8")
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if "--destdir" in command:
            destination = Path(command[command.index("--destdir") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            for package in package_offline.SYSTEM_PACKAGES_BY_DISTRIBUTION["rocky"]:
                (destination / f"{package}-1-1.el10.x86_64.rpm").write_bytes(
                    package.encode()
                )
        else:
            destination = Path(command[-1])
            repodata = destination / "repodata"
            repodata.mkdir(parents=True)
            (repodata / "repomd.xml").write_text("<repomd/>\n", encoding="utf-8")
            (repodata / "primary.xml.gz").write_bytes(b"metadata")

    monkeypatch.setattr(package_offline, "run", fake_run)
    rpm_root = tmp_path / "system-rpms"
    profile = package_offline.download_system_rpms(
        rpm_root,
        dnf_bin="dnf5",
        os_release_path=os_release,
        system_name="Linux",
        machine="x86_64",
    )
    assert "nginx" in profile["requested_packages"]

    command = commands[0]
    assert command[:4] == ["dnf5", "download", "--resolve", "--alldeps"]
    assert "podman" in command
    assert "subscription-manager" in command
    assert "postgresql-server" in command
    assert profile["profile"] == "rocky-10-x86_64"
    assert commands[1][0:2] == ["createrepo_c", "--no-database"]
    checksum_index = (rpm_root / "SHA256SUMS").read_text(encoding="ascii")
    assert "rocky-10-x86_64/podman-1-1.el10.x86_64.rpm" in checksum_index
    assert "rocky-10-x86_64/REQUESTED_PACKAGES" in checksum_index
    assert "rocky-10-x86_64/repodata/repomd.xml" in checksum_index


def test_manifest_verifies_system_rpm_profile_and_checksum_index(tmp_path: Path):
    bundle_root = tmp_path / "devcloud"
    wheels_dir = bundle_root / "offline" / "wheels"
    images_dir = bundle_root / "offline" / "images"
    rpm_root = bundle_root / "offline" / "system-rpms"
    rpm_dir = rpm_root / "rocky-10-x86_64"
    wheels_dir.mkdir(parents=True)
    images_dir.mkdir(parents=True)
    rpm_dir.mkdir(parents=True)
    (bundle_root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (wheels_dir / "fastapi-1-py3-none-any.whl").write_bytes(b"wheel")
    for name, _, _ in package_offline.IMAGES:
        (images_dir / f"{name}.tar").write_bytes(name.encode("utf-8"))
    for package in package_offline.SYSTEM_PACKAGES_BY_DISTRIBUTION["rocky"]:
        (rpm_dir / f"{package}-1-1.el10.x86_64.rpm").write_bytes(package.encode())
    (rpm_dir / "REQUESTED_PACKAGES").write_text(
        "\n".join(package_offline.SYSTEM_PACKAGES_BY_DISTRIBUTION["rocky"]) + "\n",
        encoding="ascii",
    )
    (rpm_dir / "repodata").mkdir()
    (rpm_dir / "repodata/repomd.xml").write_text("<repomd/>\n", encoding="utf-8")
    (rpm_root / "SHA256SUMS").write_text("checksums\n", encoding="ascii")
    profile = {
        "distribution_id": "rocky",
        "version_id": "10.2",
        "major_version": "10",
        "architecture": "x86_64",
        "profile": "rocky-10-x86_64",
        "bundle_role": "server",
        "requested_packages": list(
            package_offline.SYSTEM_PACKAGES_BY_DISTRIBUTION["rocky"]
        ),
    }

    package_offline.write_manifest(
        bundle_root,
        git_commit="c" * 40,
        python_versions=["3.12"],
        system_package_profile=profile,
    )
    manifest = package_offline.verify_staged_bundle(bundle_root)

    assert manifest["target"]["system_packages"]["profile"] == "rocky-10-x86_64"
    assert any(record["kind"] == "system-rpm" for record in manifest["artifacts"])
    assert any(
        record["kind"] == "system-rpm-checksums"
        for record in manifest["artifacts"]
    )
    assert any(
        record["kind"] == "system-rpm-repository-metadata"
        for record in manifest["artifacts"]
    )

def test_outer_checksum_uses_portable_sha256sum_format(tmp_path: Path):
    bundle = tmp_path / "devcloud-offline-test.tar"
    bundle.write_bytes(b"bundle")
    checksum = package_offline.write_outer_checksum(bundle)
    digest, filename = checksum.read_text(encoding="ascii").strip().split("  ")
    assert digest == package_offline.sha256_file(bundle)
    assert filename == bundle.name


def test_bundle_archive_is_gzip_compressed(tmp_path: Path, monkeypatch):
    bundle_root = tmp_path / "source"
    bundle_root.mkdir()
    (bundle_root / "payload.txt").write_text("payload", encoding="utf-8")
    output = tmp_path / "devcloud-offline-test.tar.gz"
    monkeypatch.setattr(package_offline.shutil, "which", lambda _: None)

    package_offline.create_archive(bundle_root, output)

    assert output.read_bytes()[:2] == b"\x1f\x8b"
    with tarfile.open(output, "r:gz") as archive:
        assert archive.extractfile("devcloud/payload.txt").read() == b"payload"


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
