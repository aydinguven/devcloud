from datetime import datetime, timezone
import logging
import uuid
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
        host_port = await podman_service.find_available_port(used_ports)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Generate the final identity before the first commit so concurrent
    # lifecycle requests always see the real Podman container name.
    workspace_id = str(uuid.uuid4())
    workspace = Workspace(
        id=workspace_id,
        name=data.name.strip(),
        description=data.description.strip(),
        user_id=current_user.id,
        template_id=data.template_id,
        flavor_id=data.flavor_id,
        container_name=f"devcloud-{current_user.id}-{workspace_id[:8]}",
        host_port=host_port,
        container_port=template.default_port,
        storage_path="",  # will be set by orchestrator
        status=WorkspaceStatus.CREATING,
        created_at=datetime.now(timezone.utc),
    )
    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)


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


@workspace_router.post("/deploy-stream")
async def deploy_workspace_stream(
    data: WorkspaceCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create and deploy a workspace container with real-time SSE log streaming."""
    import json
    import asyncio
    from fastapi.responses import StreamingResponse

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit_log(text: str, level: str = "info"):
        payload = json.dumps({"type": "log", "level": level, "text": text})
        await queue.put(f"data: {payload}\n\n")

    async def emit_error(text: str):
        payload = json.dumps({"type": "error", "text": text})
        await queue.put(f"data: {payload}\n\n")

    async def emit_done(workspace_id: str, web_url: str):
        payload = json.dumps({"type": "done", "workspace_id": workspace_id, "web_url": web_url})
        await queue.put(f"data: {payload}\n\n")

    async def run_deployment():
        try:
            await emit_log(f"🚀 Initializing deployment pipeline for '{data.name}'...", "info")
            await asyncio.sleep(0.05)

            template = get_template(data.template_id)
            if not template:
                await emit_error(f"Invalid template: {data.template_id}")
                return

            flavor = get_flavor(data.flavor_id)
            if not flavor:
                await emit_error(f"Invalid flavor: {data.flavor_id}")
                return

            await emit_log(f"📋 Template: {template.name} ({template.image_tag})", "info")
            await emit_log(f"⚡ Quota: {flavor.cpus} CPU(s), {flavor.memory_display} RAM ({flavor.name})", "info")

            # Host port selection
            stmt = select(Workspace.host_port).where(Workspace.status != WorkspaceStatus.DELETED)
            result = await db.execute(stmt)
            used_ports = set(result.scalars().all())

            try:
                host_port = await podman_service.find_available_port(used_ports)
                await emit_log(f"🔌 Allocated host port: {host_port}", "info")
            except RuntimeError as e:
                await emit_error(f"Port allocation failed: {str(e)}")
                return

            # Generate the final identity before the first commit so delete,
            # logs, and deployment all address the same container.
            workspace_id = str(uuid.uuid4())
            workspace = Workspace(
                id=workspace_id,
                name=data.name.strip(),
                description=data.description.strip(),
                user_id=current_user.id,
                template_id=data.template_id,
                flavor_id=data.flavor_id,
                container_name=f"devcloud-{current_user.id}-{workspace_id[:8]}",
                host_port=host_port,
                container_port=template.default_port,
                storage_path="",
                status=WorkspaceStatus.CREATING,
                created_at=datetime.now(timezone.utc),
            )
            db.add(workspace)
            await db.commit()
            await db.refresh(workspace)

            await emit_log(f"💾 Initialized persistent volume for User #{current_user.id}", "info")

            # Launch container with progress callback
            container_id, storage_path = await podman_service.create_workspace_container(
                workspace_id=workspace.id,
                user_id=current_user.id,
                container_name=workspace.container_name,
                template_id=template.id,
                flavor_id=flavor.id,
                host_port=host_port,
                workspace_token=workspace.workspace_token,
                progress_callback=emit_log,
            )

            workspace.container_id = container_id
            workspace.storage_path = storage_path
            workspace.status = WorkspaceStatus.RUNNING
            workspace.last_started_at = datetime.now(timezone.utc)
            workspace.error_message = None

            db.add(workspace)
            await db.commit()
            await db.refresh(workspace)

            await emit_log(f"📂 Persistent directory: {storage_path}", "success")
            await emit_done(workspace.id, f"/proxy/{workspace.id}/")

        except Exception as exc:
            logger.error(f"Error launching workspace: {exc}")
            if "workspace" in locals():
                workspace.status = WorkspaceStatus.ERROR
                workspace.error_message = str(exc)
                db.add(workspace)
                await db.commit()
            await emit_error(f"Deployment error: {str(exc)}")
        finally:
            await queue.put(None)

    async def stream_generator():
        task = asyncio.create_task(run_deployment())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
        await task

    return StreamingResponse(stream_generator(), media_type="text/event-stream")


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
    """Resume / Start a stopped workspace container."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")

    try:
        container_exists = await podman_service.container_exists(workspace.container_name)
        if workspace.status == WorkspaceStatus.RUNNING and container_exists:
            ws_out = WorkspaceOut.model_validate(workspace)
            ws_out.web_url = f"/proxy/{workspace.id}/"
            return ws_out

        if container_exists:
            success = await podman_service.start_container(workspace.container_name)
        else:
            logger.info(
                "Recreating missing container %s with persistent storage %s",
                workspace.container_name,
                workspace.storage_path,
            )
            container_id, storage_path = await podman_service.create_workspace_container(
                workspace_id=workspace.id,
                user_id=workspace.user_id,
                container_name=workspace.container_name,
                template_id=workspace.template_id,
                flavor_id=workspace.flavor_id,
                host_port=workspace.host_port,
                workspace_token=workspace.workspace_token,
            )
            workspace.container_id = container_id
            workspace.storage_path = storage_path
            success = True
    except (PodmanExecutionError, ValueError, RuntimeError) as exc:
        logger.exception("Failed to start workspace %s", workspace.id)
        workspace.error_message = str(exc)
        success = False

    if success:
        workspace.status = WorkspaceStatus.RUNNING
        workspace.last_started_at = datetime.now(timezone.utc)
        workspace.error_message = None
    else:
        workspace.status = WorkspaceStatus.ERROR
        workspace.error_message = workspace.error_message or "Failed to restart container."

    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"
    return ws_out


@workspace_router.post("/{workspace_id}/stop", response_model=WorkspaceOut)
async def stop_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Pause / Stop a running workspace container (preserves all data to resume later)."""
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
    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"
    return ws_out


@workspace_router.delete("/{workspace_id}")
async def delete_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a workspace container, remove its persistent storage, and delete from DB."""
    import os
    import shutil

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")
    if workspace.status == WorkspaceStatus.CREATING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace deployment is still in progress. Wait for it to finish before deleting it.",
        )

    # 1. Stop and remove container in Podman
    await podman_service.delete_container(workspace.container_name)

    # 2. Permanently remove persistent storage directory on disk
    if workspace.storage_path and os.path.exists(workspace.storage_path):
        try:
            shutil.rmtree(workspace.storage_path, ignore_errors=True)
            logger.info(f"Deleted persistent storage folder at: {workspace.storage_path}")
        except Exception as err:
            logger.warning(f"Error removing storage folder {workspace.storage_path}: {err}")

    # 3. Remove record from database
    await db.delete(workspace)
    await db.commit()
    return {"message": f"Workspace {workspace_id} and its persistent storage were deleted successfully."}


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
