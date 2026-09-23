import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import shutil
import uuid
from typing import Annotated
from urllib.parse import quote
from fastapi import APIRouter, Depends, Form, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings
from app.database import get_db
from app.models.user import User, UserRole
from app.models.node import Node
from app.models.workspace import (
    Workspace,
    WorkspaceStatus,
    consumes_compute,
    normalize_workspace_name,
    workspace_name_slug,
)
from app.models.workspace_image import WorkspaceImage
from app.models.mlflow_settings import MlflowSettings
from app.models.mlflow_server_settings import MlflowServerSettings
from app.models.mlflow_deployment import MlflowDeployment, MlflowDeploymentStatus
from app.integrations.mlflow import (
    MlflowConfigurationError,
    config_from_record as mlflow_config_from_record,
    validate_config as validate_mlflow_config,
)
from app.mlflow_workspace import environment_from_config
from app.security.secrets import SecretDecryptionError
from app.orchestrator.flavors import Flavor, get_flavor
from app.orchestrator.templates import get_template, resolve_template
from app.orchestrator.runtime_backend import runtime_for_node
from app.orchestrator.admission import admission_transaction
from app.orchestrator.scheduler import (
    accelerator_availability_details,
    validate_restart_capacity,
    flavor_availability,
    NoSchedulableNode,
    WorkspacePlacement,
    select_workspace_placement,
)
from app.resource_usage import get_cluster_usage, get_user_usage, quota_violations
from app.orchestrator.metrics_service import get_workspace_disk_usage_by_user
from app.schemas.workspace import (
    FlavorInfo,
    TemplateInfo,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceStatusOut,
)
from app.workspace_catalog import (
    flavor_enabled,
    list_enabled_flavors,
    list_enabled_templates,
    resolve_flavor,
    template_enabled,
)


def _template_definition(template) -> dict:
    return {
        "template_id": template.id,
        "name": template.name,
        "description": template.description,
        "category": template.category,
        "image_tag": template.image_tag,
        "default_port": template.default_port,
        "ide_type": template.ide_type,
        "icon": template.icon,
        "mount_workspace": template.mount_workspace,
        "health_path": template.health_path,
        "require_ready": template.require_ready,
    }


def _flavor_definition(flavor: Flavor) -> dict:
    return {
        "id": flavor.id,
        "name": flavor.name,
        "display_name": flavor.display_name,
        "description": flavor.description,
        "cpus": flavor.cpus,
        "memory_mb": flavor.memory_mb,
        "memory_display": flavor.memory_display,
        "accelerator_count": flavor.accelerator_count,
        "accelerator_vendor": flavor.accelerator_vendor,
        "accelerator_memory_mb": flavor.accelerator_memory_mb,
        "accelerator_display": flavor.accelerator_display,
        "selectable": flavor.selectable,
    }


async def _workspace_mlflow_environment(
    db: AsyncSession,
    user_id: int,
) -> dict[str, str]:
    server = await db.get(MlflowServerSettings, 1)
    if not server or not server.enabled:
        return {}
    credentials = (
        await db.execute(
            select(MlflowSettings).where(MlflowSettings.user_id == user_id)
        )
    ).scalar_one_or_none()
    if not credentials or not credentials.enabled:
        return {}
    try:
        config = mlflow_config_from_record(credentials, server)
        validate_mlflow_config(config, require_enabled=True)
    except (MlflowConfigurationError, SecretDecryptionError) as exc:
        logger.warning("Skipping invalid MLflow workspace settings for user %s: %s", user_id, exc)
        return {}
    return environment_from_config(config)


async def _workspace_service_environment(
    db: AsyncSession,
    workspace: Workspace,
) -> dict[str, str]:
    if workspace.template_id != "mlflow-serving":
        return {}
    deployment = (
        await db.execute(
            select(MlflowDeployment).where(
                MlflowDeployment.workspace_id == workspace.id
            )
        )
    ).scalar_one_or_none()
    workers = deployment.gunicorn_workers if deployment else 1
    return {
        "DISABLE_NGINX": "false",
        "GUNICORN_CMD_ARGS": f"--workers={workers}",
    }

