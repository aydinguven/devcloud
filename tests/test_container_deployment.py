from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_container_health_endpoints(client):
    health = await client.get("/healthz")
    ready = await client.get("/readyz")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


def test_controller_image_runs_migrations_before_single_uvicorn_process():
    entrypoint = (
        ROOT / "deploy" / "container" / "controller-entrypoint.sh"
    ).read_text(encoding="utf-8")
    containerfile = (
        ROOT / "containers" / "devcloud-controller" / "Containerfile"
    ).read_text(encoding="utf-8")

    assert "python -m app.migrations upgrade" in entrypoint
    assert "--workers 1" in entrypoint
    assert "USER 10001:10001" in containerfile


def test_controller_quadlet_is_offline_and_loopback_only():
    quadlet = (
        ROOT / "deploy" / "container" / "quadlet" / "devcloud-controller.container"
    ).read_text(encoding="utf-8")

    assert "Pull=never" in quadlet
    assert "PublishPort=127.0.0.1:8000:8000" in quadlet
    assert "ReadOnly=true" in quadlet
    assert "EnvironmentFile=/etc/devcloud/controller.env" in quadlet


def test_postgresql_is_not_published_to_the_host():
    quadlet = (
        ROOT / "deploy" / "container" / "quadlet" / "devcloud-postgresql.container"
    ).read_text(encoding="utf-8")

    assert "Network=devcloud.network" in quadlet
    assert "PublishPort=" not in quadlet
    assert "Pull=never" in quadlet


def test_worker_image_and_quadlet_use_rootful_host_podman_socket():
    containerfile = (
        ROOT / "containers" / "devcloud-worker" / "Containerfile"
    ).read_text(encoding="utf-8")
    entrypoint = (
        ROOT / "deploy" / "container" / "worker-entrypoint.sh"
    ).read_text(encoding="utf-8")
    quadlet = (
        ROOT / "deploy" / "container" / "quadlet" / "devcloud-worker.container"
    ).read_text(encoding="utf-8")

    assert "microdnf install -y podman" in containerfile
    assert "USER 0" in containerfile
    assert "podman info" in entrypoint
    assert "Network=host" in quadlet
    assert "Requires=podman.socket" in quadlet
    assert "/run/podman/podman.sock:/run/podman/podman.sock" in quadlet
    assert "SecurityLabelDisable=true" in quadlet
    assert "Privileged=true" not in quadlet


def test_all_builtin_vscode_images_install_locked_cline():
    code_server_base = (
        "FROM docker.io/codercom/code-server:4.137.0@"
        "sha256:57ac684d44deb6fa94317b3e8f3e128dd7fb897fffd95b4efcd16f56ce607971"
    )
    cline_version = "ARG CLINE_VERSION=4.1.17"
    cline_sha256 = (
        "ARG CLINE_VSIX_SHA256="
        "82875472744ded4a360e22c726bb6101962ecf0a178aa8efe84a5c7ea728d2f5"
    )

    for template_id in (
        "vscode-empty",
        "vscode-python",
        "vscode-react",
        "vscode-java",
    ):
        containerfile = (
            ROOT / "containers" / template_id / "Containerfile"
        ).read_text(encoding="utf-8")
        assert code_server_base in containerfile
        assert cline_version in containerfile
        assert cline_sha256 in containerfile
        assert (
            "open-vsx.org/api/saoudrizwan/claude-dev/${CLINE_VERSION}/file/"
            "saoudrizwan.claude-dev-${CLINE_VERSION}.vsix"
        ) in containerfile
        assert (
            'echo "${CLINE_VSIX_SHA256}  /tmp/cline.vsix" | sha256sum -c -'
        ) in containerfile
        assert "code-server --install-extension /tmp/cline.vsix" in containerfile
        assert (
            'grep -Fx "saoudrizwan.claude-dev@${CLINE_VERSION}"'
        ) in containerfile
        assert '"extensions.autoCheckUpdates":false' in containerfile
        assert '"extensions.autoUpdate":false' in containerfile
        assert '"chat.titleBar.signIn.enabled":false' in containerfile
        assert '"chat.byokUtilityModelDefault":"mainAgent"' in containerfile
        assert "code-server:latest" not in containerfile
        assert (
            "code-server --install-extension saoudrizwan.claude-dev"
            not in containerfile
        )

