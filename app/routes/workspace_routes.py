import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import shutil
import uuid
from typing import Annotated
from fastapi import APIRouter, Depends, Form, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings
from app.database import get_db
from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.flavors import Flavor, get_flavor, list_flavors
from app.orchestrator.templates import get_template, list_templates, resolve_template
from app.orchestrator.podman_service import podman_service, PodmanExecutionError
from app.orchestrator.runtime_backend import runtime_for_node
from app.orchestrator.scheduler import NoSchedulableNode, select_worker_node
from app.resource_usage import get_system_usage, get_user_usage, quota_violations
from app.schemas.workspace import (
    FlavorInfo,
    TemplateInfo,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceStatusOut,
)

logger = logging.getLogger("devcloud.routes.workspaces")
workspace_router = APIRouter(prefix="/api/workspaces", tags=["Workspaces"])


async def allocate_workspace_port(db: AsyncSession, remote: bool) -> int:
    """Allocate a globally unique port; remote workers validate it again locally."""
    stmt = select(Workspace.host_port).where(Workspace.status != WorkspaceStatus.DELETED)
    used_ports = set((await db.execute(stmt)).scalars().all())
    if not remote:
        return await podman_service.find_available_port(used_ports)
    for port in range(settings.PORT_RANGE_START, settings.PORT_RANGE_END + 1):
        if port not in used_ports:
            return port
    raise RuntimeError("Workspace port aralığında boş port kalmadı.")


async def get_quota_error(
    db: AsyncSession,
    user: User,
    flavor: Flavor,
) -> str | None:
    """Return a readable quota error for a proposed workspace allocation."""
    result = await db.execute(
        select(Workspace).where(Workspace.user_id == user.id)
    )
    workspaces = result.scalars().all()
    violations = await asyncio.to_thread(
        quota_violations, user, workspaces, flavor
    )
    if not violations:
        return None
    return "Kullanıcı kotası aşıldı: " + "; ".join(violations) + "."


@workspace_router.get("/templates", response_model=list[TemplateInfo])
async def get_templates():
    """List available project environment templates."""
    return list_templates()


@workspace_router.get("/flavors", response_model=list[FlavorInfo])
async def get_flavors():
    """List available resource flavors."""
    return list_flavors()


