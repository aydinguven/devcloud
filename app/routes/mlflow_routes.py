import asyncio
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.integrations.mlflow import (
    MlflowClient,
    MlflowConfigurationError,
    MlflowConnectionError,
    config_from_record,
    config_from_update,
    normalize_experiment,
    normalize_model,
    normalize_run,
    validate_config,
)
from app.models.mlflow_settings import MlflowSettings
from app.models.user import User
from app.schemas.mlflow import MlflowSettingsOut, MlflowSettingsUpdate, MlflowTestResult
from app.security.secrets import encrypt_secret

mlflow_router = APIRouter(prefix="/api/mlflow", tags=["MLflow"])


async def _mlflow_settings_for_user(
    db: AsyncSession,
    user_id: int,
) -> MlflowSettings | None:
    return (
        await db.execute(
            select(MlflowSettings).where(MlflowSettings.user_id == user_id)
        )
    ).scalar_one_or_none()


def _settings_out(record: MlflowSettings | None) -> MlflowSettingsOut:
    if record is None:
        return MlflowSettingsOut(
            enabled=False,
            base_url="",
            auth_type="none",
            username="",
            has_secret=False,
            validate_tls=True,
            ca_cert_file="",
            timeout_seconds=10,
        )
    return MlflowSettingsOut(
        enabled=record.enabled,
        base_url=record.base_url,
        auth_type=record.auth_type,
        username=record.username,
        has_secret=bool(record.encrypted_secret),
        validate_tls=record.validate_tls,
        ca_cert_file=record.ca_cert_file,
        timeout_seconds=record.timeout_seconds,
    )


async def get_mlflow_client(db: AsyncSession, user_id: int) -> MlflowClient:
    record = await _mlflow_settings_for_user(db, user_id)
    if not record:
        raise HTTPException(
            status_code=503,
            detail="MLflow bağlantınızı ML Modelleri sayfasından yapılandırın.",
        )
    try:
        config = config_from_record(record)
        validate_config(config, require_enabled=True)
        return MlflowClient(config)
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _mlflow_url(client: MlflowClient, fragment: str) -> str:
    return f"{client.config.base_url.rstrip('/')}/#/{fragment.lstrip('/')}"


def _experiment_url(client: MlflowClient, experiment_id: str) -> str:
    return _mlflow_url(client, f"experiments/{quote(experiment_id, safe='')}")


def _run_url(client: MlflowClient, experiment_id: str, run_id: str) -> str:
    return _mlflow_url(
        client,
        f"experiments/{quote(experiment_id, safe='')}/runs/{quote(run_id, safe='')}",
    )


@mlflow_router.get("/settings", response_model=MlflowSettingsOut)
async def get_mlflow_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _settings_out(await _mlflow_settings_for_user(db, current_user.id))


