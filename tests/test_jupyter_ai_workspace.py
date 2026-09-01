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


def test_release_publishes_versioned_changed_workspace_images():
    workflow = (
        ROOT / ".github/workflows/release-platform.yml"
    ).read_text(encoding="utf-8")

    assert "Build and smoke-test workspace image" in workflow
    assert '"${image}-${DEVCLOUD_VERSION}"' in workflow
    assert '"${image}-${DEVCLOUD_VERSION}-${SHORT_SHA}"' in workflow
    assert "Resolve release build scope" in workflow
    assert "git diff --quiet" in workflow
    assert "containers/${image}" in workflow
    assert "vscode-empty vscode-python vscode-react vscode-java jupyter-python" in workflow
    assert "rebuild_jupyter" in workflow
    assert "needs: release_scope" in workflow
    assert "needs: [release_scope, workspace_images]" in workflow
    assert "needs.workspace_images.result == 'skipped'" in workflow
    assert "code-server --list-extensions | grep -Fx saoudrizwan.claude-dev" in workflow


def test_worker_forwards_shared_gateway_to_jupyter_and_cline_configuration():
    podman_service = (ROOT / "app/orchestrator/podman_service.py").read_text(
        encoding="utf-8"
    )

    vscode_branch, jupyter_branch = podman_service.split(
        "elif is_jupyter:", 1
    )
    vscode_branch = vscode_branch.split("if is_vscode:", 1)[1]
    jupyter_branch = jupyter_branch.split(
        "for k, v in template.env_vars.items():", 1
    )[0]
    assert "managed_cline_files" in vscode_branch
    assert "DEVCLOUD_CLINE_SECRETS_JSON=" in vscode_branch
    assert "DEVCLOUD_CLINE_PROVIDERS_JSON=" in vscode_branch
    assert "ANTHROPIC_AUTH_TOKEN" in jupyter_branch
    assert "ANTHROPIC_BASE_URL" in jupyter_branch
    assert "CLAUDE_CODE_EXECUTABLE=/opt/conda/bin/claude" in jupyter_branch
    assert "DEVCLOUD_CLAUDE_SETTINGS_JSON=" in jupyter_branch
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT=1" in jupyter_branch
