import pytest
from httpx import AsyncClient
from sqlalchemy import select

import app.routes.mlflow_routes as mlflow_module
from app.integrations.mlflow import MlflowClient
from app.models.mlflow_settings import MlflowSettings
from tests.conftest import TestingSessionLocal


async def _user_headers(
    client: AsyncClient,
    username: str,
) -> tuple[dict[str, str], int]:
    response = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "Password123!",
        },
    )
    return (
        {"Authorization": f"Bearer {response.json()['access_token']}"},
        response.json()["user"]["id"],
    )


def _settings_payload(
    secret: str | None = "model-registry-token",
    base_url: str = "https://mlflow.internal",
):
    return {
        "enabled": True,
        "base_url": base_url,
        "auth_type": "bearer",
        "username": "",
        "secret": secret,
        "validate_tls": True,
        "ca_cert_file": "",
        "timeout_seconds": 10,
    }


@pytest.mark.asyncio
async def test_user_mlflow_secret_is_write_only_and_connection_can_be_tested(
    client: AsyncClient,
    monkeypatch,
):
    headers, user_id = await _user_headers(client, "mlflow_user")
    saved = await client.put(
        "/api/mlflow/settings",
        headers=headers,
        json=_settings_payload(),
    )
    assert saved.status_code == 200
    assert saved.json()["has_secret"] is True
    assert "secret" not in saved.json()
    assert "model-registry-token" not in saved.text

    async with TestingSessionLocal() as session:
        record = (
            await session.execute(
                select(MlflowSettings).where(MlflowSettings.user_id == user_id)
            )
        ).scalar_one()
        assert record.encrypted_secret
        assert record.encrypted_secret != "model-registry-token"

    async def fake_test(self):
        assert self.config.secret == "model-registry-token"
        return 1, 12

    monkeypatch.setattr(mlflow_module.MlflowClient, "test", fake_test)
    tested = await client.post(
        "/api/mlflow/settings/test",
        headers=headers,
        json=_settings_payload(secret=None),
    )
    assert tested.status_code == 200
    assert tested.json()["response_time_ms"] == 12
    assert tested.json()["experiment_count"] == 1


@pytest.mark.asyncio
async def test_mlflow_settings_and_models_are_isolated_per_user(
    client: AsyncClient,
    monkeypatch,
):
    alice_headers, _ = await _user_headers(client, "mlflow_alice")
    bob_headers, _ = await _user_headers(client, "mlflow_bob")
    await client.put(
        "/api/mlflow/settings",
        headers=alice_headers,
        json=_settings_payload("alice-token", "https://alice-mlflow.internal"),
    )
    await client.put(
        "/api/mlflow/settings",
        headers=bob_headers,
        json=_settings_payload("bob-token", "https://bob-mlflow.internal"),
    )

    async def fake_search(self, search="", page_token="", max_results=100):
        owner = "alice" if "alice-mlflow" in self.config.base_url else "bob"
        assert self.config.secret == f"{owner}-token"
        return {
            "registered_models": [
                {
                    "name": f"{owner}-model",
                    "description": "User-owned registry model",
                    "aliases": ["champion"],
                    "tags": [{"key": "owner", "value": owner}],
                    "latest_versions": [{"version": "7", "status": "READY"}],
                }
            ],
            "next_page_token": "",
        }

    monkeypatch.setattr(MlflowClient, "search_registered_models", fake_search)
    alice_models = await client.get("/api/mlflow/models", headers=alice_headers)
    bob_models = await client.get("/api/mlflow/models", headers=bob_headers)

    assert alice_models.status_code == 200
    assert bob_models.status_code == 200
    assert alice_models.json()["models"][0]["name"] == "alice-model"
    assert bob_models.json()["models"][0]["name"] == "bob-model"
    assert alice_models.json()["models"][0]["tags_map"] == {"owner": "alice"}
    assert bob_models.json()["models"][0]["tags_map"] == {"owner": "bob"}

    alice_settings = await client.get("/api/mlflow/settings", headers=alice_headers)
    bob_settings = await client.get("/api/mlflow/settings", headers=bob_headers)
    assert alice_settings.json()["base_url"] == "https://alice-mlflow.internal"
    assert bob_settings.json()["base_url"] == "https://bob-mlflow.internal"


@pytest.mark.asyncio
async def test_user_without_mlflow_settings_gets_setup_guidance(client: AsyncClient):
    headers, _ = await _user_headers(client, "mlflow_unconfigured")
    settings = await client.get("/api/mlflow/settings", headers=headers)
    models = await client.get("/api/mlflow/models", headers=headers)

    assert settings.status_code == 200
    assert settings.json()["enabled"] is False
    assert settings.json()["has_secret"] is False
    assert models.status_code == 503
    assert "ML Modelleri" in models.json()["detail"]