@mlflow_router.put("/settings", response_model=MlflowSettingsOut)
async def update_mlflow_settings(
    update: MlflowSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await _mlflow_settings_for_user(db, current_user.id)
    try:
        candidate = config_from_update(update, record)
        if update.enabled:
            validate_config(candidate)
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if record is None:
        record = MlflowSettings(user_id=current_user.id)
    for field_name, value in update.model_dump(exclude={"secret"}).items():
        setattr(record, field_name, value)
    if update.secret:
        record.encrypted_secret = encrypt_secret(update.secret)
    elif update.auth_type == "none":
        record.encrypted_secret = ""
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _settings_out(record)


@mlflow_router.post("/settings/test", response_model=MlflowTestResult)
async def test_mlflow_settings(
    update: MlflowSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await _mlflow_settings_for_user(db, current_user.id)
    try:
        candidate = config_from_update(update, record)
        validate_config(candidate)
        count, elapsed_ms = await MlflowClient(candidate).test()
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return MlflowTestResult(
        success=True,
        message="Kişisel MLflow bağlantınız başarılı.",
        response_time_ms=elapsed_ms,
        experiment_count=count,
        model_count=0,
    )


@mlflow_router.get("/models")
async def list_mlflow_models(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    search: str = Query(default="", max_length=200),
    page_token: str = Query(default="", max_length=4096),
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.search_registered_models(search=search, page_token=page_token)
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "models": [
            {
                **normalize_model(item),
                "mlflow_url": _mlflow_url(
                    client, f"models/{quote(str(item.get('name') or ''), safe='')}"
                ),
            }
            for item in payload.get("registered_models") or []
        ],
        "next_page_token": payload.get("next_page_token") or "",
    }


@mlflow_router.get("/models/{model_name}")
async def get_mlflow_model(
    model_name: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        model_payload = await client.get_registered_model(model_name)
        versions_payload = await client.search_model_versions(model_name)
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    model = normalize_model(model_payload.get("registered_model") or {})
    versions = []
    for version in versions_payload.get("model_versions") or []:
        run_id = str(version.get("run_id") or "")
        run_link = str(version.get("run_link") or "")
        experiment_id = (
            run_link.split("/experiments/", 1)[1].split("/", 1)[0]
            if "/experiments/" in run_link
            else ""
        )
        versions.append(
            {
                **version,
                "devcloud_run_url": f"/runs/{quote(run_id, safe='')}" if run_id else "",
                "mlflow_run_url": (
                    _run_url(client, experiment_id, run_id)
                    if run_id and experiment_id
                    else run_link
                ),
            }
        )
    model["versions"] = versions
    model["mlflow_url"] = _mlflow_url(
        client, f"models/{quote(model_name, safe='')}"
    )
    return model


@mlflow_router.get("/experiments")
async def list_mlflow_experiments(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page_token: str = Query(default="", max_length=4096),
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.search_experiments(page_token=page_token)
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    experiments = []
    for item in payload.get("experiments") or []:
        experiment = normalize_experiment(item)
        experiment["mlflow_url"] = _experiment_url(
            client, str(experiment.get("experiment_id") or "")
        )
        experiments.append(experiment)
    return {
        "experiments": experiments,
        "next_page_token": payload.get("next_page_token") or "",
    }


@mlflow_router.get("/experiments/{experiment_id}")
async def get_mlflow_experiment(
    experiment_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.get_experiment(experiment_id)
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    experiment = normalize_experiment(payload.get("experiment") or {})
    experiment["mlflow_url"] = _experiment_url(client, experiment_id)
    return experiment


@mlflow_router.get("/runs")
async def list_mlflow_runs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    experiment_id: str = Query(..., min_length=1, max_length=256),
    filter_string: str = Query(default="", max_length=2000),
    page_token: str = Query(default="", max_length=4096),
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.search_runs(
            [experiment_id],
            filter_string=filter_string,
            page_token=page_token,
        )
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    runs = []
    for item in payload.get("runs") or []:
        run = normalize_run(item)
        run["mlflow_url"] = _run_url(client, run["experiment_id"], run["run_id"])
        runs.append(run)
    return {"runs": runs, "next_page_token": payload.get("next_page_token") or ""}


@mlflow_router.get("/runs/compare")
async def compare_mlflow_runs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    run_ids: Annotated[list[str], Query(min_length=2, max_length=10)],
):
    client = await get_mlflow_client(db, current_user.id)
    unique_ids = list(dict.fromkeys(run_ids))
    if len(unique_ids) < 2 or len(unique_ids) > 10:
        raise HTTPException(status_code=422, detail="Karşılaştırmak için 2-10 farklı run seçin.")
    try:
        payloads = await asyncio.gather(*(client.get_run(run_id) for run_id in unique_ids))
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    runs = []
    for payload in payloads:
        run = normalize_run(payload.get("run") or {})
        run["mlflow_url"] = _run_url(client, run["experiment_id"], run["run_id"])
        runs.append(run)
    return {"runs": runs}


@mlflow_router.get("/runs/{run_id}")
async def get_mlflow_run(
    run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        run_payload, artifacts_payload, versions_payload = await asyncio.gather(
            client.get_run(run_id),
            client.list_artifacts(run_id),
            client.search_model_versions(max_results=1000),
        )
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    run = normalize_run(run_payload.get("run") or {})
    run["mlflow_url"] = _run_url(client, run["experiment_id"], run["run_id"])
    run["artifacts"] = [
        {
            **artifact,
            "mlflow_url": (
                f"{run['mlflow_url']}/artifacts/"
                f"{quote(str(artifact.get('path') or ''), safe='/')}"
            ),
        }
        for artifact in artifacts_payload.get("files") or []
    ]
    run["registered_model_versions"] = [
        {
            **version,
            "mlflow_url": _mlflow_url(
                client, f"models/{quote(str(version.get('name') or ''), safe='')}"
            ),
        }
        for version in versions_payload.get("model_versions") or []
        if str(version.get("run_id") or "") == run_id
    ]
    return run


@mlflow_router.get("/runs/{run_id}/artifacts")
async def list_mlflow_artifacts(
    run_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    path: str = Query(default="", max_length=2000),
    page_token: str = Query(default="", max_length=4096),
):
    client = await get_mlflow_client(db, current_user.id)
    try:
        payload = await client.list_artifacts(run_id, path=path, page_token=page_token)
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "files": payload.get("files") or [],
        "next_page_token": payload.get("next_page_token") or "",
    }