async def _sync_mlflow_deployment_status(
    db: AsyncSession,
    workspace: Workspace,
    status,
    message: str,
    error: str | None = None,
) -> None:
    if workspace.template_id != "mlflow-serving":
        return
    deployment = (
        await db.execute(
            select(MlflowDeployment).where(
                MlflowDeployment.workspace_id == workspace.id
            )
        )
    ).scalar_one_or_none()
    if deployment:
        deployment.status = status
        deployment.status_message = message
        deployment.error_message = error
        db.add(deployment)


logger = logging.getLogger("devcloud.routes.workspaces")
workspace_router = APIRouter(prefix="/api/workspaces", tags=["Workspaces"])


class QuotaExceeded(RuntimeError):
    pass


def _snapshot_ide_type(template_id: str) -> str:
    """Carry the source workspace's interface over to its snapshot template."""
    source = get_template(template_id)
    if source:
        return source.ide_type
    if "jupyter" in template_id:
        return "jupyter"
    if "terminal" in template_id:
        return "terminal"
    return "vscode"


class WorkspaceNameConflict(RuntimeError):
    """The owner already has a workspace with the requested name."""


WORKSPACE_NAME_CONFLICT_DETAIL = "Bu adla bir çalışma alanınız zaten var."


def _is_workspace_name_conflict(exc: IntegrityError) -> bool:
    """Tell the per-owner name constraint apart from port/GPU slot races.

    PostgreSQL names the violated index, SQLite names the columns, so both
    spellings are matched.
    """
    message = str(getattr(exc, "orig", exc)).lower()
    return "uq_workspaces_user_name" in message or "name_key" in message


async def available_workspace_name(
    db: AsyncSession,
    *,
    user_id: int,
    name: str,
    limit: int = 60,
) -> str:
    """Return the requested name, or its first free numeric variant.

    Used where the name is derived rather than typed by the user, so a
    collision should not fail the whole operation.
    """
    base = name.strip()
    candidate = base
    suffix = 2
    while await workspace_name_taken(db, user_id=user_id, name=candidate):
        marker = f"-{suffix}"
        candidate = f"{base[: max(1, limit - len(marker))]}{marker}"
        suffix += 1
    return candidate


async def workspace_name_taken(
    db: AsyncSession,
    *,
    user_id: int,
    name: str,
) -> bool:
    """Report whether the owner already uses a workspace name."""
    name_key = normalize_workspace_name(name)
    if not name_key:
        return False
    existing = (
        await db.execute(
            select(Workspace.id).where(
                Workspace.user_id == user_id,
                Workspace.name_key == name_key,
            )
        )
    ).first()
    return existing is not None


