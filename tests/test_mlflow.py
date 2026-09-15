import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

import app.routes.mlflow_routes as mlflow_module
from app.routes.workspace_routes import _workspace_mlflow_environment
from app.integrations.mlflow import MlflowClient, MlflowConfig, MlflowConnectionError
from app.models.mlflow_settings import MlflowSettings
from app.models.mlflow_server_settings import MlflowServerSettings
from tests.conftest import TestingSessionLocal


async def _user_headers(
    client: AsyncClient,
    username: str,
) -> tuple[dict[str, str], int]:
    async with TestingSessionLocal() as session:
        server = await session.get(MlflowServerSettings, 1)
        if server is None:
            session.add(
                MlflowServerSettings(
                    id=1,
                    enabled=True,
                    base_url="https://managed-mlflow.internal",
                )
            )
            await session.commit()
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
):
    return {
        "enabled": True,
        "auth_type": "bearer",
        "username": "",
        "secret": secret,
    }


def _client_config(base_url: str = "https://mlflow.internal") -> MlflowConfig:
    return MlflowConfig(
        enabled=True,
        base_url=base_url,
        auth_type="none",
        username="",
        secret="",
        validate_tls=True,
        ca_cert_file="",
        timeout_seconds=10,
    )


@pytest.mark.asyncio
async def test_mlflow_client_filters_model_versions_by_run_id(monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model_versions": [
                    {"version": "2"},
                    {"version": "10"},
                    {"version": "invalid"},
                ]
            },
        )

    client = MlflowClient(_client_config())
    monkeypatch.setattr(
        client,

        "_client",
        lambda: httpx.AsyncClient(
            base_url=client.config.base_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    payload = await client.search_model_versions(run_id="run-123", max_results=100)

    assert requests[0].url.path == "/api/2.0/mlflow/model-versions/search"
    assert requests[0].url.params["filter"] == "run_id = 'run-123'"
    assert requests[0].url.params["max_results"] == "100"
    assert "order_by" not in requests[0].url.params
    assert [item["version"] for item in payload["model_versions"]] == [
        "10",
        "2",
        "invalid",
    ]



@pytest.mark.asyncio
async def test_mlflow_client_preserves_admin_reverse_proxy_prefix(monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"experiments": []})

    client = MlflowClient(_client_config("https://gateway.internal/mlflow"))
    monkeypatch.setattr(
        client,
        "_client",
        lambda: httpx.AsyncClient(
            base_url=f"{client.config.base_url}/",
            transport=httpx.MockTransport(handler),
        ),
    )

    await client.search_experiments(max_results=1)

    assert requests[0].url.path == (
        "/mlflow/api/2.0/mlflow/experiments/search"
    )


@pytest.mark.asyncio
async def test_mlflow_client_surfaces_upstream_400_reason(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error_code": "INVALID_PARAMETER_VALUE", "message": "Bad run id"},
        )

    client = MlflowClient(_client_config())
    monkeypatch.setattr(
        client,
        "_client",
        lambda: httpx.AsyncClient(
            base_url=client.config.base_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(MlflowConnectionError) as exc_info:
        await client.get_run("bad-run")

    assert str(exc_info.value) == "MLflow API isteği başarısız (400): Bad run id"


@pytest.mark.asyncio
async def test_user_mlflow_secret_is_write_only_and_connection_can_be_tested(
    client: AsyncClient,
    monkeypatch,
):
    headers, user_id = await _user_headers(client, "mlflow_user")
    saved = await client.put(
        "/api/mlflow/settings",
        headers=headers,
        json={**_settings_payload(), "base_url": "https://user-override.invalid"},
    )
    assert saved.status_code == 200
    assert saved.json()["has_secret"] is True
    assert "secret" not in saved.json()
    assert "model-registry-token" not in saved.text
    assert saved.json()["base_url"] == "https://managed-mlflow.internal"

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
        assert self.config.base_url == "https://managed-mlflow.internal"
        return 1, 1, 12, "3.6.0"

    monkeypatch.setattr(mlflow_module.MlflowClient, "test", fake_test)
    tested = await client.post(
        "/api/mlflow/settings/test",
        headers=headers,
        json=_settings_payload(secret=None),
    )
    assert tested.status_code == 200
    assert tested.json()["response_time_ms"] == 12
    assert tested.json()["experiment_count"] == 1
    assert tested.json()["registry_available"] is True
    assert tested.json()["server_version"] == "3.6.0"


@pytest.mark.asyncio
async def test_mlflow_settings_and_models_are_isolated_per_user(
    client: AsyncClient,
    monkeypatch,
):
    alice_headers, alice_user_id = await _user_headers(client, "mlflow_alice")
    bob_headers, bob_user_id = await _user_headers(client, "mlflow_bob")
    await client.put(
        "/api/mlflow/settings",
        headers=alice_headers,
        json=_settings_payload("alice-token"),
    )
    await client.put(
        "/api/mlflow/settings",
        headers=bob_headers,
        json=_settings_payload("bob-token"),
    )

    async def fake_search(self, search="", page_token="", max_results=100):
        owner = "alice" if self.config.secret == "alice-token" else "bob"
        assert self.config.secret == f"{owner}-token"
        assert self.config.base_url == "https://managed-mlflow.internal"
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
    assert alice_settings.json()["base_url"] == "https://managed-mlflow.internal"
    assert bob_settings.json()["base_url"] == "https://managed-mlflow.internal"

    async with TestingSessionLocal() as session:
        alice_environment = await _workspace_mlflow_environment(session, alice_user_id)
        bob_environment = await _workspace_mlflow_environment(session, bob_user_id)
    assert alice_environment == {
        "MLFLOW_TRACKING_URI": "https://managed-mlflow.internal",
        "MLFLOW_TRACKING_TOKEN": "alice-token",
    }
    assert bob_environment["MLFLOW_TRACKING_TOKEN"] == "bob-token"


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
        json=_settings_payload("tracking-token"),
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
        "https://managed-mlflow.internal/#/experiments/42"
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
        json=_settings_payload("lineage-token"),
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

    async def fake_versions(self, name="", run_id="", max_results=200):
        assert name == ""
        assert run_id == "run-1"
        assert max_results == 100
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
            "mlflow_url": "https://managed-mlflow.internal/#/models/fraud-model",
        }
    ]
    assert compare.status_code == 200, compare.text
    assert [run["run_id"] for run in compare.json()["runs"]] == ["run-1", "run-2"]
    assert compare.json()["runs"][0]["metrics_map"]["loss"] == 0.1


@pytest.mark.asyncio
async def test_mlflow_run_detail_survives_optional_enrichment_errors(
    client: AsyncClient,
    monkeypatch,
):
    headers, _ = await _user_headers(client, "mlflow_partial_detail")
    await client.put(
        "/api/mlflow/settings",
        headers=headers,
        json=_settings_payload("partial-token"),
    )

    async def fake_get_run(self, run_id):
        return {
            "run": {
                "info": {
                    "run_id": run_id,
                    "experiment_id": "7",
                    "status": "FINISHED",
                }
            }
        }

    async def fail_artifacts(self, run_id, path="", page_token=""):
        raise mlflow_module.MlflowConnectionError("artifact endpoint rejected request")

    async def fail_versions(self, name="", run_id="", max_results=200):
        assert run_id == "run-1"
        raise mlflow_module.MlflowConnectionError("registry endpoint unavailable")

    monkeypatch.setattr(MlflowClient, "get_run", fake_get_run)
    monkeypatch.setattr(MlflowClient, "list_artifacts", fail_artifacts)
    monkeypatch.setattr(MlflowClient, "search_model_versions", fail_versions)

    detail = await client.get("/api/mlflow/runs/run-1", headers=headers)

    assert detail.status_code == 200, detail.text
    assert detail.json()["run_id"] == "run-1"
    assert detail.json()["artifacts"] == []
    assert detail.json()["registered_model_versions"] == []
    assert len(detail.json()["warnings"]) == 2


@pytest.mark.asyncio
async def test_mlflow_run_detail_still_fails_when_core_run_request_fails(
    client: AsyncClient,
    monkeypatch,
):
    headers, _ = await _user_headers(client, "mlflow_failed_detail")
    await client.put(
        "/api/mlflow/settings",
        headers=headers,
        json=_settings_payload("failed-token"),
    )

    async def fail_get_run(self, run_id):
        raise mlflow_module.MlflowConnectionError("run not available")

    async def fake_artifacts(self, run_id, path="", page_token=""):
        return {"files": []}

    async def fake_versions(self, name="", run_id="", max_results=200):
        return {"model_versions": []}

    monkeypatch.setattr(MlflowClient, "get_run", fail_get_run)
    monkeypatch.setattr(MlflowClient, "list_artifacts", fake_artifacts)
    monkeypatch.setattr(MlflowClient, "search_model_versions", fake_versions)

    detail = await client.get("/api/mlflow/runs/run-1", headers=headers)

    assert detail.status_code == 502
    assert detail.json()["detail"] == "run not available"


@pytest.mark.asyncio
async def test_mlflow_overview_metric_history_and_safe_artifact_preview(
    client: AsyncClient,
    monkeypatch,
):
    headers, _ = await _user_headers(client, "mlflow_analytics")
    await client.put(
        "/api/mlflow/settings",
        headers=headers,
        json=_settings_payload("analytics-token"),
    )

    async def fake_experiments(self, page_token="", max_results=100):
        return {"experiments": [{"experiment_id": "9", "name": "Risk"}]}

    async def fake_models(self, search="", page_token="", max_results=100):
        return {"registered_models": [{"name": "risk-model", "latest_versions": [{"version": "2"}]}]}

    async def fake_runs(self, experiment_ids, filter_string="", page_token="", max_results=100):
        return {"runs": [{"info": {"run_id": "run-9", "experiment_id": "9", "status": "FINISHED", "start_time": 10}, "data": {"metrics": [{"key": "loss", "value": 0.1}]}}]}

    async def fake_history(self, run_id, metric_key, max_results=2000):
        assert (run_id, metric_key) == ("run-9", "loss")
        return {"metrics": [{"value": index / 1000, "step": index, "timestamp": index} for index in range(600)]}

    async def fake_artifacts(self, run_id, path="", page_token=""):
        assert run_id == "run-9"
        return {"files": [{"path": "reports/result.json", "is_dir": False, "file_size": 12}]}

    async def fake_download(self, run_id, path, *, max_bytes):
        assert run_id == "run-9" and path == "reports/result.json"
        assert max_bytes == 2 * 1024 * 1024
        return b'{"ok": true}', "application/json"

    monkeypatch.setattr(MlflowClient, "search_experiments", fake_experiments)
    monkeypatch.setattr(MlflowClient, "search_registered_models", fake_models)
    monkeypatch.setattr(MlflowClient, "search_runs", fake_runs)
    monkeypatch.setattr(MlflowClient, "get_metric_history", fake_history)
    monkeypatch.setattr(MlflowClient, "list_artifacts", fake_artifacts)
    monkeypatch.setattr(MlflowClient, "download_artifact", fake_download)

    overview = await client.get("/api/mlflow/overview", headers=headers)
    assert overview.status_code == 200, overview.text
    assert overview.json()["experiment_count"] == 1
    assert overview.json()["model_count"] == 1
    assert overview.json()["status_counts"] == {"FINISHED": 1}

    history = await client.get("/api/mlflow/runs/run-9/metrics/loss/history", headers=headers)
    assert history.status_code == 200, history.text
    assert history.json()["source_point_count"] == 600
    assert len(history.json()["points"]) == 500
    assert history.json()["points"][0]["step"] == 0
    assert history.json()["points"][-1]["step"] == 599

    preview = await client.get(
        "/api/mlflow/runs/run-9/artifacts/preview",
        headers=headers,
        params={"path": "reports/result.json"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["kind"] == "text"
    assert preview.json()["content"] == '{"ok": true}'

    traversal = await client.get(
        "/api/mlflow/runs/run-9/artifacts/preview",
        headers=headers,
        params={"path": "../secret.txt"},
    )
    assert traversal.status_code == 400

    unsupported = await client.get(
        "/api/mlflow/runs/run-9/artifacts/preview",
        headers=headers,
        params={"path": "model.pkl"},
    )
    assert unsupported.status_code == 415
