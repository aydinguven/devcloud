from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.integrations.mlflow import (
    MlflowClient,
    MlflowConfigurationError,
    MlflowConnectionError,
    config_from_record,
    normalize_model,
    validate_config,
)
from app.models.mlflow_settings import MlflowSettings
from app.models.user import User

mlflow_router = APIRouter(prefix="/api/mlflow", tags=["MLflow"])


async def get_mlflow_client(db: AsyncSession) -> MlflowClient:
    record = await db.get(MlflowSettings, 1)
    if not record:
        raise HTTPException(status_code=503, detail="MLflow entegrasyonu yapılandırılmadı.")
    try:
        config = config_from_record(record)
        validate_config(config, require_enabled=True)
        return MlflowClient(config)
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@mlflow_router.get("/models")
async def list_mlflow_models(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    search: str = Query(default="", max_length=200),
    page_token: str = Query(default="", max_length=4096),
):
    client = await get_mlflow_client(db)
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
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    client = await get_mlflow_client(db)
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

