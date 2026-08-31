from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_jupyter_workspace_image_installs_jupyter_ai_and_claude_acp():
    containerfile = (ROOT / "containers/jupyter-python/Containerfile").read_text(
        encoding="utf-8"
    )

    assert '"jupyter-ai[magics]==3.1.3"' in containerfile
    assert "@anthropic-ai/claude-code@2.1.251" in containerfile
    assert "@agentclientprotocol/claude-agent-acp@0.70.0" in containerfile
    assert "nodejs=22" in containerfile


def test_release_publishes_versioned_jupyter_workspace_image():
    workflow = (
        ROOT / ".github/workflows/release-platform.yml"
    ).read_text(encoding="utf-8")

    assert "Build and smoke-test Jupyter AI image" in workflow
    assert '"jupyter-python-${DEVCLOUD_VERSION}"' in workflow
    assert '"jupyter-python-${DEVCLOUD_VERSION}-${SHORT_SHA}"' in workflow
    assert "Resolve release build scope" in workflow
    assert "git diff --quiet" in workflow
    assert "containers/jupyter-python" in workflow
    assert "rebuild_jupyter" in workflow
    assert "needs: release_scope" in workflow
    assert "needs: [release_scope, jupyter_workspace]" in workflow
    assert "needs.jupyter_workspace.result == 'skipped'" in workflow


def test_worker_forwards_shared_gateway_token_only_in_jupyter_configuration():
    podman_service = (ROOT / "app/orchestrator/podman_service.py").read_text(
        encoding="utf-8"
    )

    jupyter_branch = podman_service.split(
        'elif "jupyter" in template_id:', 1
    )[1].split("for k, v in template.env_vars.items():", 1)[0]
    assert "ANTHROPIC_AUTH_TOKEN" in jupyter_branch
    assert "ANTHROPIC_BASE_URL" in jupyter_branch
    assert "CLAUDE_CODE_EXECUTABLE=" not in jupyter_branch
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT=1" in jupyter_branch
