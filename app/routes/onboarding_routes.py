import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update as sql_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user, get_current_user
from app.database import get_db
from app.models.mlflow_server_settings import MlflowServerSettings
from app.models.onboarding import OnboardingProgress, OnboardingSettings
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from app.schemas.onboarding import (
    OnboardingSettingsOut,
    OnboardingSettingsUpdate,
    OnboardingRestartRequest,
    OnboardingStateOut,
    OnboardingStateUpdate,
)
from app.workspace_catalog import list_enabled_flavors, list_enabled_templates

onboarding_router = APIRouter(tags=["Onboarding"])

TOUR_TOPICS = {
    "workspace-create",
    "resource-usage",
    "workspace-detail",
    "mlflow",
    "profile",
    "admin",
}
TOUR_STATUSES = {"not_started", "in_progress", "paused", "completed"}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _choices(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(topic): str(choice)
        for topic, choice in parsed.items()
        if topic in TOUR_TOPICS and choice in {"show", "skip"}
    }


async def _settings(db: AsyncSession) -> OnboardingSettings | None:
    return await db.get(OnboardingSettings, 1)


async def _capabilities(
    db: AsyncSession, user: User
) -> tuple[dict[str, bool], dict[str, str | None]]:
    mlflow = await db.get(MlflowServerSettings, 1)
    can_create_workspace = bool(
        await list_enabled_templates(db) and await list_enabled_flavors(db)
    )
    workspace_id = (
        await db.execute(
            select(Workspace.id)
            .where(
                Workspace.user_id == user.id,
                Workspace.status != WorkspaceStatus.DELETED,
                Workspace.template_id != "mlflow-serving",
            )
            .order_by(Workspace.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    features = {
        "workspace-create": can_create_workspace,
        "resource-usage": True,
        "workspace-detail": workspace_id is not None,
        "mlflow": bool(mlflow and mlflow.enabled),
        "profile": True,
        "admin": user.role == UserRole.ADMIN,
    }
    return features, {
        "first_workspace_url": f"/workspaces/{workspace_id}" if workspace_id else None
    }


async def _state_out(
    db: AsyncSession,
    user: User,
    settings_record: OnboardingSettings | None = None,
    progress: OnboardingProgress | None = None,
) -> OnboardingStateOut:
    settings_record = settings_record or await _settings(db)
    enabled = bool(settings_record and settings_record.enabled)
    version = settings_record.current_version if settings_record else 1
    if progress is None:
        progress = await db.get(OnboardingProgress, user.id)

    fresh = progress is None or progress.tour_version != version
    status_value = "not_started" if fresh else progress.status
    if status_value not in TOUR_STATUSES:
        status_value = "not_started"
    topic_choices = {} if fresh else _choices(progress.topic_choices_json)
    current_topic = "" if fresh else progress.current_topic
    current_step = "" if fresh else progress.current_step
    revision = progress.revision if progress else 0

    enabled_at = _utc(settings_record.enabled_at) if settings_record else None
    created_at = _utc(user.created_at)
    is_new_user = bool(enabled_at and created_at and created_at >= enabled_at)
    auto_offer = enabled and (is_new_user or progress is not None)
    features, context = await _capabilities(db, user)
    return OnboardingStateOut(
        enabled=enabled,
        auto_offer=auto_offer,
        tour_version=version,
        status=status_value,
        current_topic=current_topic,
        current_step=current_step,
        topic_choices=topic_choices,
        revision=revision,
        features=features,
        context=context,
    )


@onboarding_router.get(
    "/api/admin/onboarding-settings", response_model=OnboardingSettingsOut
)
async def get_onboarding_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await _settings(db)
    if record is None:
        return OnboardingSettingsOut(
            enabled=False,
            current_version=1,
            enabled_at=None,
            updated_at=None,
        )
    return OnboardingSettingsOut.model_validate(record, from_attributes=True)


@onboarding_router.put(
    "/api/admin/onboarding-settings", response_model=OnboardingSettingsOut
)
async def update_onboarding_settings(
    payload: OnboardingSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    now = datetime.now(timezone.utc)
    record = (
        await db.execute(
            select(OnboardingSettings)
            .where(OnboardingSettings.id == 1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        record = OnboardingSettings(
            id=1,
            enabled=payload.enabled,
            current_version=payload.current_version,
            enabled_at=now if payload.enabled else None,
            updated_at=now,
        )
        db.add(record)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Tur ayarları başka bir oturumda güncellendi.",
            ) from exc
    else:
        if payload.current_version < record.current_version:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Tur sürümü azaltılamaz.",
            )
        if payload.enabled and not record.enabled:
            record.enabled_at = now
        record.enabled = payload.enabled
        record.current_version = payload.current_version
        record.updated_at = now
        await db.commit()
    await db.refresh(record)
    return OnboardingSettingsOut.model_validate(record, from_attributes=True)


@onboarding_router.get("/api/onboarding/state", response_model=OnboardingStateOut)
async def get_onboarding_state(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return await _state_out(db, user)


@onboarding_router.patch("/api/onboarding/state", response_model=OnboardingStateOut)
async def update_onboarding_state(
    payload: OnboardingStateUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_record = await _settings(db)
    if not settings_record or not settings_record.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tur kapalı.")
    if payload.tour_version != settings_record.current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tur sürümü değişti; durumu yenileyin.",
        )

    progress = await db.get(OnboardingProgress, user.id)
    current_revision = progress.revision if progress else 0
    if payload.expected_revision != current_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tur durumu başka bir oturumda güncellendi.",
        )

    fresh = progress is None or progress.tour_version != settings_record.current_version
    next_status = "not_started" if fresh else progress.status
    next_topic = "" if fresh else progress.current_topic
    next_step = "" if fresh else progress.current_step
    next_choices = {} if fresh else _choices(progress.topic_choices_json)
    completed_at = None if fresh else progress.completed_at

    features, _context = await _capabilities(db, user)
    if payload.current_topic is not None:
        if payload.current_topic and payload.current_topic not in TOUR_TOPICS:
            raise HTTPException(status_code=422, detail="Bilinmeyen tur konusu.")
        next_topic = payload.current_topic
    if payload.current_step is not None:
        next_step = payload.current_step
    if payload.status is not None:
        next_status = payload.status
        completed_at = (
            datetime.now(timezone.utc) if payload.status == "completed" else None
        )
    if payload.topic_choice is not None:
        topic_id = payload.topic_choice.topic_id
        if topic_id not in TOUR_TOPICS:
            raise HTTPException(status_code=422, detail="Bilinmeyen tur konusu.")
        if payload.topic_choice.choice == "show" and not features.get(topic_id, False):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bu tur konusu şu anda kullanılamıyor.",
            )
        next_choices[topic_id] = payload.topic_choice.choice
    if next_status == "completed":
        next_topic = ""
        next_step = ""

    now = datetime.now(timezone.utc)
    values = {
        "tour_version": settings_record.current_version,
        "status": next_status,
        "current_topic": next_topic,
        "current_step": next_step,
        "topic_choices_json": json.dumps(
            next_choices, ensure_ascii=False, sort_keys=True
        ),
        "completed_at": completed_at,
        "updated_at": now,
        "revision": current_revision + 1,
    }
    if progress is None:
        progress = OnboardingProgress(user_id=user.id, **values)
        db.add(progress)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Tur durumu başka bir oturumda oluşturuldu.",
            ) from exc
    else:
        result = await db.execute(
            sql_update(OnboardingProgress)
            .where(
                OnboardingProgress.user_id == user.id,
                OnboardingProgress.revision == payload.expected_revision,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Tur durumu başka bir oturumda güncellendi.",
            )
        await db.commit()
        db.expire(progress)
        await db.refresh(progress)
    return await _state_out(db, user, settings_record, progress)


@onboarding_router.post("/api/onboarding/restart", response_model=OnboardingStateOut)
async def restart_onboarding(
    payload: OnboardingRestartRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings_record = await _settings(db)
    if not settings_record or not settings_record.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tur kapalı.")
    if payload.tour_version != settings_record.current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tur sürümü değişti; durumu yenileyin.",
        )
    progress = await db.get(OnboardingProgress, user.id)
    current_revision = progress.revision if progress else 0
    if payload.expected_revision != current_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tur durumu başka bir oturumda güncellendi.",
        )
    values = {
        "tour_version": settings_record.current_version,
        "status": "not_started",
        "current_topic": "",
        "current_step": "",
        "topic_choices_json": "{}",
        "completed_at": None,
        "updated_at": datetime.now(timezone.utc),
        "revision": current_revision + 1,
    }
    if progress is None:
        progress = OnboardingProgress(user_id=user.id, **values)
        db.add(progress)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Tur durumu başka bir oturumda oluşturuldu.",
            ) from exc
    else:
        result = await db.execute(
            sql_update(OnboardingProgress)
            .where(
                OnboardingProgress.user_id == user.id,
                OnboardingProgress.revision == payload.expected_revision,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Tur durumu başka bir oturumda güncellendi.",
            )
        await db.commit()
        db.expire(progress)
        await db.refresh(progress)
    return await _state_out(db, user, settings_record, progress)
