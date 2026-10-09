"""Admin API: cross-user workspace listing and system statistics."""

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceStatus
from app.schemas.workspace import WorkspaceOut

router = APIRouter()


@router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_all_workspaces(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: List all workspaces across all users."""
    stmt = select(Workspace).order_by(Workspace.created_at.desc())
    result = await db.execute(stmt)
    return [WorkspaceOut.model_validate(ws) for ws in result.scalars().all()]


@router.post("/workspaces/{workspace_id}/migrate")
async def migrate_workspace(
    workspace_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    target_node_id: str | None = None,
):
    """Reject unsafe metadata-only migration until data transfer is implemented."""
    raise HTTPException(
        status_code=501,
        detail=(
            "Workerlar arası workspace taşıma henüz desteklenmiyor. node_id "
            "değiştirmek kalıcı veriyi taşımaz; worker'ı drain durumunda tutun "
            "ve operator kontrollü yedek/geri yükleme süreci kullanın."
        ),
    )


@router.get("/stats")
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
        "runtime_mode": "worker-only",
    }