@workspace_router.get("/usage")
async def get_resource_usage(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return host usage plus the current user's allocation and quota."""
    result = await db.execute(
        select(Workspace).where(Workspace.user_id == current_user.id)
    )
    workspaces = result.scalars().all()
    system_usage, user_usage = await asyncio.gather(
        asyncio.to_thread(get_system_usage),
        asyncio.to_thread(get_user_usage, current_user, workspaces),
    )
    return {
        "system": system_usage,
        "user": user_usage,
    }


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
    template = await resolve_template(db, data.template_id)
    if not template:
        raise HTTPException(status_code=400, detail=f"Geçersiz şablon ID: {data.template_id}")

    flavor = get_flavor(data.flavor_id)
    if not flavor or not flavor.selectable:
        raise HTTPException(status_code=400, detail=f"Geçersiz kaynak profili ID: {data.flavor_id}")

    quota_error = await get_quota_error(db, current_user, flavor)
    if quota_error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=quota_error)

    try:
        node = await select_worker_node(db, flavor)
        host_port = await allocate_workspace_port(db, remote=node is not None)
    except (NoSchedulableNode, RuntimeError) as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Generate the final identity before the first commit so concurrent
    # lifecycle requests always see the real Podman container name.
    workspace_id = str(uuid.uuid4())
    workspace = Workspace(
        id=workspace_id,
        name=data.name.strip(),
        description=data.description.strip(),
        user_id=current_user.id,
        node_id=node.id if node else None,
        template_id=data.template_id,
        flavor_id=data.flavor_id,
        container_name=f"devcloud-{current_user.id}-{workspace_id[:8]}",
        host_port=host_port,
        container_port=template.default_port,
        storage_path="",  # will be set by orchestrator
        status=WorkspaceStatus.CREATING,
        auto_stop_minutes=data.auto_stop_minutes,
        created_at=datetime.now(timezone.utc),
    )
    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)


    # Launch container via Podman
    try:
        runtime = runtime_for_node(workspace.node_id)
        container_id, storage_path = await runtime.create_workspace_container(
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
    from fastapi.responses import StreamingResponse

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit_log(text: str, level: str = "info"):
        payload = json.dumps({"type": "log", "level": level, "text": text}, ensure_ascii=False)
        await queue.put(f"data: {payload}\n\n")

    async def emit_error(text: str):
        payload = json.dumps({"type": "error", "text": text}, ensure_ascii=False)
        await queue.put(f"data: {payload}\n\n")

    async def emit_done(workspace_id: str, web_url: str):
        payload = json.dumps({"type": "done", "workspace_id": workspace_id, "web_url": web_url})
        await queue.put(f"data: {payload}\n\n")

    async def run_deployment():
        try:
            await emit_log(f"'{data.name}' için kurulum süreci başlatılıyor...", "info")
            await asyncio.sleep(0.05)

            template = await resolve_template(db, data.template_id)
            if not template:
                await emit_error(f"Geçersiz şablon: {data.template_id}")
                return

            flavor = get_flavor(data.flavor_id)
            if not flavor or not flavor.selectable:
                await emit_error(f"Geçersiz kaynak profili: {data.flavor_id}")
                return

            quota_error = await get_quota_error(db, current_user, flavor)
            if quota_error:
                await emit_error(quota_error)
                return

            await emit_log(f"Şablon: {template.name} ({template.image_tag})", "info")
            await emit_log(f"Çalışma alanı kaynağı: {flavor.cpus} CPU, {flavor.memory_display} RAM ({flavor.name})", "info")

            try:
                node = await select_worker_node(db, flavor)
                host_port = await allocate_workspace_port(db, remote=node is not None)
                placement = node.name if node else "yerel runtime"
                await emit_log(f"Worker seçildi: {placement}; host portu ayrıldı: {host_port}", "info")
            except (NoSchedulableNode, RuntimeError) as e:
                await emit_error(f"Port ayrılamadı: {str(e)}")
                return

            # Generate the final identity before the first commit so delete,
            # logs, and deployment all address the same container.
            workspace_id = str(uuid.uuid4())
            workspace = Workspace(
                id=workspace_id,
                name=data.name.strip(),
                description=data.description.strip(),
                user_id=current_user.id,
                node_id=node.id if node else None,
                template_id=data.template_id,
                flavor_id=data.flavor_id,
                container_name=f"devcloud-{current_user.id}-{workspace_id[:8]}",
                host_port=host_port,
                container_port=template.default_port,
                storage_path="",
                status=WorkspaceStatus.CREATING,
                auto_stop_minutes=data.auto_stop_minutes,
                created_at=datetime.now(timezone.utc),
            )
            db.add(workspace)
            await db.commit()
            await db.refresh(workspace)

            await emit_log(f"Kullanıcı #{current_user.id} için kalıcı volume hazırlandı", "info")

            # Launch container with progress callback
            runtime = runtime_for_node(workspace.node_id)
            container_id, storage_path = await runtime.create_workspace_container(
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

            await emit_log(f"Kalıcı dizin: {storage_path}", "success")
            await emit_done(workspace.id, f"/proxy/{workspace.id}/")

        except Exception as exc:
            logger.error(f"Error launching workspace: {exc}")
            if "workspace" in locals():
                workspace.status = WorkspaceStatus.ERROR
                workspace.error_message = str(exc)
                db.add(workspace)
                await db.commit()
            await emit_error(f"Kurulum hatası: {str(exc)}")
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
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")

    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")

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
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")

    try:
        runtime = runtime_for_node(workspace.node_id)
        container_exists = await runtime.container_exists(workspace.container_name)
        if workspace.status == WorkspaceStatus.RUNNING and container_exists:
            ws_out = WorkspaceOut.model_validate(workspace)
            ws_out.web_url = f"/proxy/{workspace.id}/"
            return ws_out

        if container_exists:
            success = await runtime.start_container(workspace.container_name)
        else:
            logger.info(
                "Recreating missing container %s with persistent storage %s",
                workspace.container_name,
                workspace.storage_path,
            )
            container_id, storage_path = await runtime.create_workspace_container(
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
        workspace.error_message = workspace.error_message or "Container yeniden başlatılamadı."

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
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")

    runtime = runtime_for_node(workspace.node_id)
    await runtime.stop_container(workspace.container_name)
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
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")
    if workspace.status == WorkspaceStatus.CREATING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Çalışma alanı kurulumu devam ediyor. Silmeden önce tamamlanmasını bekleyin.",
        )

    # 1. Stop and remove container in Podman
    runtime = runtime_for_node(workspace.node_id)
    await runtime.delete_container(workspace.container_name, workspace.storage_path)

    # 2. Permanently remove persistent storage directory on disk
    if not workspace.node_id and workspace.storage_path and os.path.exists(workspace.storage_path):
        try:
            shutil.rmtree(workspace.storage_path, ignore_errors=True)
            logger.info(f"Deleted persistent storage folder at: {workspace.storage_path}")
        except Exception as err:
            logger.warning(f"Error removing storage folder {workspace.storage_path}: {err}")

    # 3. Remove record from database
    await db.delete(workspace)
    await db.commit()
    return {"message": f"Çalışma alanı {workspace_id} ve kalıcı depolaması silindi."}


@workspace_router.get("/{workspace_id}/logs")
async def get_workspace_logs(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tail: int = 100,
):
    """Retrieve logs from container."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")

    runtime = runtime_for_node(workspace.node_id)
    logs = await runtime.get_logs(workspace.container_name, tail=tail)
    return {"workspace_id": workspace_id, "logs": logs}


@workspace_router.get("/stats/summary")
async def get_workspaces_stats_summary(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Retrieve live CPU, RAM, Disk, and uptime stats for all workspaces owned by the user."""
    from app.orchestrator.metrics_service import get_workspace_live_metrics

    stmt = (
        select(Workspace)
        .where(Workspace.user_id == current_user.id, Workspace.status != WorkspaceStatus.DELETED)
    )
    res = await db.execute(stmt)
    workspaces = res.scalars().all()

    metrics_list = []
    for ws in workspaces:
        m = await get_workspace_live_metrics(ws)
        metrics_list.append(m)

    return {"stats": metrics_list}


@workspace_router.get("/{workspace_id}/stats")
async def get_single_workspace_stats(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Retrieve live CPU, RAM, Disk, and uptime stats for a single workspace."""
    from app.orchestrator.metrics_service import get_workspace_live_metrics

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    res = await db.execute(stmt)
    workspace = res.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")

    return await get_workspace_live_metrics(workspace)


@workspace_router.get("/{workspace_id}/backup/download")
async def download_workspace_backup(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Download a full .zip backup archive of the workspace persistent directory."""
    import tempfile
    from fastapi.responses import FileResponse
    from app.orchestrator.backup_service import create_workspace_zip_backup

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    res = await db.execute(stmt)
    workspace = res.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")
    if workspace.node_id:
        raise HTTPException(
            status_code=501,
            detail="Remote worker backup streaming henüz etkin değil.",
        )
    if not workspace.storage_path or not os.path.exists(workspace.storage_path):
        raise HTTPException(status_code=404, detail="Storage path does not exist.")

    tmp_zip = Path(tempfile.gettempdir()) / f"devcloud_backup_{workspace.name}_{workspace.id[:8]}.zip"
    create_workspace_zip_backup(workspace.storage_path, tmp_zip)

    return FileResponse(
        path=str(tmp_zip),
        filename=f"{workspace.name}-backup.zip",
        media_type="application/zip",
    )


@workspace_router.post("/{workspace_id}/snapshot")
async def snapshot_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    template_name: Annotated[str, Form()],
    template_description: Annotated[str, Form()] = "",
):
    """Snapshot a running/stopped container into a reusable custom template."""
    import re
    from app.models.custom_template import CustomTemplate
    from app.orchestrator.backup_service import snapshot_workspace_to_image

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    res = await db.execute(stmt)
    workspace = res.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")
    if workspace.node_id:
        raise HTTPException(
            status_code=501,
            detail="Worker snapshot'ları merkezi image registry tamamlandıktan sonra etkinleştirilecek.",
        )

    slug = re.sub(r"[^a-zA-Z0-9_\-]", "", template_name.lower().replace(" ", "-"))[:30]
    template_id = f"custom-{slug}"

    success, image_tag_or_err = await snapshot_workspace_to_image(workspace, slug)
    if not success:
        raise HTTPException(status_code=500, detail=image_tag_or_err)

    custom_tpl = CustomTemplate(
        id=template_id,
        name=template_name.strip(),
        description=template_description.strip() or f"Snapshotted from {workspace.name}",
        category="Özel",
        icon="cube",
        image_tag=image_tag_or_err,
        default_port=workspace.container_port,
        ide_type="jupyter" if "jupyter" in workspace.template_id else "vscode",
        is_ready=True,
    )
    db.add(custom_tpl)
    await db.commit()

    return {
        "message": f"Successfully created template '{template_name}' from workspace.",
        "template_id": template_id,
        "template_name": template_name,
        "image_tag": image_tag_or_err,
    }
