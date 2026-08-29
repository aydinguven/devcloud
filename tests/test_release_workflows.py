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


def test_platform_release_is_restricted_to_release_triggers_and_rocky_runner():
    content = workflow("release-platform.yml")

    assert "workflow_dispatch:" in content
    assert '"v*.*.*"' in content
    assert "pull_request:" not in content
    assert "runs-on: [self-hosted, linux, x64, rocky10, devcloud-release]" in content
    assert 'Manual releases must be dispatched from main.' in content
    assert 'Tag ${release_tag} does not match app version' in content


def test_platform_release_builds_and_verifies_every_distribution_artifact():
    content = workflow("release-platform.yml")

    assert "deploy/container/build-controller-image.sh" in content
    assert "deploy/container/build-worker-image.sh" in content
    assert "deploy/build_platform_update.py" in content
    assert "--bundle-role server" in content
    assert "--bundle-role worker" in content
    assert "--skip-image-build" in content
    assert "--check-runtime" in content
    assert "prepare_release" in content
    assert "load_platform_release" in content


def test_platform_release_publishes_quay_release_assets_and_stable_channel():
    content = workflow("release-platform.yml")

    assert "QUAY_USERNAME" in content
    assert "QUAY_PASSWORD" in content
    assert "podman push" in content
    assert "gh release create" in content
    assert "gh release upload" in content
    assert "devcloud-update-channel.json" in content
    assert "git push origin HEAD:stable" in content
    assert "contents: write" in content


def test_release_operator_guide_documents_required_controls():
    content = (ROOT / "RELEASE.md").read_text(encoding="utf-8")

    assert "public" in content
    assert "isolated Rocky Linux 10" in content
    assert "QUAY_USERNAME" in content
    assert "RELEASE_GPG_PRIVATE_KEY" in content
    assert "--ref stable" in content
