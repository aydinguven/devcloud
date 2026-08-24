from typing import Annotated
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.podman_service import podman_service
from app.schemas.user import UserOut
from app.schemas.workspace import WorkspaceOut

admin_router = APIRouter(prefix="/api/admin", tags=["Admin"])


@admin_router.get("/users", response_model=list[UserOut])
async def list_all_users(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: List all registered users."""
    stmt = select(User).order_by(User.id.asc())
    result = await db.execute(stmt)
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@admin_router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_all_workspaces(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: List all workspaces across all users."""
    stmt = select(Workspace).order_by(Workspace.created_at.desc())
    result = await db.execute(stmt)
    return [WorkspaceOut.model_validate(ws) for ws in result.scalars().all()]


@admin_router.get("/stats")
async def get_system_stats(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Summary statistics of system and containers."""
    total_users = (await db.execute(select(func.count(User.id)))).scalar_one()
    total_workspaces = (await db.execute(select(func.count(Workspace.id)))).scalar_one()
    running_workspaces = (
        await db.execute(
            select(func.count(Workspace.id)).where(Workspace.status == WorkspaceStatus.RUNNING)
        )
    ).scalar_one()

    return {
        "total_users": total_users,
        "total_workspaces": total_workspaces,
        "running_workspaces": running_workspaces,
        "podman_mode": "mock" if podman_service.is_mock else "native",
    }
