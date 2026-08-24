from datetime import datetime, timezone
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.flavors import get_flavor, list_flavors
from app.orchestrator.templates import get_template, list_templates
from app.orchestrator.podman_service import podman_service, PodmanExecutionError
from app.schemas.workspace import (
    FlavorInfo,
    TemplateInfo,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceStatusOut,
)

logger = logging.getLogger("devcloud.routes.workspaces")
workspace_router = APIRouter(prefix="/api/workspaces", tags=["Workspaces"])


@workspace_router.get("/templates", response_model=list[TemplateInfo])
async def get_templates():
    """List available project environment templates."""
    return list_templates()


@workspace_router.get("/flavors", response_model=list[FlavorInfo])
async def get_flavors():
    """List available resource flavors."""
    return list_flavors()


@workspace_router.get("", response_model=list[WorkspaceOut])
async def list_user_workspaces(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all workspaces owned by the logged-in user."""
    stmt = (
        select(Workspace)
        .where(Workspace.user_id == current_user.id)
        .order_by(Workspace.created_at.desc())
    )
    result = await db.execute(stmt)
    workspaces = result.scalars().all()
    
    # Enrich with web_url for quick launching
    out = []
    for ws in workspaces:
        ws_out = WorkspaceOut.model_validate(ws)
        ws_out.web_url = f"/proxy/{ws.id}/"
        out.append(ws_out)
    return out


@workspace_router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    data: WorkspaceCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a new persistent workspace and deploy container."""
    template = get_template(data.template_id)
    if not template:
        raise HTTPException(status_code=400, detail=f"Invalid template ID: {data.template_id}")

    flavor = get_flavor(data.flavor_id)
    if not flavor:
        raise HTTPException(status_code=400, detail=f"Invalid flavor ID: {data.flavor_id}")

    # Fetch currently used ports from database
    stmt = select(Workspace.host_port).where(Workspace.status != WorkspaceStatus.DELETED)
    result = await db.execute(stmt)
    used_ports = set(result.scalars().all())

    # Find free port
    try:
        host_port = podman_service.find_free_port(used_ports)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Initialize workspace object
    workspace = Workspace(
        name=data.name.strip(),
        description=data.description.strip(),
        user_id=current_user.id,
        template_id=data.template_id,
        flavor_id=data.flavor_id,
        container_name=f"devcloud-{current_user.id}-{data.name.strip().lower().replace(' ', '-')[:10]}",
        host_port=host_port,
        container_port=template.default_port,
        storage_path="",  # will be set by orchestrator
        status=WorkspaceStatus.CREATING,
        created_at=datetime.now(timezone.utc),
    )
    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)

    # Ensure unique container name incorporating workspace ID
    workspace.container_name = f"devcloud-{current_user.id}-{workspace.id[:8]}"

    # Launch container via Podman
    try:
        container_id, storage_path = await podman_service.create_workspace_container(
            workspace_id=workspace.id,
            user_id=current_user.id,
            container_name=workspace.container_name,
            template_id=template.id,
            flavor_id=flavor.id,
            host_port=host_port,
            workspace_token=workspace.workspace_token,
        )
        workspace.container_id = container_id
        workspace.storage_path = storage_path
        workspace.status = WorkspaceStatus.RUNNING
        workspace.last_started_at = datetime.now(timezone.utc)
        workspace.error_message = None
    except Exception as exc:
        logger.error(f"Error launching workspace {workspace.id}: {exc}")
        workspace.status = WorkspaceStatus.ERROR
        workspace.error_message = str(exc)

    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)

    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"
    return ws_out


@workspace_router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace_detail(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get single workspace details."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")

    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"
    return ws_out


@workspace_router.post("/{workspace_id}/start", response_model=WorkspaceOut)
async def start_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Start a stopped workspace container."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")

    if workspace.status == WorkspaceStatus.RUNNING:
        return WorkspaceOut.model_validate(workspace)

    success = await podman_service.start_container(workspace.container_name)
    if success:
        workspace.status = WorkspaceStatus.RUNNING
        workspace.last_started_at = datetime.now(timezone.utc)
        workspace.error_message = None
    else:
        workspace.status = WorkspaceStatus.ERROR
        workspace.error_message = "Failed to restart container."

    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    return WorkspaceOut.model_validate(workspace)


@workspace_router.post("/{workspace_id}/stop", response_model=WorkspaceOut)
async def stop_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Stop a running workspace container."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")

    await podman_service.stop_container(workspace.container_name)
    workspace.status = WorkspaceStatus.STOPPED
    workspace.last_stopped_at = datetime.now(timezone.utc)

    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    return WorkspaceOut.model_validate(workspace)


@workspace_router.delete("/{workspace_id}")
async def delete_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a workspace container and mark workspace as removed."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")

    await podman_service.delete_container(workspace.container_name)
    
    await db.delete(workspace)
    await db.commit()
    return {"message": f"Workspace {workspace_id} deleted successfully."}


@workspace_router.get("/{workspace_id}/logs")
async def get_workspace_logs_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tail: int = 100,
):
    """Retrieve logs from the workspace container."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")

    logs = await podman_service.get_logs(workspace.container_name, tail=tail)
    return {"workspace_id": workspace_id, "logs": logs}
