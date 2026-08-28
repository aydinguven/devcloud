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

