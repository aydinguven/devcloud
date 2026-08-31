from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_ci_runs_tests_without_write_permissions():
    content = workflow("ci.yml")

    assert "pull_request:" in content
    assert "branches:" in content
    assert "contents: read" in content
    assert "python -m pytest -q" in content
    assert "contents: write" not in content


def test_platform_release_is_restricted_to_release_triggers_and_hosted_runner():
    content = workflow("release-platform.yml")

    assert "workflow_dispatch:" in content
    assert '"v*.*.*"' in content
    assert "pull_request:" not in content
    assert "runs-on: ubuntu-24.04" in content
    assert "docker run --privileged --rm" in content
    assert "rockylinux/rockylinux:10.2" in content
    assert "self-hosted" not in content
    assert 'Manual releases must be dispatched from main.' in content
    assert 'Tag ${release_tag} does not match app version' in content


def test_platform_release_builds_and_verifies_every_distribution_artifact():
    workflow_content = workflow("release-platform.yml")
    builder = (
        ROOT / "deploy" / "ci" / "build-release-assets.sh"
    ).read_text(encoding="utf-8")

    assert "deploy/ci/build-release-assets.sh" in workflow_content
    assert "driver = \"vfs\"" in builder
    assert "python -m pytest -q" in builder
    assert "deploy/container/build-controller-image.sh" in builder
    assert "deploy/container/build-worker-image.sh" in builder
    assert "deploy/build_platform_update.py" in builder
    assert "--bundle-role server" in builder
    assert "--bundle-role worker" in builder
    assert "--skip-image-build" in builder
    assert "--check-runtime" in builder
    assert "prepare_release" in builder
    assert "load_platform_release" in builder
    assert '--release-keyring "${ASSET_DIR}/devcloud-release-keyring.gpg"' in builder
    assert "github.event_name == 'push' || inputs.sign_release" in workflow_content


def test_platform_release_publishes_quay_release_assets_and_stable_channel():
    content = workflow("release-platform.yml")
    builder = (
        ROOT / "deploy" / "ci" / "build-release-assets.sh"
    ).read_text(encoding="utf-8")

    assert "QUAY_USERNAME" in content
    assert "QUAY_PASSWORD" in content
    assert "podman push" in builder
    assert "gh release create" in content
    assert "gh release upload" in content
    assert "devcloud-update-channel.json" in content
    assert "git push origin HEAD:stable" in content
    assert "contents: write" in content


def test_release_operator_guide_documents_required_controls():
    content = (ROOT / "RELEASE.md").read_text(encoding="utf-8")

    assert "No self-hosted runner registration is required" in content
    assert "privileged, disposable Rocky Linux" in content
    assert "at least 8 GiB free" in content
    assert "QUAY_USERNAME" in content
    assert "RELEASE_GPG_PRIVATE_KEY" in content
    assert "--ref stable" in content
