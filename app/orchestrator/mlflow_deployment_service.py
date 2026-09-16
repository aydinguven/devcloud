"""Durable orchestration for immutable MLflow model service deployments."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select, update

from app.agents.manager import AgentCommandError, AgentUnavailable, agent_manager
from app.config import settings
from app.database import AsyncSessionLocal
from app.integrations.mlflow import (
    MlflowClient,
    MlflowConfigurationError,
    MlflowConnectionError,
    config_from_record,
    validate_config,
)
from app.mlflow_workspace import environment_from_config
from app.models.mlflow_deployment import (
    MlflowDeployment,
    MlflowDeploymentEvent,
    MlflowDeploymentStatus,
    MlflowModelBuild,
    MlflowModelBuildStatus,
)
from app.models.mlflow_server_settings import MlflowServerSettings
from app.models.mlflow_settings import MlflowSettings
from app.models.node import Node
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.workspace_image import WorkspaceImage
from app.orchestrator.flavors import get_flavor
from app.orchestrator.admission import admission_transaction
from app.orchestrator.runtime_backend import runtime_for_node
from app.orchestrator.scheduler import NoSchedulableNode, select_worker_node
from app.orchestrator.templates import get_template
from app.routes.workspace_routes import (
    QuotaExceeded,
    _flavor_definition,
    _template_definition,
    delete_workspace_resources,
    get_quota_error,
    schedule_and_reserve_workspace,
    workspace_runtime_image,
)
from app.schemas.workspace import WorkspaceCreate
from app.workspace_catalog import resolve_flavor
from app.workspace_image_service import (
    WorkspaceImageError,
    delete_registry_image,
    image_archive_path,
    import_registry_image,
)

logger = logging.getLogger("devcloud.mlflow.deployments")
_LEASE_OWNER = f"{socket.gethostname()}:{os.getpid()}"
_LEASE_SECONDS = 300
_ACTIVE_DEPLOYMENT_STATUSES = {
    MlflowDeploymentStatus.WAITING_FOR_IMAGE,
    MlflowDeploymentStatus.SCHEDULING,
    MlflowDeploymentStatus.STARTING,
    MlflowDeploymentStatus.HEALTH_CHECKING,
    MlflowDeploymentStatus.DELETING,
}


class DeploymentLeaseLost(RuntimeError):
    pass


async def _assert_deployment_lease(db, deployment: MlflowDeployment) -> None:
    await db.refresh(deployment)
    if deployment.lease_owner != _LEASE_OWNER:
        raise DeploymentLeaseLost("Deployment lease başka bir worker tarafından devralındı.")


async def _lease_heartbeat(
    deployment_id: str,
    build_id: str | None = None,
    interval_seconds: int = 30,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS)
        async with AsyncSessionLocal() as lease_db:
            deployment_result = await lease_db.execute(
                update(MlflowDeployment)
                .where(
                    MlflowDeployment.id == deployment_id,
                    MlflowDeployment.lease_owner == _LEASE_OWNER,
                    MlflowDeployment.status.in_(_ACTIVE_DEPLOYMENT_STATUSES),
                )
                .values(lease_expires_at=deadline)
            )
            if deployment_result.rowcount != 1:
                await lease_db.rollback()
                return
            if build_id:
                await lease_db.execute(
                    update(MlflowModelBuild)
                    .where(
                        MlflowModelBuild.id == build_id,
                        MlflowModelBuild.lease_owner == _LEASE_OWNER,
                        MlflowModelBuild.status.in_(
                            {
                                MlflowModelBuildStatus.VALIDATING,
                                MlflowModelBuildStatus.BUILDING,
                                MlflowModelBuildStatus.IMPORTING,
                            }
                        ),
                    )
                    .values(lease_expires_at=deadline)
                )
            await lease_db.commit()


@asynccontextmanager
async def _maintain_deployment_lease(
    deployment_id: str,
    build_id: str | None = None,
):
    task = asyncio.create_task(_lease_heartbeat(deployment_id, build_id))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def append_deployment_event(
    db,
    deployment: MlflowDeployment,
    message: str,
    level: str = "info",
) -> None:
    sequence = (
        await db.execute(
            select(func.coalesce(func.max(MlflowDeploymentEvent.sequence), 0)).where(
                MlflowDeploymentEvent.deployment_id == deployment.id
            )
        )
    ).scalar_one()
    db.add(
        MlflowDeploymentEvent(
            deployment_id=deployment.id,
            sequence=int(sequence) + 1,
            level=level[:16],
            message=message[:1000],
        )
    )


async def set_deployment_stage(
    db,
    deployment: MlflowDeployment,
    status: MlflowDeploymentStatus,
    message: str,
    *,
    level: str = "info",
    error: str | None = None,
) -> None:
    await _assert_deployment_lease(db, deployment)
    deployment.status = status
    deployment.status_message = message[:500]
    deployment.error_message = error
    if status in {MlflowDeploymentStatus.FAILED, MlflowDeploymentStatus.DELETING}:
        deployment.quota_reserved = False
    if status in {
        MlflowDeploymentStatus.RUNNING,
        MlflowDeploymentStatus.STOPPED,
        MlflowDeploymentStatus.FAILED,
        MlflowDeploymentStatus.DELETING,
    }:
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
    else:
        deployment.lease_owner = _LEASE_OWNER
        deployment.lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=_LEASE_SECONDS
        )
    await append_deployment_event(db, deployment, message, level)
    db.add(deployment)
    await db.commit()


async def _mlflow_client_for_user(db, user_id: int) -> MlflowClient:
    credentials = (
        await db.execute(
            select(MlflowSettings).where(MlflowSettings.user_id == user_id)
        )
    ).scalar_one_or_none()
    server = await db.get(MlflowServerSettings, 1)
    if credentials is None or server is None:
        raise MlflowConfigurationError("MLflow bağlantısı yapılandırılmamış.")
    config = config_from_record(credentials, server)
    validate_config(config, require_enabled=True)
    return MlflowClient(config)


async def _reserve_deployment_quota(db, deployment: MlflowDeployment) -> None:
    if deployment.quota_reserved:
        return
    async with admission_transaction(db):
        await db.refresh(deployment)
        if deployment.quota_reserved:
            return
        user = await db.get(User, deployment.user_id, populate_existing=True)
        flavor = await resolve_flavor(db, deployment.flavor_id)
        if user is None or flavor is None:
            raise RuntimeError("Deployment kullanıcısı veya kaynak profili bulunamadı.")
        error = await get_quota_error(db, user, flavor)
        if error:
            raise QuotaExceeded(error)
        deployment.quota_reserved = True
        db.add(deployment)


def _mlflow_ca_certificate(client: MlflowClient) -> str:
    path_value = client.config.ca_cert_file.strip()
    if not path_value:
        return ""
    path = Path(path_value).resolve()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("MLflow özel CA dosyası worker build için okunamadı.") from exc
    if len(content.encode("utf-8")) > 1024 * 1024 or "BEGIN CERTIFICATE" not in content:
        raise RuntimeError("MLflow özel CA dosyası geçersiz veya çok büyük.")
    return content


def _image_tag(build: MlflowModelBuild) -> str:
    prefix = settings.DEVCLOUD_REGISTRY_URL.rstrip("/")
    if not prefix:
        raise RuntimeError("MLflow deployments require DEVCLOUD_REGISTRY_URL.")
    safe_name = re.sub(r"[^a-z0-9._-]+", "-", build.model_name.lower()).strip("-.")
    safe_name = safe_name[:80] or "model"
    return (
        f"{prefix}/devcloud-mlflow-{safe_name}-v{build.model_version}:"
        f"{build.id[:12]}"
    )


async def enroll_model_build_cleanup(
    db,
    build_id: str | None,
    *,
    excluding_deployment_id: str | None = None,
) -> bool:
    """Mark a last-reference build for durable cleanup in the caller transaction."""
    if not build_id:
        return False
    reference_query = select(func.count(MlflowDeployment.id)).where(
        MlflowDeployment.build_id == build_id
    )
    if excluding_deployment_id:
        reference_query = reference_query.where(
            MlflowDeployment.id != excluding_deployment_id
        )
    references = (await db.execute(reference_query)).scalar_one()
    if references:
        return False
    build = await db.get(MlflowModelBuild, build_id)
    if build is None:
        return False
    build.status = MlflowModelBuildStatus.DELETING
    build.status_message = "Model image artıkları temizleme kuyruğuna alındı."
    build.lease_owner = ""
    build.lease_expires_at = None
    if build.workspace_image_id:
        image = await db.get(WorkspaceImage, build.workspace_image_id)
        if image:
            image.enabled = False
            db.add(image)
    db.add(build)
    return True


async def enroll_orphaned_model_builds(db, limit: int = 20) -> int:
    enrolled = 0
    async with admission_transaction(db):
        build_ids = list(
            (
                await db.execute(
                    select(MlflowModelBuild.id)
                    .where(
                        MlflowModelBuild.status.in_(
                            {
                                MlflowModelBuildStatus.READY,
                                MlflowModelBuildStatus.FAILED,
                            }
                        ),
                        ~select(MlflowDeployment.id)
                        .where(MlflowDeployment.build_id == MlflowModelBuild.id)
                        .exists(),
                    )
                    .limit(limit)
                )
            ).scalars()
        )
        for build_id in build_ids:
            enrolled += int(await enroll_model_build_cleanup(db, build_id))
    return enrolled


async def garbage_collect_model_build(db, build_id: str | None) -> bool:
    """Persist and retry cleanup until registry, workers, and archive agree."""
    if not build_id:
        return True
    references = (
        await db.execute(
            select(func.count(MlflowDeployment.id)).where(
                MlflowDeployment.build_id == build_id
            )
        )
    ).scalar_one()
    if references:
        return False
    build = await db.get(MlflowModelBuild, build_id)
    if build is None:
        return True
    image = (
        await db.get(WorkspaceImage, build.workspace_image_id)
        if build.workspace_image_id
        else None
    )
    if image:
        workspace_references = (
            await db.execute(
                select(func.count(Workspace.id)).where(Workspace.image_id == image.id)
            )
        ).scalar_one()
        if workspace_references:
            return False
    build.status = MlflowModelBuildStatus.DELETING
    build.status_message = "Model image artıkları temizleniyor."
    build.lease_owner = ""
    build.lease_expires_at = None
    if image:
        image.enabled = False
        db.add(image)
    db.add(build)
    await db.commit()

    image_ref = image.image_ref if image else _image_tag(build)
    source_ref = image.source_ref if image else image_ref
    pending_worker = False
    nodes = (await db.execute(select(Node))).scalars().all()
    await db.commit()
    for node in nodes:
        try:
            capabilities = json.loads(node.capabilities_json or "{}")
        except ValueError:
            capabilities = {}
        reported = any(
            isinstance(item, dict) and item.get("image_ref") == image_ref
            for item in (capabilities.get("workspace_images") or [])
        )
        if not reported:
            continue
        if not agent_manager.is_connected(node.id):
            pending_worker = True
            continue
        try:
            result = await agent_manager.get(node.id).request(
                "image.remove", {"image_tag": image_ref}, timeout=130
            )
            pending_worker = pending_worker or not bool(result.get("success"))
        except (AgentCommandError, AgentUnavailable, TimeoutError):
            pending_worker = True

    registry_deleted = True
    try:
        await asyncio.to_thread(
            delete_registry_image,
            source_ref=source_ref,
            username=settings.DEVCLOUD_REGISTRY_USERNAME,
            password=settings.DEVCLOUD_REGISTRY_PASSWORD,
        )
    except WorkspaceImageError:
        registry_deleted = False
        logger.warning("Registry model image cleanup remains pending: %s", source_ref)
    if pending_worker or not registry_deleted:
        return False

    build = await db.get(MlflowModelBuild, build_id)
    image = (
        await db.get(WorkspaceImage, build.workspace_image_id)
        if build and build.workspace_image_id
        else None
    )
    archive_path = image_archive_path(image.filename) if image else None
    if build:
        await db.delete(build)
    if image:
        await db.delete(image)
    await db.commit()
    if archive_path:
        archive_path.unlink(missing_ok=True)
    return True


async def _ready_build(db, deployment: MlflowDeployment) -> MlflowModelBuild | None:
    return (
        await db.execute(
            select(MlflowModelBuild)
            .where(
                MlflowModelBuild.user_id == deployment.user_id,
                MlflowModelBuild.model_name == deployment.model_name,
                MlflowModelBuild.model_version == deployment.model_version,
                MlflowModelBuild.recipe_version == "mlflow-v1",
                MlflowModelBuild.status == MlflowModelBuildStatus.READY,
                MlflowModelBuild.workspace_image_id.is_not(None),
            )
            .order_by(MlflowModelBuild.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _build_image(
    db,
    deployment: MlflowDeployment,
    client: MlflowClient,
) -> MlflowModelBuild:
    cached_build = None
    async with admission_transaction(db):
        await _assert_deployment_lease(db, deployment)
        build = await _ready_build(db, deployment)
        if build is not None:
            image = await db.get(WorkspaceImage, build.workspace_image_id)
            if image and image.enabled:
                deployment.build_id = build.id
                db.add(deployment)
                await append_deployment_event(
                    db,
                    deployment,
                    "Hazır model image önbellekten kullanılıyor.",
                    "success",
                )
                cached_build = build
        if cached_build is None:
            if deployment.build_id:
                await enroll_model_build_cleanup(
                    db,
                    deployment.build_id,
                    excluding_deployment_id=deployment.id,
                )
            build = MlflowModelBuild(
                user_id=deployment.user_id,
                model_name=deployment.model_name,
                model_version=deployment.model_version,
                run_id=deployment.run_id,
                source_uri=deployment.source_uri,
                model_uri=f"models:/{deployment.model_name}/{deployment.model_version}",
                status=MlflowModelBuildStatus.BUILDING,
                status_message="Model serving image oluşturuluyor.",
                lease_owner=_LEASE_OWNER,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(
                    seconds=_LEASE_SECONDS
                ),
            )
            db.add(build)
            await db.flush()
            deployment.build_id = build.id
            db.add(deployment)
            await append_deployment_event(
                db,
                deployment,
                "Model artifact'ları image build worker'ında hazırlanıyor.",
            )
    if cached_build is not None:
        return cached_build

    build_flavor = get_flavor("t1.nano")
    if build_flavor is None:
        raise RuntimeError("Image build kaynak profili bulunamadı.")
    node = await select_worker_node(db, build_flavor)
    await db.commit()
    image_tag = _image_tag(build)
    async with _maintain_deployment_lease(deployment.id, build.id):
        result = await agent_manager.get(node.id).request(
            "image.mlflow.build",
            {
                "model_name": build.model_name,
                "model_version": build.model_version,
                "image_tag": image_tag,
                "mlflow_environment": environment_from_config(client.config),
                "mlflow_ca_certificate": _mlflow_ca_certificate(client),
            },
            timeout=float(settings.MLFLOW_MODEL_BUILD_TIMEOUT_SECONDS) + 30,
        )
        if not result.get("success"):
            detail = str(result.get("logs") or "Image build başarısız.")[-4000:]
            build.status = MlflowModelBuildStatus.FAILED
            build.status_message = "Model serving image oluşturulamadı."
            build.error_message = detail
            build.lease_owner = ""
            build.lease_expires_at = None
            build.completed_at = datetime.now(timezone.utc)
            db.add(build)
            await db.commit()
            raise RuntimeError(detail)

        await _assert_deployment_lease(db, deployment)
        build.status = MlflowModelBuildStatus.IMPORTING
        build.status_message = "Model image doğrulanıp controller kataloğuna alınıyor."
        db.add(build)
        await append_deployment_event(db, deployment, "Model image oluşturuldu; checksum kataloğu hazırlanıyor.", "success")
        await db.commit()

        metadata = await asyncio.to_thread(
            import_registry_image,
            image_ref=image_tag,
            source_ref=image_tag,
            username=settings.DEVCLOUD_REGISTRY_USERNAME,
            password=settings.DEVCLOUD_REGISTRY_PASSWORD,
        )
    await _assert_deployment_lease(db, deployment)
    image = WorkspaceImage(
        id=str(metadata["id"]),
        template_id="mlflow-serving",
        purpose="mlflow_model",
        owner_user_id=deployment.user_id,
        display_name=f"{deployment.model_name} v{deployment.model_version}",
        image_ref=str(metadata["image_ref"]),
        source_type="mlflow_model",
        source_ref=image_tag,
        digest=str(metadata["digest"]),
        sha256=str(metadata["sha256"]),
        filename=str(metadata["filename"]),
        size=int(metadata["size"]),
        architecture=str(metadata["architecture"]),
        enabled=True,
    )
    db.add(image)
    await db.flush()
    build.workspace_image_id = image.id
    build.status = MlflowModelBuildStatus.READY
    build.status_message = "Model serving image hazır."
    build.error_message = None
    build.lease_owner = ""
    build.lease_expires_at = None
    build.completed_at = datetime.now(timezone.utc)
    db.add(build)
    await db.commit()
    return build


async def _reserve_when_image_synced(
    db,
    deployment: MlflowDeployment,
    build: MlflowModelBuild,
):
    image = await db.get(WorkspaceImage, build.workspace_image_id)
    user = await db.get(User, deployment.user_id)
    template = get_template("mlflow-serving")
    flavor = await resolve_flavor(db, deployment.flavor_id)
    if image is None or user is None or template is None or flavor is None:
        raise RuntimeError("Deployment çalışma alanı girdileri artık bulunamıyor.")
    request = WorkspaceCreate(
        name=deployment.name,
        description=f"MLflow {deployment.model_name} v{deployment.model_version}",
        template_id=template.id,
        flavor_id=flavor.id,
        auto_stop_minutes=deployment.auto_stop_minutes,
    )
    last_error: Exception | None = None
    for _ in range(60):
        try:
            await _assert_deployment_lease(db, deployment)
            workspace, placement = await schedule_and_reserve_workspace(
                db,
                data=request,
                current_user=user,
                template=template,
                flavor=flavor,
                workspace_image=image,
                linked_deployment=deployment,
            )
            return workspace, placement, template, flavor
        except NoSchedulableNode as exc:
            last_error = exc
            await db.rollback()
            await asyncio.sleep(2)
    raise RuntimeError(
        "Model image worker'lara zamanında senkronize edilemedi: "
        f"{last_error or 'uygun worker yok'}"
    )


async def process_mlflow_deployment(deployment_id: str) -> None:
    async with AsyncSessionLocal() as db:
        claimed = await db.execute(
            update(MlflowDeployment)
            .where(
                MlflowDeployment.id == deployment_id,
                MlflowDeployment.status == MlflowDeploymentStatus.QUEUED,
            )
            .values(
                status=MlflowDeploymentStatus.WAITING_FOR_IMAGE,
                status_message="Model versiyonu doğrulanıyor.",
                error_message=None,
                lease_owner=_LEASE_OWNER,
                lease_expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=_LEASE_SECONDS),
            )
        )
        await db.commit()
        if claimed.rowcount != 1:
            return
        deployment = await db.get(MlflowDeployment, deployment_id)
        if deployment is None:
            return
        await append_deployment_event(db, deployment, "Deployment kuyruğundan alındı.")
        await db.commit()

        try:
            client = await _mlflow_client_for_user(db, deployment.user_id)
            payload = await client.get_model_version(
                deployment.model_name, deployment.model_version
            )
            version = payload.get("model_version") or {}
            if (
                str(version.get("name") or "") != deployment.model_name
                or str(version.get("version") or "") != deployment.model_version
            ):
                raise RuntimeError("MLflow beklenen model versiyonunu döndürmedi.")
            deployment.run_id = str(version.get("run_id") or deployment.run_id)
            deployment.source_uri = str(version.get("source") or deployment.source_uri)
            await _reserve_deployment_quota(db, deployment)
            await set_deployment_stage(
                db,
                deployment,
                MlflowDeploymentStatus.WAITING_FOR_IMAGE,
                f"{deployment.model_name} v{deployment.model_version} doğrulandı.",
                level="success",
            )

            build = await _build_image(db, deployment, client)
            await set_deployment_stage(
                db,
                deployment,
                MlflowDeploymentStatus.SCHEDULING,
                "Model image worker'lara senkronize ediliyor ve kaynak ayrılıyor.",
            )
            workspace, placement, template, flavor = await _reserve_when_image_synced(
                db, deployment, build
            )
            deployment.workspace_id = workspace.id
            await set_deployment_stage(
                db,
                deployment,
                MlflowDeploymentStatus.STARTING,
                f"Worker seçildi: {placement.node.name}; model servisi başlatılıyor.",
            )

            image_ref, image_sha256 = await workspace_runtime_image(db, workspace, template)
            runtime = runtime_for_node(workspace.node_id)
            container_id, storage_path = await runtime.create_workspace_container(
                workspace_id=workspace.id,
                user_id=deployment.user_id,
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
                mlflow_environment={},
                service_environment={
                    "DISABLE_NGINX": "false",
                    "GUNICORN_CMD_ARGS": f"--workers={deployment.gunicorn_workers}",
                },
            )
            workspace.container_id = container_id
            workspace.storage_path = storage_path
            workspace.status = WorkspaceStatus.RUNNING
            workspace.last_started_at = datetime.now(timezone.utc)
            workspace.error_message = None
            db.add(workspace)
            await set_deployment_stage(
                db,
                deployment,
                MlflowDeploymentStatus.RUNNING,
                "Model servisi hazır; /invocations endpoint'i istek kabul ediyor.",
                level="success",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("MLflow deployment %s failed", deployment_id)
            await db.rollback()
            deployment = await db.get(MlflowDeployment, deployment_id)
            if deployment is None or deployment.lease_owner != _LEASE_OWNER:
                return
            if deployment.build_id:
                build = await db.get(MlflowModelBuild, deployment.build_id)
                if build and build.status in {
                    MlflowModelBuildStatus.VALIDATING,
                    MlflowModelBuildStatus.BUILDING,
                    MlflowModelBuildStatus.IMPORTING,
                }:
                    build.status = MlflowModelBuildStatus.FAILED
                    build.status_message = "Model image build başarısız oldu."
                    build.error_message = str(exc)[:4000]
                    build.lease_owner = ""
                    build.lease_expires_at = None
                    build.completed_at = datetime.now(timezone.utc)
                    db.add(build)
            if deployment.workspace_id:
                workspace = await db.get(Workspace, deployment.workspace_id)
                if workspace and workspace.status != WorkspaceStatus.RUNNING:
                    workspace.status = WorkspaceStatus.ERROR
                    workspace.error_message = str(exc)[:4000]
                    db.add(workspace)
            await set_deployment_stage(
                db,
                deployment,
                MlflowDeploymentStatus.FAILED,
                "Model deployment başarısız oldu.",
                level="error",
                error=str(exc)[:4000],
            )


async def _recover_stale_deployment(db, deployment: MlflowDeployment) -> None:
    claim = await db.execute(
        update(MlflowDeployment)
        .where(
            MlflowDeployment.id == deployment.id,
            or_(
                MlflowDeployment.lease_expires_at.is_(None),
                MlflowDeployment.lease_expires_at < datetime.now(timezone.utc),
            ),
        )
        .values(
            lease_owner=_LEASE_OWNER,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
    )
    await db.commit()
    if claim.rowcount != 1:
        return
    await db.refresh(deployment)
    if deployment.status == MlflowDeploymentStatus.DELETING:
        deployment.status = MlflowDeploymentStatus.FAILED
        deployment.status_message = "Yarım kalan silme işlemi yeniden denenebilir."
        deployment.error_message = "Controller silme işlemi sırasında yeniden başladı."
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
        db.add(deployment)
        await db.commit()
        return
    if not deployment.workspace_id:
        deployment.status = MlflowDeploymentStatus.QUEUED
        deployment.status_message = "Yarım kalan deployment devam ettirilecek."
        deployment.error_message = None
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
        db.add(deployment)
        await db.commit()
        return
    workspace = await db.get(Workspace, deployment.workspace_id)
    if workspace is None:
        deployment.workspace_id = None
        deployment.quota_reserved = False
        deployment.status = MlflowDeploymentStatus.QUEUED
        deployment.status_message = "Eksik workspace kaydı yeniden oluşturulacak."
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
        db.add(deployment)
        await db.commit()
        return
    await db.commit()
    runtime = runtime_for_node(workspace.node_id)
    try:
        exists = await runtime.container_exists(workspace.container_name)
        running = exists and await runtime.get_container_status(
            workspace.container_name
        ) == "running"
        healthy = running and await runtime.health_ready(
            workspace.container_name, workspace.host_port, "/ping"
        )
    except AgentUnavailable:
        deployment.lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=30
        )
        db.add(deployment)
        await db.commit()
        return
    if healthy:
        workspace.status = WorkspaceStatus.RUNNING
        workspace.error_message = None
        workspace.last_started_at = workspace.last_started_at or datetime.now(timezone.utc)
        deployment.status = MlflowDeploymentStatus.RUNNING
        deployment.status_message = "Çalışan model servisi controller restart sonrası benimsendi."
        deployment.error_message = None
        deployment.lease_owner = ""
        deployment.lease_expires_at = None
        db.add(workspace)
        db.add(deployment)
        await db.commit()
        return
    await delete_workspace_resources(db, workspace, allow_transient=True)
    deployment.workspace_id = None
    deployment.quota_reserved = False
    deployment.status = MlflowDeploymentStatus.QUEUED
    deployment.status_message = "Yarım kalan servis temizlendi; deployment yeniden kuyruğa alındı."
    deployment.error_message = None
    deployment.lease_owner = ""
    deployment.lease_expires_at = None
    db.add(deployment)
    await db.commit()


async def recover_interrupted_mlflow_deployments() -> None:
    async with AsyncSessionLocal() as db:
        deployments = (
            await db.execute(
                select(MlflowDeployment).where(
                    MlflowDeployment.status.in_(_ACTIVE_DEPLOYMENT_STATUSES),
                    or_(
                        MlflowDeployment.lease_expires_at.is_(None),
                        MlflowDeployment.lease_expires_at
                        < datetime.now(timezone.utc),
                    ),
                )
            )
        ).scalars().all()
        for deployment in deployments:
            await _recover_stale_deployment(db, deployment)
        await db.execute(
            update(MlflowModelBuild)
            .where(
                MlflowModelBuild.status.in_(
                    {
                        MlflowModelBuildStatus.VALIDATING,
                        MlflowModelBuildStatus.BUILDING,
                        MlflowModelBuildStatus.IMPORTING,
                    }
                ),
                or_(
                    MlflowModelBuild.lease_expires_at.is_(None),
                    MlflowModelBuild.lease_expires_at
                    < datetime.now(timezone.utc),
                ),
            )
            .values(
                status=MlflowModelBuildStatus.FAILED,
                status_message="Controller image build sırasında yeniden başladı.",
                error_message="Build yeniden denenmelidir.",
                lease_owner="",
                lease_expires_at=None,
                completed_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()


async def mlflow_deployment_background_worker(check_interval_seconds: int = 2) -> None:
    """Claim queued deployments durably; one claim can survive request disconnects."""
    while True:
        try:
            await recover_interrupted_mlflow_deployments()
            async with AsyncSessionLocal() as cleanup_db:
                await enroll_orphaned_model_builds(cleanup_db)
                cleanup_ids = list(
                    (
                        await cleanup_db.execute(
                            select(MlflowModelBuild.id)
                            .where(
                                MlflowModelBuild.status
                                == MlflowModelBuildStatus.DELETING
                            )
                            .limit(10)
                        )
                    ).scalars()
                )
                await cleanup_db.commit()
                for build_id in cleanup_ids:
                    await garbage_collect_model_build(cleanup_db, build_id)
            async with AsyncSessionLocal() as db:
                deployment_ids = list(
                    (
                        await db.execute(
                            select(MlflowDeployment.id)
                            .where(MlflowDeployment.status == MlflowDeploymentStatus.QUEUED)
                            .order_by(MlflowDeployment.created_at)
                            .limit(5)
                        )
                    ).scalars()
                )
                await db.commit()
            for deployment_id in deployment_ids:
                await process_mlflow_deployment(deployment_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MLflow deployment worker iteration failed")
        await asyncio.sleep(check_interval_seconds)
