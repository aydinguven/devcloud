"""Workspace admission, reservation and runtime-definition logic.

Shared by the workspace HTTP routes and the MLflow deployment orchestrator, so
it must not depend on either. Errors are domain exceptions; HTTP mapping is the
caller's job.
"""

import asyncio
from datetime import datetime, timezone
import logging
from types import SimpleNamespace
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.mlflow import (
    MlflowConfigurationError,
    config_from_record as mlflow_config_from_record,
    validate_config as validate_mlflow_config,
)
from app.mlflow_workspace import environment_from_config
from app.models.mlflow_deployment import MlflowDeployment
from app.models.mlflow_server_settings import MlflowServerSettings
from app.models.mlflow_settings import MlflowSettings
from app.models.user import User
from app.models.workspace import (
    Workspace,
    WorkspaceStatus,
    consumes_compute,
    normalize_workspace_name,
    workspace_name_slug,
)
from app.models.workspace_image import WorkspaceImage
from app.orchestrator.admission import admission_transaction
from app.orchestrator.flavors import Flavor, get_flavor
from app.orchestrator.metrics_service import get_workspace_disk_usage_by_user
from app.orchestrator.runtime_backend import runtime_for_node
from app.orchestrator.scheduler import WorkspacePlacement, select_workspace_placement
from app.quotas import effective_quota
from app.resource_usage import quota_violations
from app.schemas.workspace import WorkspaceCreate
from app.security.secrets import SecretDecryptionError

logger = logging.getLogger("devcloud.workspaces")


class WorkspaceOperationError(Exception):
    """A lifecycle operation was refused or failed; carries its HTTP status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def template_definition(template) -> dict:
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


def flavor_definition(flavor: Flavor) -> dict:
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


async def workspace_mlflow_environment(
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


async def workspace_service_environment(
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


async def sync_mlflow_deployment_status(
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


class QuotaExceeded(RuntimeError):
    pass


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
    quota_allocations = [
        *workspaces,
        *(SimpleNamespace(flavor_id=item.flavor_id) for item in reservations),
    ]
    if disk_used_bytes is None and include_disk:
        disk_usage = await get_workspace_disk_usage_by_user(workspaces)
        disk_used_bytes = disk_usage.get(user.id, 0)
    quota = await effective_quota(db, user)
    violations = await asyncio.to_thread(
        quota_violations,
        user,
        quota_allocations,
        flavor,
        disk_used_bytes=disk_used_bytes or 0,
        include_disk=include_disk,
        quota=quota,
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
            raise WorkspaceOperationError(
                409,
                "Çalışma alanı kurulumu devam ediyor veya başka bir işlem sürüyor.",
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
        raise WorkspaceOperationError(502, str(exc)) from exc
    await db.delete(workspace)
    await db.commit()
