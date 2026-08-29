from typing import Annotated

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
    normalize_model,
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
        model_count=count,
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
        "models": [normalize_model(item) for item in payload.get("registered_models") or []],
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
    model["versions"] = versions_payload.get("model_versions") or []
    return model