async def active_workspace_image(
    db: AsyncSession,
    *,
    template_id: str,
    image_ref: str,
) -> WorkspaceImage | None:
    """Resolve the exact enabled managed image used for a new workspace."""
    if not image_ref:
        return None
    return (
        await db.execute(
            select(WorkspaceImage)
            .where(
                WorkspaceImage.template_id == template_id,
                WorkspaceImage.image_ref == image_ref,
                WorkspaceImage.enabled.is_(True),
            )
            .order_by(WorkspaceImage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def workspace_runtime_image(
    db: AsyncSession,
    workspace: Workspace,
    template,
) -> tuple[str, str]:
    """Return the immutable image reference/checksum pinned to a workspace."""
    if workspace.image_id:
        image = await db.get(WorkspaceImage, workspace.image_id)
        if image is None:
            raise RuntimeError("Workspace için sabitlenen image kaydı bulunamadı.")
        return image.image_ref, image.sha256
    return template.image_tag, ""


async def allocate_workspace_port(db: AsyncSession, node_id: str) -> int:
    """Allocate a port unique on one worker.

    The database constraint closes races between controller requests; the
    worker remains the final authority because unmanaged host processes may
    also occupy a port.
    """
    stmt = select(Workspace.host_port).where(
        Workspace.node_id == node_id,
        Workspace.status != WorkspaceStatus.DELETED,
    )
    used_ports = set((await db.execute(stmt)).scalars().all())
    for port in range(settings.PORT_RANGE_START, settings.PORT_RANGE_END + 1):
        if port not in used_ports:
            return port
    raise RuntimeError("Workspace port aralığında boş port kalmadı.")


async def reserve_workspace(
    db: AsyncSession,
    *,
    data: WorkspaceCreate,
    current_user: User,
    placement: WorkspacePlacement,
    template,
    flavor: Flavor,
    workspace_image: WorkspaceImage | None = None,
) -> Workspace:
    """Atomically reserve a worker port and, when requested, one GPU slot."""
    node = placement.node
    accelerator = placement.accelerator
    workspace_id = str(uuid.uuid4())
    host_port = await allocate_workspace_port(db, node.id)
    name = data.name.strip()
    # The UUID keeps the container name unique; the slug only makes `podman ps`
    # readable, so an unslugifiable name simply contributes nothing.
    name_fragment = workspace_name_slug(name)
    container_name = "-".join(
        part
        for part in ("devcloud", str(current_user.id), name_fragment, workspace_id[:8])
        if part
    )
    workspace = Workspace(
            id=workspace_id,
            # `name_key` is derived by the mapper from `name`.
            name=name,
            description=data.description.strip(),
            user_id=current_user.id,
            node_id=node.id,
            template_id=data.template_id,
            flavor_id=data.flavor_id,
            image_id=workspace_image.id if workspace_image else None,
            container_name=container_name,
            host_port=host_port,
            container_port=template.default_port,
            storage_path="",
            status=WorkspaceStatus.CREATING,
            auto_stop_minutes=data.auto_stop_minutes,
            accelerator_device_id=accelerator.device_id if accelerator else None,
            accelerator_cdi_name=accelerator.cdi_name if accelerator else None,
            accelerator_model=accelerator.model if accelerator else None,
            accelerator_kind=accelerator.kind if accelerator else None,
            accelerator_slot=accelerator.slot if accelerator else None,
            accelerator_memory_mb=accelerator.memory_mb if accelerator else 0,
            accelerator_shared_slots=accelerator.shared_slots if accelerator else 0,
            created_at=datetime.now(timezone.utc),
    )
    db.add(workspace)
    await db.flush()
    return workspace


async def schedule_and_reserve_workspace(
    db: AsyncSession,
    *,
    data: WorkspaceCreate,
    current_user: User,
    template,
    flavor: Flavor,
    workspace_image: WorkspaceImage | None = None,
    linked_deployment: MlflowDeployment | None = None,
) -> tuple[Workspace, WorkspacePlacement]:
    """Check name, user quota and placement inside one serialized reservation."""
    user_id = current_user.id
    if await workspace_name_taken(db, user_id=user_id, name=data.name):
        raise WorkspaceNameConflict(WORKSPACE_NAME_CONFLICT_DETAIL)
    existing = (await db.execute(select(Workspace).where(Workspace.user_id == user_id))).scalars().all()
    disk_usage = await get_workspace_disk_usage_by_user(existing)
    if workspace_image is None:
        workspace_image = await active_workspace_image(
            db, template_id=template.id, image_ref=template.image_tag
        )
    required_image = workspace_image.image_ref if workspace_image else template.image_tag
    required_sha256 = workspace_image.sha256 if workspace_image else None
    for attempt in range(5):
        try:
            async with admission_transaction(db):
                user = await db.get(User, user_id, populate_existing=True)
                error = None
                if not (linked_deployment and linked_deployment.quota_reserved):
                    error = await get_quota_error(
                        db, user, flavor, disk_used_bytes=disk_usage.get(user_id, 0)
                    )
                if error:
                    raise QuotaExceeded(error)
                placement = await select_workspace_placement(
                    db,
                    flavor,
                    required_image=required_image,
                    required_image_sha256=required_sha256,
                )
                workspace = await reserve_workspace(
                    db, data=data, current_user=user, placement=placement,
                    template=template, flavor=flavor, workspace_image=workspace_image,
                )
                if linked_deployment is not None:
                    linked_deployment.workspace_id = workspace.id
                    linked_deployment.quota_reserved = False
                    db.add(linked_deployment)
            return workspace, placement
        except IntegrityError as exc:
            # Port and GPU-slot races are worth retrying; a duplicate name is
            # a decision the caller has to change, so surface it verbatim
            # instead of exhausting the attempts and reporting a 503.
            if _is_workspace_name_conflict(exc):
                raise WorkspaceNameConflict(WORKSPACE_NAME_CONFLICT_DETAIL) from exc
            if attempt == 4:
                raise RuntimeError("Workspace reservation conflicted repeatedly.")
    raise RuntimeError("Workspace reservation failed.")


async def get_quota_error(
    db: AsyncSession,
    user: User,
    flavor: Flavor,
    *,
    disk_used_bytes: int | None = None,
    include_disk: bool = True,
) -> str | None:
    """Return a readable quota error for a proposed workspace allocation.

    CPU and RAM are charged only for workspaces that currently hold compute;
    `quota_violations` applies that filter. The full workspace list is still
    passed through so the disk metric keeps counting stopped workspaces.
    """
    result = await db.execute(
        select(Workspace).where(Workspace.user_id == user.id)
    )
    workspaces = result.scalars().all()
    reservations = (
        await db.execute(
            select(MlflowDeployment).where(
                MlflowDeployment.user_id == user.id,
                MlflowDeployment.quota_reserved.is_(True),
                MlflowDeployment.workspace_id.is_(None),
            )
        )
    ).scalars().all()
    from types import SimpleNamespace
    quota_allocations = [
        *workspaces,
        *(SimpleNamespace(flavor_id=item.flavor_id) for item in reservations),
    ]
    if disk_used_bytes is None and include_disk:
        disk_usage = await get_workspace_disk_usage_by_user(workspaces)
        disk_used_bytes = disk_usage.get(user.id, 0)
    violations = await asyncio.to_thread(
        quota_violations,
        user,
        quota_allocations,
        flavor,
        disk_used_bytes=disk_used_bytes or 0,
        include_disk=include_disk,
    )
    if not violations:
        return None
    return "Kullanıcı kotası aşıldı: " + "; ".join(violations) + "."


async def get_resume_quota_error(
    db: AsyncSession,
    workspace: Workspace,
) -> str | None:
    """Return a quota error blocking a workspace from resuming, if any.

    Because a stopped workspace no longer charges CPU and RAM, resuming it is a
    fresh admission decision and has to be re-checked. A workspace already
    holding compute is counted in the current usage, so it admits for free.
    Disk is excluded: the check runs under the admission lock, which must never
    perform worker I/O, and a full disk should not trap a user out of the very
    workspace they need in order to clean it up.
    """
    if consumes_compute(workspace):
        return None
    flavor = get_flavor(workspace.flavor_id)
    if not flavor:
        return None
    owner = await db.get(User, workspace.user_id, populate_existing=True)
    if owner is None:
        return None
    return await get_quota_error(
        db, owner, flavor, disk_used_bytes=0, include_disk=False
    )


@workspace_router.get("/templates", response_model=list[TemplateInfo])
async def get_templates(db: Annotated[AsyncSession, Depends(get_db)]):
    """List available project environment templates."""
    return await list_enabled_templates(db)


@workspace_router.get("/flavors", response_model=list[FlavorInfo])
async def get_flavors(db: Annotated[AsyncSession, Depends(get_db)]):
    """List available resource flavors."""
    catalog = []
    for item in await list_enabled_flavors(db):
        flavor = get_flavor(item.id)
        available, message = await flavor_availability(db, flavor)
        details = (
            await accelerator_availability_details(db, flavor)
            if flavor and flavor.accelerator_count
            else {}
        )
        catalog.append(item.model_copy(update={
            "available": available,
            "availability_message": message,
            **details,
        }))
    return catalog


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
    nodes = (await db.execute(select(Node))).scalars().all()
    disk_usage = await get_workspace_disk_usage_by_user(workspaces)
    user_usage = await asyncio.to_thread(
        get_user_usage,
        current_user,
        workspaces,
        disk_used_bytes=disk_usage.get(current_user.id, 0),
    )
    return {
        "system": get_cluster_usage(nodes),
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
        .where(
            Workspace.user_id == current_user.id,
            Workspace.template_id != "mlflow-serving",
        )
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
    if template.id == "mlflow-serving":
        raise HTTPException(
            status_code=400,
            detail="MLflow servis workspace yalnızca model deployment API'si ile oluşturulabilir.",
        )
    if not await template_enabled(db, data.template_id):
        raise HTTPException(status_code=400, detail=f"Şablon devre dışı: {data.template_id}")

    flavor = await resolve_flavor(db, data.flavor_id)
    if not flavor or not await flavor_enabled(db, data.flavor_id):
        raise HTTPException(status_code=400, detail=f"Geçersiz kaynak profili ID: {data.flavor_id}")

    try:
        workspace, placement = await schedule_and_reserve_workspace(
            db,
            data=data,
            current_user=current_user,
            template=template,
            flavor=flavor,
        )
    except (QuotaExceeded, WorkspaceNameConflict) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (NoSchedulableNode, RuntimeError) as e:
        raise HTTPException(status_code=503, detail=str(e))


    # Launch container via Podman
    try:
        runtime = runtime_for_node(workspace.node_id)
        image_ref, image_sha256 = await workspace_runtime_image(db, workspace, template)
        container_id, storage_path = await runtime.create_workspace_container(
            workspace_id=workspace.id,
            user_id=current_user.id,
            container_name=workspace.container_name,
            template_id=template.id,
            flavor_id=flavor.id,
            template_definition=_template_definition(template),
            flavor_definition=_flavor_definition(flavor),
            host_port=workspace.host_port,
            workspace_token=workspace.workspace_token,
            accelerator_cdi_name=workspace.accelerator_cdi_name or "",
            image_ref=image_ref,
            image_sha256=image_sha256,
            mlflow_environment=await _workspace_mlflow_environment(db, current_user.id),
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
            if template.id == "mlflow-serving":
                await emit_error(
                    "MLflow servis workspace yalnızca model deployment ekranından oluşturulabilir."
                )
                return
            if not await template_enabled(db, data.template_id):
                await emit_error(f"Şablon devre dışı: {data.template_id}")
                return

            flavor = await resolve_flavor(db, data.flavor_id)
            if not flavor or not await flavor_enabled(db, data.flavor_id):
                await emit_error(f"Geçersiz kaynak profili: {data.flavor_id}")
                return

            await emit_log(f"Şablon: {template.name} ({template.image_tag})", "info")
            await emit_log(f"Çalışma alanı kaynağı: {flavor.cpus} CPU, {flavor.memory_display} RAM ({flavor.name})", "info")

            try:
                workspace, placement = await schedule_and_reserve_workspace(
                    db,
                    data=data,
                    current_user=current_user,
                    template=template,
                    flavor=flavor,
                )
                await emit_log(
                    f"Worker seçildi: {placement.node.name}; host portu ayrıldı: "
                    f"{workspace.host_port}",
                    "info",
                )
                if placement.accelerator:
                    await emit_log(
                        f"GPU ayrıldı: {placement.accelerator.model}; "
                        f"slot {placement.accelerator.slot + 1}/"
                        f"{placement.accelerator.shared_slots}",
                        "success",
                    )
            except WorkspaceNameConflict as e:
                await emit_error(str(e))
                return
            except (NoSchedulableNode, RuntimeError) as e:
                await emit_error(f"Kaynak ayrılamadı: {str(e)}")
                return

            await emit_log(f"Kullanıcı #{current_user.id} için kalıcı volume hazırlandı", "info")

            # Launch container with progress callback
            runtime = runtime_for_node(workspace.node_id)
            image_ref, image_sha256 = await workspace_runtime_image(db, workspace, template)
            container_id, storage_path = await runtime.create_workspace_container(
                workspace_id=workspace.id,
                user_id=current_user.id,
                container_name=workspace.container_name,
                template_id=template.id,
                flavor_id=flavor.id,
                template_definition=_template_definition(template),
                flavor_definition=_flavor_definition(flavor),
                host_port=workspace.host_port,
                workspace_token=workspace.workspace_token,
                accelerator_cdi_name=workspace.accelerator_cdi_name or "",
                image_ref=image_ref,
                image_sha256=image_sha256,
                mlflow_environment=await _workspace_mlflow_environment(db, current_user.id),
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

    async with admission_transaction(db):
        await db.refresh(workspace)
        if workspace.status in {WorkspaceStatus.CREATING, WorkspaceStatus.STARTING, WorkspaceStatus.STOPPING}:
            raise HTTPException(status_code=409, detail="Çalışma alanı kurulumu devam ediyor veya başka bir işlem sürüyor.")
        try:
            await validate_restart_capacity(db, workspace)
        except NoSchedulableNode as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # The owner's quota, not the caller's: an admin may resume another
        # user's workspace and must not spend their own allowance.
        resume_quota_error = await get_resume_quota_error(db, workspace)
        if resume_quota_error:
            raise HTTPException(status_code=409, detail=resume_quota_error)
        was_running = workspace.status == WorkspaceStatus.RUNNING
        workspace.status = WorkspaceStatus.STARTING

    try:
        runtime = runtime_for_node(workspace.node_id)
        container_exists = await runtime.container_exists(workspace.container_name)
        if was_running and container_exists and await runtime.get_container_status(workspace.container_name) == "running":
            workspace.status = WorkspaceStatus.RUNNING
            await db.commit()
            ws_out = WorkspaceOut.model_validate(workspace)
            ws_out.web_url = f"/proxy/{workspace.id}/"
            return ws_out

        if container_exists:
            success = await runtime.start_container(workspace.container_name)
            if success and workspace.template_id == "mlflow-serving":
                success = False
                for _ in range(30):
                    if await runtime.health_ready(
                        workspace.container_name, workspace.host_port, "/ping"
                    ):
                        success = True
                        break
                    await asyncio.sleep(0.5)
                if not success:
                    workspace.error_message = "Model servisi /ping sağlık kontrolünü geçemedi."
        else:
            logger.info(
                "Recreating missing container %s with persistent storage %s",
                workspace.container_name,
                workspace.storage_path,
            )
            template = await resolve_template(db, workspace.template_id)
            flavor = await resolve_flavor(db, workspace.flavor_id)
            if not template or not flavor:
                raise ValueError("Workspace şablonu veya kaynak profili artık bulunamıyor.")
            image_ref, image_sha256 = await workspace_runtime_image(db, workspace, template)
            container_id, storage_path = await runtime.create_workspace_container(
                workspace_id=workspace.id,
                user_id=workspace.user_id,
                container_name=workspace.container_name,
                template_id=workspace.template_id,
                flavor_id=workspace.flavor_id,
                template_definition=_template_definition(template),
                flavor_definition=_flavor_definition(flavor),
                host_port=workspace.host_port,
                workspace_token=workspace.workspace_token,
                accelerator_cdi_name=workspace.accelerator_cdi_name or "",
                image_ref=image_ref,
                image_sha256=image_sha256,
                mlflow_environment=(
                    {}
                    if workspace.template_id == "mlflow-serving"
                    else await _workspace_mlflow_environment(db, workspace.user_id)
                ),
                service_environment=await _workspace_service_environment(db, workspace),
            )
            workspace.container_id = container_id
            workspace.storage_path = storage_path
            success = True
    except (ValueError, RuntimeError, TimeoutError) as exc:
        logger.exception("Failed to start workspace %s", workspace.id)
        workspace.error_message = str(exc)
        success = False

    if success:
        workspace.status = WorkspaceStatus.RUNNING
        workspace.last_started_at = datetime.now(timezone.utc)
        workspace.error_message = None
        await _sync_mlflow_deployment_status(
            db,
            workspace,
            MlflowDeploymentStatus.RUNNING,
            "Model servisi başlatıldı.",
        )
    else:
        workspace.status = WorkspaceStatus.ERROR
        workspace.error_message = workspace.error_message or "Container yeniden başlatılamadı."
        await _sync_mlflow_deployment_status(
            db,
            workspace,
            MlflowDeploymentStatus.FAILED,
            "Model servisi başlatılamadı.",
            workspace.error_message,
        )

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

    async with admission_transaction(db):
        await db.refresh(workspace)
        if workspace.status in {WorkspaceStatus.CREATING, WorkspaceStatus.STARTING, WorkspaceStatus.STOPPING}:
            raise HTTPException(status_code=409, detail="Çalışma alanı kurulumu devam ediyor veya başka bir işlem sürüyor.")
        previous_status = workspace.status
        workspace.status = WorkspaceStatus.STOPPING
    runtime = runtime_for_node(workspace.node_id)
    try:
        if not await runtime.stop_container(workspace.container_name):
            raise RuntimeError("Worker could not stop the container; state was preserved.")
    except (RuntimeError, TimeoutError) as exc:
        workspace.status = previous_status
        workspace.error_message = str(exc)
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    workspace.status = WorkspaceStatus.STOPPED
    workspace.error_message = None
    workspace.last_stopped_at = datetime.now(timezone.utc)
    await _sync_mlflow_deployment_status(
        db,
        workspace,
        MlflowDeploymentStatus.STOPPED,
        "Model servisi durduruldu.",
    )

    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"
    return ws_out


async def delete_workspace_resources(
    db: AsyncSession,
    workspace: Workspace,
    *,
    allow_transient: bool = False,
) -> None:
    """Delete one workspace after its owning domain has authorized lifecycle."""
    async with admission_transaction(db):
        await db.refresh(workspace)
        if (
            workspace.status in {
                WorkspaceStatus.CREATING,
                WorkspaceStatus.STARTING,
                WorkspaceStatus.STOPPING,
            }
            and not allow_transient
        ):
            raise HTTPException(
                status_code=409,
                detail="Çalışma alanı kurulumu devam ediyor veya başka bir işlem sürüyor.",
            )
        previous_status = workspace.status
        workspace.status = WorkspaceStatus.STOPPING
    runtime = runtime_for_node(workspace.node_id)
    try:
        if not await runtime.delete_container(
            workspace.container_name, workspace.storage_path
        ):
            raise RuntimeError(
                "Worker could not delete the workspace; data and tracking were preserved."
            )
    except (RuntimeError, TimeoutError) as exc:
        workspace.status = previous_status
        workspace.error_message = str(exc)
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await db.delete(workspace)
    await db.commit()


@workspace_router.delete("/{workspace_id}")
async def delete_workspace_endpoint(
    workspace_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a user workspace; managed model services use deployment deletion."""
    workspace = await db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Çalışma alanı bulunamadı.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Erişim reddedildi.")
    if workspace.template_id == "mlflow-serving":
        raise HTTPException(
            status_code=409,
            detail="Model servis workspace'i deployment ekranından silinmelidir.",
        )
    await delete_workspace_resources(db, workspace)
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
    """Stream a ZIP backup from the worker that owns the workspace."""
    from app.agents.manager import AgentCommandError, AgentUnavailable, agent_manager

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    res = await db.execute(stmt)
    workspace = res.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")
    try:
        connection = agent_manager.get(workspace.node_id)
        metadata, stream = await connection.open_stream(
            "workspace.backup.open",
            {
                "workspace_id": workspace.id,
                "container_name": workspace.container_name,
            },
            timeout=120,
        )
    except (AgentUnavailable, AgentCommandError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    async def body():
        try:
            while True:
                item = await connection.receive_stream(stream)
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item.data
        finally:
            await connection.close_stream(stream.id)

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", workspace.name).strip(".-")
    filename = f"{(safe_name[:80] or workspace.id)}-backup.zip"
    headers = {
        "Content-Disposition": (
            'attachment; filename="workspace-backup.zip"; '
            f"filename*=UTF-8''{quote(filename)}"
        ),
    }
    size = metadata.get("size")
    if isinstance(size, int) and size >= 0:
        headers["Content-Length"] = str(size)
    return StreamingResponse(
        body(),
        media_type="application/zip",
        headers=headers,
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
    from app.agents.manager import agent_manager
    from app.models.custom_template import CustomTemplate

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    res = await db.execute(stmt)
    workspace = res.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")
    if (
        settings.DEVCLOUD_DEPLOYMENT_ROLE != "all-in-one"
        and not settings.USE_MOCK_PODMAN
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Workspace snapshots are currently all-in-one only. "
                "Distributed export requires a separately authorized registry "
                "publication workflow."
            ),
        )
    slug = re.sub(r"[^a-zA-Z0-9_\-]", "", template_name.lower().replace(" ", "-"))[:30]
    template_id = f"custom-{slug}"
    image_tag = f"localhost/devcloud-{template_id}:latest"
    try:
        result = await agent_manager.get(workspace.node_id).request(
            "container.snapshot",
            {
                "workspace_id": workspace.id,
                "container_name": workspace.container_name,
                "image_tag": image_tag,
            },
            timeout=180,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    image_tag_or_err = str(result["image_tag"])

    custom_tpl = CustomTemplate(
        id=template_id,
        name=template_name.strip(),
        description=template_description.strip() or f"Snapshotted from {workspace.name}",
        category="Özel",
        icon="cube",
        image_tag=image_tag_or_err,
        default_port=workspace.container_port,
        ide_type=_snapshot_ide_type(workspace.template_id),
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