@pytest.mark.asyncio
async def test_mlflow_experiments_and_runs_include_tracking_data_and_links(
    client: AsyncClient,
    monkeypatch,
):
    headers, _ = await _user_headers(client, "mlflow_tracking")
    await client.put(
        "/api/mlflow/settings",
        headers=headers,
        json=_settings_payload("tracking-token", "https://tracking.internal"),
    )

    async def fake_experiments(self, page_token="", max_results=100):
        assert self.config.secret == "tracking-token"
        return {
            "experiments": [
                {
                    "experiment_id": "42",
                    "name": "Fraud Detection",
                    "tags": [{"key": "owner", "value": "risk"}],
                }
            ]
        }

    async def fake_runs(
        self,
        experiment_ids,
        filter_string="",
        page_token="",
        max_results=100,
    ):
        assert experiment_ids == ["42"]
        assert filter_string == "metrics.accuracy > 0.9"
        return {
            "runs": [
                {
                    "info": {
                        "run_id": "run-1",
                        "experiment_id": "42",
                        "status": "FINISHED",
                    },
                    "data": {
                        "params": [{"key": "depth", "value": "8"}],
                        "metrics": [{"key": "accuracy", "value": 0.97}],
                        "tags": [{"key": "mlflow.runName", "value": "baseline"}],
                    },
                }
            ]
        }

    monkeypatch.setattr(MlflowClient, "search_experiments", fake_experiments)
    monkeypatch.setattr(MlflowClient, "search_runs", fake_runs)

    experiments = await client.get("/api/mlflow/experiments", headers=headers)
    runs = await client.get(
        "/api/mlflow/runs",
        headers=headers,
        params={"experiment_id": "42", "filter_string": "metrics.accuracy > 0.9"},
    )
    assert experiments.status_code == 200, experiments.text
    assert experiments.json()["experiments"][0]["tags_map"] == {"owner": "risk"}
    assert experiments.json()["experiments"][0]["mlflow_url"] == (
        "https://tracking.internal/#/experiments/42"
    )
    assert runs.status_code == 200, runs.text
    run = runs.json()["runs"][0]
    assert run["run_name"] == "baseline"
    assert run["params_map"] == {"depth": "8"}
    assert run["metrics_map"] == {"accuracy": 0.97}
    assert run["mlflow_url"].endswith("/#/experiments/42/runs/run-1")

    for path, expected in (
        ("/experiments", "Deneyler ve Run'lar"),
        ("/experiments/42", "MLflow Experiment"),
        ("/runs/run-1", "Model Soy Ağacı"),
        ("/runs/compare?run_ids=run-1&run_ids=run-2", "Run Karşılaştırma"),
    ):
        page = await client.get(path, headers=headers)
        assert page.status_code == 200, page.text
        assert expected in page.text


@pytest.mark.asyncio
async def test_mlflow_run_detail_artifacts_lineage_and_comparison(
    client: AsyncClient,
    monkeypatch,
):
    headers, _ = await _user_headers(client, "mlflow_lineage")
    await client.put(
        "/api/mlflow/settings",
        headers=headers,
        json=_settings_payload("lineage-token", "https://lineage.internal"),
    )

    async def fake_get_run(self, run_id):
        return {
            "run": {
                "info": {"run_id": run_id, "experiment_id": "7", "status": "FINISHED"},
                "data": {
                    "params": [{"key": "seed", "value": run_id[-1]}],
                    "metrics": [{"key": "loss", "value": 0.1 if run_id == "run-1" else 0.2}],
                    "tags": [{"key": "mlflow.runName", "value": f"name-{run_id}"}],
                },
            }
        }

    async def fake_artifacts(self, run_id, path="", page_token=""):
        assert run_id == "run-1"
        return {"files": [{"path": "model/model.pkl", "is_dir": False, "file_size": 123}]}

    async def fake_versions(self, name="", max_results=200):
        assert name == ""
        return {
            "model_versions": [
                {"name": "fraud-model", "version": "3", "run_id": "run-1"},
                {"name": "other-model", "version": "1", "run_id": "other-run"},
            ]
        }

    monkeypatch.setattr(MlflowClient, "get_run", fake_get_run)
    monkeypatch.setattr(MlflowClient, "list_artifacts", fake_artifacts)
    monkeypatch.setattr(MlflowClient, "search_model_versions", fake_versions)

    detail = await client.get("/api/mlflow/runs/run-1", headers=headers)
    compare = await client.get(
        "/api/mlflow/runs/compare",
        headers=headers,
        params=[("run_ids", "run-1"), ("run_ids", "run-2")],
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["artifacts"][0]["path"] == "model/model.pkl"
    assert detail.json()["artifacts"][0]["mlflow_url"].endswith(
        "/runs/run-1/artifacts/model/model.pkl"
    )
    assert detail.json()["registered_model_versions"] == [
        {
            "name": "fraud-model",
            "version": "3",
            "run_id": "run-1",
            "mlflow_url": "https://lineage.internal/#/models/fraud-model",
        }
    ]
    assert compare.status_code == 200, compare.text
    assert [run["run_id"] for run in compare.json()["runs"]] == ["run-1", "run-2"]
    assert compare.json()["runs"][0]["metrics_map"]["loss"] == 0.1
