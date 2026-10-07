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


def test_release_operator_guide_documents_required_controls():
    content = (ROOT / "RELEASE.md").read_text(encoding="utf-8")

    assert "No self-hosted runner registration is required" in content
    assert "privileged, disposable Rocky Linux" in content
    assert "at least 8 GiB free" in content
    assert "built-in `GITHUB_TOKEN`" in content
    assert "private by default" in content
    assert "gzip-compressed Docker archives" in content
    assert "Admin > Workspace Image'ları" in content
    assert "QUAY_USERNAME" in content
    assert "RELEASE_GPG_PRIVATE_KEY" in content
    assert "--ref stable" in content
