import asyncio
import hashlib
import json
import os
import secrets
import shlex
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.auth.ldap import (
    DirectoryConfigurationError,
    DirectoryConnectionError,
    config_from_update,
    encrypt_directory_secret,
    test_directory_configuration,
    validate_directory_config,
)
from app.database import get_db
from app.download_updates import (
    DownloadUpdateDisabled,
    DownloadUpdateInProgress,
    download_update_manager,
)
from app.models.user import User
from app.models.directory_settings import DirectorySettings
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.node import Node, NodeStatus
from app.models.download_settings import DownloadSettings
from app.models.workspace_image import WorkspaceImage
from app.models.custom_template import CustomTemplate
from app.models.worker_bootstrap_ticket import WorkerBootstrapTicket
from app.agents.manager import agent_manager
from app.schemas.user import UserOut, UserQuotaUpdate
from app.schemas.directory import (
    DirectorySettingsOut,
    DirectorySettingsUpdate,
    DirectoryTestResult,
)
from app.schemas.workspace import WorkspaceOut
from app.schemas.node import NodeCreate, NodeCreated, NodeOut, NodeUpdate, NodeLabelsUpdate
from app.schemas.download_settings import DownloadSettingsOut, DownloadSettingsUpdate
from app.schemas.workspace_image import (
    WorkspaceImageOut,
    WorkspaceImageRegistryImport,
    WorkspaceImageUpdate,
)
from app.schemas.worker_bootstrap import WorkerBootstrapTicketCreated
from app.config import settings
from app.installer.platform import InstallerError
from app.installer.update_source import validate_git_source
from app.ingress_settings import (
    MAX_CERTIFICATE_BYTES,
    MAX_PRIVATE_KEY_BYTES,
    IngressApplyError,
    IngressConfigurationError,
    ingress_manager,
    normalize_https_hostname,
)
from app.orchestrator.templates import BUILTIN_TEMPLATE_IDS, TEMPLATES
from app.workspace_image_service import (
    WorkspaceImageError,
    image_archive_path,
    image_storage_root,
    import_registry_image,
    import_uploaded_archive,
)
from app.worker_bootstrap import (
    WORKER_BOOTSTRAP_TTL_SECONDS,
    controller_base_url,
    current_platform_release,
    new_ticket_token,
    ticket_hash,
)

admin_router = APIRouter(prefix="/api/admin", tags=["Admin"])


def _update_queue_root() -> Path:
    return Path(settings.UPDATE_QUEUE_ROOT).resolve()


def _read_update_status() -> dict:
    root = _update_queue_root()
    for name in ("running.json", "pending.json", "status.json"):
        path = root / name
        if path.is_file() and not path.is_symlink():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    if name == "pending.json":
                        value.setdefault("state", "queued")
                    elif name == "running.json":
                        value.setdefault("state", "running")
                    return value
            except (OSError, json.JSONDecodeError):
                return {"state": "unknown", "error": f"Cannot read {name}"}
    return {"state": "idle"}


def _worker_image_state(node: Node) -> list[dict]:
    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except ValueError:
        return []
    images = capabilities.get("workspace_images", []) if isinstance(capabilities, dict) else []
    return images if isinstance(images, list) else []


def _worker_image_progress(node: Node) -> list[dict]:
    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except ValueError:
        return []
    progress = (
        capabilities.get("workspace_image_sync", [])
        if isinstance(capabilities, dict)
        else []
    )
    return progress if isinstance(progress, list) else []


def _workspace_image_out(record: WorkspaceImage, nodes: list[Node]) -> WorkspaceImageOut:
    workers = []
    for node in nodes:
        ready = any(
            isinstance(item, dict)
            and item.get("image_ref") == record.image_ref
            and item.get("sha256") == record.sha256
            for item in _worker_image_state(node)
        )
        progress = next(
            (
                item
                for item in _worker_image_progress(node)
                if isinstance(item, dict) and item.get("id") == record.id
            ),
            {},
        )
        state = "ready" if ready else str(progress.get("state") or "pending")
        if node.status == NodeStatus.OFFLINE and not ready:
            state = "offline"
        downloaded = int(
            progress.get("downloaded_bytes") or (record.size if ready else 0)
        )
        total = int(progress.get("total_bytes") or record.size)
        workers.append(
            {
                "node_id": node.id,
                "node_name": node.name,
                "state": state,
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "percent": (
                    100.0
                    if ready
                    else round(
                        min(100.0, (downloaded / total * 100) if total else 0),
                        1,
                    )
                ),
                "error": str(progress.get("error") or "")[:500],
            }
        )
    synced = sum(1 for item in workers if item["state"] == "ready")
    return WorkspaceImageOut.model_validate(
        {
            **{column.name: getattr(record, column.name) for column in record.__table__.columns},
            "synced_workers": synced,
            "total_workers": len(nodes),
            "workers": workers,
        }
    )


async def _workspace_template(db: AsyncSession, template_id: str) -> tuple[str, str]:
    if template_id in BUILTIN_TEMPLATE_IDS:
        template = TEMPLATES[template_id]
        return template.name, template.image_tag
    custom = await db.get(CustomTemplate, template_id)
    if custom:
        return custom.name, custom.image_tag
    raise HTTPException(status_code=400, detail="Bilinmeyen workspace şablonu")


async def _register_workspace_image(
    db: AsyncSession,
    *,
    template_id: str,
    display_name: str,
    default_display_name: str,
    source_type: str,
    source_ref: str,
    metadata: dict[str, object],
) -> WorkspaceImage:
    await db.execute(
        update(WorkspaceImage)
        .where(WorkspaceImage.template_id == template_id)
        .values(enabled=False)
    )
    record = WorkspaceImage(
        id=str(metadata["id"]),
        template_id=template_id,
        display_name=display_name.strip() or default_display_name,
        image_ref=str(metadata["image_ref"]),
        source_type=source_type,
        source_ref=source_ref,
        digest=str(metadata["digest"]),
        sha256=str(metadata["sha256"]),
        filename=str(metadata["filename"]),
        size=int(metadata["size"]),
        architecture=str(metadata["architecture"]),
        enabled=True,
    )
    db.add(record)
    try:
        await db.commit()
        await db.refresh(record)
    except Exception:
        await db.rollback()
        image_archive_path(record.filename).unlink(missing_ok=True)
        raise
    return record


@admin_router.get("/workspace-images", response_model=list[WorkspaceImageOut])
async def list_workspace_images(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    records = (
        await db.execute(select(WorkspaceImage).order_by(WorkspaceImage.created_at.desc()))
    ).scalars().all()
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    return [_workspace_image_out(record, nodes) for record in records]


@admin_router.post(
    "/workspace-images/import", response_model=WorkspaceImageOut, status_code=status.HTTP_201_CREATED
)
async def import_workspace_image_from_registry(
    payload: WorkspaceImageRegistryImport,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    template_name, image_ref = await _workspace_template(db, payload.template_id)
    try:
        metadata = await asyncio.to_thread(
            import_registry_image,
            image_ref=image_ref,
            source_ref=payload.source_ref,
            username=payload.username,
            password=payload.password,
        )
        record = await _register_workspace_image(
            db,
            template_id=payload.template_id,
            display_name=payload.display_name,
            default_display_name=template_name,
            source_type="registry",
            source_ref=payload.source_ref.removeprefix("docker://"),
            metadata=metadata,
        )
    except WorkspaceImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    return _workspace_image_out(record, nodes)


@admin_router.post(
    "/workspace-images/upload", response_model=WorkspaceImageOut, status_code=status.HTTP_201_CREATED
)
async def upload_workspace_image_archive(
    template_id: Annotated[str, Form(min_length=2, max_length=64)],
    archive: Annotated[UploadFile, File()],
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    display_name: Annotated[str, Form(max_length=160)] = "",
):
    template_name, image_ref = await _workspace_template(db, template_id)
    upload_root = image_storage_root() / ".uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    upload_path = upload_root / f"{uuid.uuid4()}.upload"
    size = 0
    try:
        with upload_path.open("xb") as destination:
            while chunk := await archive.read(1024 * 1024):
                size += len(chunk)
                if size > settings.WORKSPACE_IMAGE_MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Workspace image archive is too large")
                destination.write(chunk)
        if not size:
            raise HTTPException(status_code=400, detail="Workspace image archive is empty")
        metadata = await asyncio.to_thread(
            import_uploaded_archive,
            image_ref=image_ref,
            upload_path=upload_path,
        )
        record = await _register_workspace_image(
            db,
            template_id=template_id,
            display_name=display_name,
            default_display_name=template_name,
            source_type="upload",
            source_ref=Path(archive.filename or "workspace-image.tar").name,
            metadata=metadata,
        )
    except WorkspaceImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        upload_path.unlink(missing_ok=True)
        await archive.close()
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    return _workspace_image_out(record, nodes)


@admin_router.patch("/workspace-images/{image_id}", response_model=WorkspaceImageOut)
async def update_workspace_image(
    image_id: str,
    payload: WorkspaceImageUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await db.get(WorkspaceImage, image_id)
    if not record:
        raise HTTPException(status_code=404, detail="Workspace image bulunamadı")
    if payload.enabled:
        await db.execute(
            update(WorkspaceImage)
            .where(
                WorkspaceImage.template_id == record.template_id,
                WorkspaceImage.id != record.id,
            )
            .values(enabled=False)
        )
    record.enabled = payload.enabled
    db.add(record)
    await db.commit()
    await db.refresh(record)
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    return _workspace_image_out(record, nodes)


@admin_router.delete("/workspace-images/{image_id}")
async def delete_workspace_image(
    image_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await db.get(WorkspaceImage, image_id)
    if not record:
        raise HTTPException(status_code=404, detail="Workspace image bulunamadı")
    archive_path = image_archive_path(record.filename)
    await db.delete(record)
    await db.commit()
    archive_path.unlink(missing_ok=True)
    return {"deleted": True, "image_id": image_id}


def _download_settings_out(record: DownloadSettings) -> DownloadSettingsOut:
    base_url = record.public_base_url.rstrip("/")
    return DownloadSettingsOut(
        public_base_url=base_url,
        https_enabled=record.https_enabled,
        https_hostname=record.https_hostname,
        http_fallback_enabled=record.http_fallback_enabled,
        certificate_uploaded=bool(record.certificate_sha256),
        certificate_subject=record.certificate_subject,
        certificate_not_after=record.certificate_not_after,
        certificate_sha256=record.certificate_sha256,
    )


async def _get_or_create_download_settings(db: AsyncSession) -> DownloadSettings:
    record = await db.get(DownloadSettings, 1)
    if record:
        return record
    record = DownloadSettings(
        id=1,
        public_base_url=settings.DOWNLOAD_PUBLIC_BASE_URL,
        https_hostname=settings.HTTPS_DEFAULT_HOSTNAME,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


@admin_router.get("/download-settings", response_model=DownloadSettingsOut)
async def get_download_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _download_settings_out(await _get_or_create_download_settings(db))


@admin_router.put("/download-settings", response_model=DownloadSettingsOut)
async def update_download_settings(
    update: DownloadSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await _get_or_create_download_settings(db)
    record.public_base_url = update.public_base_url
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _download_settings_out(record)


async def _read_upload(upload: UploadFile | None, maximum: int, label: str) -> bytes | None:
    if upload is None or not upload.filename:
        return None
    content = await upload.read(maximum + 1)
    if len(content) > maximum:
        raise HTTPException(
            status_code=413,
            detail=f"{label} izin verilen dosya boyutunu aşıyor.",
        )
    if not content:
        raise HTTPException(status_code=422, detail=f"{label} boş olamaz.")
    return content


@admin_router.post("/download-settings/https", response_model=DownloadSettingsOut)
async def apply_https_settings(
    https_enabled: Annotated[bool, Form()],
    https_hostname: Annotated[str, Form()],
    http_fallback_enabled: Annotated[bool, Form()],
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    certificate: Annotated[UploadFile | None, File()] = None,
    private_key: Annotated[UploadFile | None, File()] = None,
):
    record = await _get_or_create_download_settings(db)
    certificate_pem = await _read_upload(
        certificate, MAX_CERTIFICATE_BYTES, "Sertifika"
    )
    private_key_pem = await _read_upload(
        private_key, MAX_PRIVATE_KEY_BYTES, "Private key"
    )
    try:
        hostname = normalize_https_hostname(https_hostname)
        effective_http_fallback = (
            http_fallback_enabled if https_enabled else True
        )
        info = await ingress_manager.apply(
            https_enabled=https_enabled,
            hostname=hostname,
            http_fallback_enabled=effective_http_fallback,
            certificate_pem=certificate_pem,
            private_key_pem=private_key_pem,
        )
    except IngressConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IngressApplyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"HTTPS ayar dosyaları yazılamadı: {exc}",
        ) from exc

    record.https_enabled = https_enabled
    record.https_hostname = hostname
    record.http_fallback_enabled = effective_http_fallback
    record.public_base_url = (
        f"{'https' if https_enabled else 'http'}://{hostname}"
    )
    if info:
        record.certificate_subject = info.subject
        record.certificate_not_after = info.not_after
        record.certificate_sha256 = info.sha256
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _download_settings_out(record)


def _node_out(node: Node, enrollment_token: str | None = None):
    values = dict(
        id=node.id,
        name=node.name,
        hostname=node.hostname,
        enabled=node.enabled,
        schedulable=node.schedulable,
        status=node.status,
        cpu_total=node.cpu_total,
        memory_total_mb=node.memory_total_mb,
        disk_total_mb=node.disk_total_mb,
        cpu_percent=node.cpu_percent,
        memory_used_mb=node.memory_used_mb,
        disk_used_mb=node.disk_used_mb,
        active_containers_count=node.active_containers_count,
        labels=json.loads(node.labels_json or "{}"),
        capabilities=json.loads(node.capabilities_json or "{}"),
        inventory=json.loads(node.inventory_json or "[]"),
        reconciliation=json.loads(node.reconciliation_json or "{}"),
        agent_version=node.agent_version,
        last_seen_at=node.last_seen_at,
        created_at=node.created_at,
        connected=agent_manager.is_connected(node.id),
    )
    if enrollment_token is not None:
        return NodeCreated(**values, enrollment_token=enrollment_token)
    return NodeOut(**values)


@admin_router.get("/nodes", response_model=list[NodeOut])
async def list_nodes(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    return [_node_out(node) for node in nodes]


@admin_router.post(
    "/worker-bootstrap-tickets",
    response_model=WorkerBootstrapTicketCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_worker_bootstrap_ticket(
    request: Request,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a short-lived command that may enroll exactly one worker."""
    current_platform_release()
    token = new_ticket_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=WORKER_BOOTSTRAP_TTL_SECONDS
    )
    db.add(
        WorkerBootstrapTicket(
            token_hash=ticket_hash(token),
            created_by_user_id=str(_admin.id),
            expires_at=expires_at,
        )
    )
    await db.commit()
    base_url = await controller_base_url(request, db)
    install_url = f"{base_url}/api/bootstrap/workers/{token}/install.sh"
    return WorkerBootstrapTicketCreated(
        install_url=install_url,
        command=f"curl -fsSL {shlex.quote(install_url)} | sudo bash",
        expires_at=expires_at,
    )


@admin_router.post("/nodes", response_model=NodeCreated, status_code=status.HTTP_201_CREATED)
async def create_node(
    data: NodeCreate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    existing = (await db.execute(select(Node).where(Node.name == data.name))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Bu isimde bir worker zaten var.")
    token = secrets.token_urlsafe(32)
    node = Node(
        name=data.name,
        schedulable=data.schedulable,
        labels_json=json.dumps(data.labels, ensure_ascii=False),
        agent_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        status=NodeStatus.PENDING,
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return _node_out(node, token)


@admin_router.patch("/nodes/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: str,
    data: NodeUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")
    values = data.model_dump(exclude_unset=True)
    labels = values.pop("labels", None)
    for field_name, value in values.items():
        setattr(node, field_name, value)
    if labels is not None:
        node.labels_json = json.dumps(labels, ensure_ascii=False)
    if not node.schedulable and node.status == NodeStatus.ONLINE:
        node.status = NodeStatus.DRAINING
    elif node.schedulable and agent_manager.is_connected(node.id):
        node.status = NodeStatus.ONLINE
    db.add(node)
    await db.commit()
    await db.refresh(node)
    if not node.enabled:
        await agent_manager.disconnect(node.id, "Worker yönetici tarafından devre dışı bırakıldı")
    return _node_out(node)


@admin_router.post("/nodes/{node_id}/rotate-token", response_model=NodeCreated)
async def rotate_node_token(
    node_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")
    token = secrets.token_urlsafe(32)
    node.agent_token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    db.add(node)
    await db.commit()
    await db.refresh(node)
    await agent_manager.disconnect(node.id, "Worker enrollment token'ı yenilendi")
    return _node_out(node, token)


@admin_router.delete("/nodes/{node_id}")
async def delete_node(
    node_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Delete a worker node from the cluster."""
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")

    assigned_workspaces = (
        await db.execute(
            select(func.count(Workspace.id)).where(
                Workspace.node_id == node_id,
            )
        )
    ).scalar_one()
    if assigned_workspaces > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Bu worker'a atanmış {assigned_workspaces} çalışma alanı var. "
                "Worker'ı silmeden önce çalışma alanlarını başka bir worker'a taşıyın veya silin."
            ),
        )

    await agent_manager.disconnect(node.id, "Worker sistemden silindi")
    await db.delete(node)
    await db.commit()
    return {"message": f"Worker '{node.name}' başarıyla silindi."}


@admin_router.get("/nodes/events-stream")
async def node_events_stream(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Server-Sent Events stream for real-time node telemetry and connection events."""
    from fastapi.responses import StreamingResponse
    queue = agent_manager.subscribe_events()

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'init', 'data': {'message': 'connected'}})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            agent_manager.unsubscribe_events(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@admin_router.put("/nodes/{node_id}/labels", response_model=NodeOut)
async def update_node_labels(
    node_id: str,
    data: NodeLabelsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Update label annotations for a worker node."""
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")
    node.labels_json = json.dumps(data.labels, ensure_ascii=False)
    db.add(node)
    await db.commit()
    await db.refresh(node)
    await agent_manager.broadcast_event(
        "node.updated", {"node_id": node.id, "labels": data.labels}
    )
    return _node_out(node)


@admin_router.post("/nodes/{node_id}/upgrade")
async def upgrade_node(
    node_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Trigger remote OTA upgrade for a connected worker."""
    node = await db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Worker bulunamadı.")
    if not agent_manager.is_connected(node.id):
        raise HTTPException(
            status_code=400, detail="Worker çevrimdışı; yükseltme komutu gönderilemez."
        )
    connection = agent_manager.get(node.id)
    try:
        resp = await connection.request("system.upgrade", {}, timeout=15)
        return {
            "message": f"Worker '{node.name}' yükseltme işlemi başlatıldı.",
            "detail": resp,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Yükseltme komutu başarısız oldu: {exc}"
        )


def _directory_settings_out(record: DirectorySettings) -> DirectorySettingsOut:
    return DirectorySettingsOut(
        enabled=record.enabled,
        server_host=record.server_host,
        server_port=record.server_port,
        use_ssl=record.use_ssl,
        validate_tls=record.validate_tls,
        ca_cert_file=record.ca_cert_file,
        connect_timeout_seconds=record.connect_timeout_seconds,
        bind_dn=record.bind_dn,
        has_bind_password=bool(record.encrypted_bind_password),
        user_base_dn=record.user_base_dn,
        user_filter=record.user_filter,
        username_attribute=record.username_attribute,
        email_attribute=record.email_attribute,
        display_name_attribute=record.display_name_attribute,
        group_membership_attribute=record.group_membership_attribute,
        required_group_dn=record.required_group_dn,
        admin_group_dn=record.admin_group_dn,
        nested_group_search=record.nested_group_search,
    )


async def _get_or_create_directory_settings(
    db: AsyncSession,
) -> DirectorySettings:
    record = await db.get(DirectorySettings, 1)
    if record:
        return record
    record = DirectorySettings(id=1)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


@admin_router.get("/directory-settings", response_model=DirectorySettingsOut)
async def get_directory_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Return LDAP configuration without returning the bind password."""
    return _directory_settings_out(await _get_or_create_directory_settings(db))


@admin_router.put("/directory-settings", response_model=DirectorySettingsOut)
async def update_directory_settings(
    update: DirectorySettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Store LDAP configuration and encrypt the bind password at rest."""
    record = await _get_or_create_directory_settings(db)
    try:
        candidate = config_from_update(update, record)
        if update.enabled:
            validate_directory_config(candidate)
    except DirectoryConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    values = update.model_dump(exclude={"bind_password"})
    for field_name, value in values.items():
        setattr(record, field_name, value)
    if update.bind_password:
        record.encrypted_bind_password = encrypt_directory_secret(
            update.bind_password
        )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _directory_settings_out(record)


@admin_router.post("/directory-settings/test", response_model=DirectoryTestResult)
async def test_directory_settings(
    update: DirectorySettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Test the submitted (or saved) bind credentials and search base."""
    record = await _get_or_create_directory_settings(db)
    try:
        candidate = config_from_update(update, record)
        message, elapsed_ms = await asyncio.to_thread(
            test_directory_configuration, candidate
        )
    except (DirectoryConfigurationError, DirectoryConnectionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    scheme = "ldaps" if candidate.use_ssl else "ldap"
    return DirectoryTestResult(
        success=True,
        message=message,
        server=f"{scheme}://{candidate.server_host}:{candidate.server_port}",
        response_time_ms=elapsed_ms,
    )


@admin_router.get("/users", response_model=list[UserOut])
async def list_all_users(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: List all registered users."""
    stmt = select(User).order_by(User.id.asc())
    result = await db.execute(stmt)
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@admin_router.put("/users/{user_id}/quota", response_model=UserOut)
async def update_user_quota(
    user_id: int,
    quota: UserQuotaUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Update CPU, RAM, and persistent-disk quota for one user."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")
    user.cpu_quota = quota.cpu_quota
    user.memory_mb_quota = quota.memory_mb_quota
    user.disk_mb_quota = quota.disk_mb_quota
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@admin_router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_all_workspaces(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: List all workspaces across all users."""
    stmt = select(Workspace).order_by(Workspace.created_at.desc())
    result = await db.execute(stmt)
    return [WorkspaceOut.model_validate(ws) for ws in result.scalars().all()]


@admin_router.post("/workspaces/{workspace_id}/migrate")
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
        "runtime_mode": "worker-only",
    }


@admin_router.get("/templates")
async def list_custom_templates(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: List all custom database templates."""
    from app.models.custom_template import CustomTemplate
    stmt = select(CustomTemplate).order_by(CustomTemplate.created_at.desc())
    res = await db.execute(stmt)
    return res.scalars().all()


@admin_router.delete("/templates/{template_id}")
async def delete_custom_template(
    template_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Delete a custom template from database."""
    from app.models.custom_template import CustomTemplate
    stmt = select(CustomTemplate).where(CustomTemplate.id == template_id)
    res = await db.execute(stmt)
    tpl = res.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found.")
    await db.delete(tpl)
    await db.commit()
    return {"message": f"Template {template_id} deleted successfully."}


@admin_router.post("/templates/build-stream")
async def build_custom_template_stream(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str, Form()],
    template_id: Annotated[str, Form()],
    containerfile: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "Custom",
    default_port: Annotated[int, Form()] = 8080,
    ide_type: Annotated[str, Form()] = "vscode",
):
    """Admin: Build a new Containerfile and register as a template with SSE logs."""
    import json
    import asyncio
    from fastapi.responses import StreamingResponse
    from app.models.custom_template import CustomTemplate

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit(text: str, level: str = "info"):
        payload = json.dumps({"type": "log", "level": level, "text": text}, ensure_ascii=False)
        await queue.put(f"data: {payload}\n\n")

    async def run_builder():
        try:
            from app.orchestrator.flavors import get_flavor
            from app.orchestrator.scheduler import select_worker_node

            if not settings.DEVCLOUD_REGISTRY_URL and not settings.USE_MOCK_PODMAN:
                raise RuntimeError(
                    "Custom image builds require DEVCLOUD_REGISTRY_URL so every "
                    "worker can pull the result."
                )
            prefix = settings.DEVCLOUD_REGISTRY_URL.rstrip("/")
            image_tag = (
                f"{prefix}/devcloud-{template_id}:latest"
                if prefix
                else f"localhost/devcloud-{template_id}:latest"
            )
            await emit(f"🔨 Starting build for [{image_tag}]...", "info")
            flavor = get_flavor("t1.nano")
            if flavor is None:
                raise RuntimeError("Build worker resource profile is missing.")
            node = await select_worker_node(db, flavor)
            await emit(f"Build worker selected: {node.name}", "info")
            result = await agent_manager.get(node.id).request(
                "image.build",
                {
                    "containerfile": containerfile,
                    "image_tag": image_tag,
                    "push": bool(prefix),
                },
                timeout=900,
            )
            if not result.get("success"):
                err_payload = json.dumps({"type": "error", "text": f"Build failed:\n{result.get('logs', '')}"})
                await queue.put(f"data: {err_payload}\n\n")
                return

            # Save in database
            custom_tpl = CustomTemplate(
                id=template_id.strip().lower(),
                name=name.strip(),
                description=description.strip(),
                category=category.strip() or "Custom",
                icon="cube",
                image_tag=image_tag,
                default_port=default_port,
                ide_type=ide_type,
                containerfile=containerfile,
                is_ready=True,
            )
            db.add(custom_tpl)
            await db.commit()

            done_payload = json.dumps({"type": "done", "template_id": template_id, "image_tag": image_tag})
            await queue.put(f"data: {done_payload}\n\n")
        except Exception as exc:
            err_payload = json.dumps({"type": "error", "text": f"Unexpected error: {str(exc)}"})
            await queue.put(f"data: {err_payload}\n\n")
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(run_builder())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
        await task

    return StreamingResponse(stream(), media_type="text/event-stream")


@admin_router.get("/system/update-info")
async def get_system_update_info(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Get the current checkout revision and application version."""
    import subprocess
    from app.config import settings

    if not settings.UPDATES_ENABLED:
        return {
            "commit": "image",
            "branch": "container",
            "status": "Container updates are managed by the host installer.",
            "version": settings.APP_VERSION,
            "update_source_type": settings.UPDATE_SOURCE_TYPE,
            "update_source": settings.UPDATE_SOURCE,
            "update_ref": settings.UPDATE_REF,
        }
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=settings.BASE_DIR,
            text=True,
            timeout=5,
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=settings.BASE_DIR,
            text=True,
            timeout=5,
        ).strip()
        return {
            "commit": commit,
            "branch": branch,
            "status": "Hazır",
            "version": settings.APP_VERSION,
            "update_source_type": settings.UPDATE_SOURCE_TYPE,
            "update_source": settings.UPDATE_SOURCE,
            "update_ref": settings.UPDATE_REF,
        }
    except Exception as exc:
        return {
            "commit": "unknown",
            "branch": "unknown",
            "status": str(exc),
            "version": settings.APP_VERSION,
            "update_source_type": settings.UPDATE_SOURCE_TYPE,
            "update_source": settings.UPDATE_SOURCE,
            "update_ref": settings.UPDATE_REF,
        }


@admin_router.get("/system/release-upload/status")
async def get_release_upload_status(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Return the root-owned queued updater's durable status."""
    if not settings.UPDATES_ENABLED:
        raise HTTPException(status_code=503, detail="Release updates are disabled.")
    return _read_update_status()


@admin_router.post("/system/release-source", status_code=status.HTTP_202_ACCEPTED)
async def queue_git_release_update(
    repository: Annotated[str, Form()],
    ref: Annotated[str, Form()],
    _admin: Annotated[User, Depends(get_current_admin_user)],
    allow_unsigned: Annotated[bool, Form()] = False,
):
    """Queue a platform release selected through a Git channel file."""
    if not settings.UPDATES_ENABLED:
        raise HTTPException(status_code=503, detail="Release updates are disabled.")
    try:
        repository, ref = validate_git_source(repository, ref)
    except InstallerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    root = _update_queue_root()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "pending.json").exists() or (root / "running.json").exists():
        raise HTTPException(
            status_code=409,
            detail="Another release update is already queued or running.",
        )
    request = {
        "state": "queued",
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "source_type": "git",
        "repository": repository,
        "ref": ref,
        "filename": f"{repository}@{ref}",
        "allow_unsigned": allow_unsigned,
    }
    marker_tmp = root / "pending.tmp"
    marker_tmp.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    os.chmod(marker_tmp, 0o600)
    os.replace(marker_tmp, root / "pending.json")
    return {
        "state": "queued",
        "source_type": "git",
        "repository": repository,
        "ref": ref,
        "allow_unsigned": allow_unsigned,
    }


@admin_router.post(
    "/system/release-upload",
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_release_update(
    release: Annotated[UploadFile, File()],
    _admin: Annotated[User, Depends(get_current_admin_user)],
    allow_unsigned: Annotated[bool, Form()] = False,
):
    """Stage a platform release for the root-owned systemd updater."""
    if not settings.UPDATES_ENABLED:
        raise HTTPException(status_code=503, detail="Release updates are disabled.")
    filename = Path(release.filename or "").name
    if not filename.lower().endswith((".zip", ".tar", ".tar.gz", ".tgz")):
        raise HTTPException(
            status_code=422,
            detail="Release must be a ZIP, tar, tar.gz, or tgz archive.",
        )
    root = _update_queue_root()
    uploads = root / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    uploads.mkdir(parents=True, exist_ok=True)
    if (root / "pending.json").exists() or (root / "running.json").exists():
        raise HTTPException(
            status_code=409,
            detail="Another release update is already queued or running.",
        )
    suffix = "".join(Path(filename).suffixes[-2:]) or ".release"
    destination = uploads / f"{uuid.uuid4().hex}{suffix}"
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as handle:
            while chunk := await release.read(1024 * 1024):
                size += len(chunk)
                if size > settings.UPDATE_MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Release upload exceeds the configured size limit.",
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if not size:
            raise HTTPException(status_code=422, detail="Release archive is empty.")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        request = {
            "state": "queued",
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "source_type": "bundle",
            "filename": filename,
            "bundle": str(destination),
            "size": size,
            "sha256": digest.hexdigest(),
            "allow_unsigned": allow_unsigned,
        }
        marker_tmp = root / "pending.tmp"
        marker_tmp.write_text(
            json.dumps(request, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(marker_tmp, 0o600)
        os.replace(marker_tmp, root / "pending.json")
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    return {
        "state": "queued",
        "filename": filename,
        "size": size,
        "sha256": digest.hexdigest(),
        "allow_unsigned": allow_unsigned,
    }


@admin_router.post("/system/update-stream")
async def run_system_update_stream(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Run the guarded updater and stream its combined output."""
    import asyncio
    import json
    from pathlib import Path
    from fastapi.responses import StreamingResponse
    from app.config import settings
    from deploy.package_offline import get_app_version

    if not settings.UPDATES_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Container updates are managed by the host installer.",
        )

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit(text: str, level: str = "info"):
        payload = json.dumps({"type": "log", "level": level, "text": text}, ensure_ascii=False)
        await queue.put(f"data: {payload}\n\n")

    async def run_updater():
        try:
            project_dir = Path(settings.BASE_DIR).resolve()
            update_script = project_dir / "deploy" / "update.sh"
            if not update_script.is_file():
                raise FileNotFoundError(f"Güncelleme betiği bulunamadı: {update_script}")

            await emit("DevCloud platform güncellemesi başlatıldı.", "info")
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(update_script),
                cwd=str(project_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if proc.stdout is None:
                raise RuntimeError("Güncelleme çıktısı okunamadı.")

            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    await emit(line, "info")

            return_code = await proc.wait()
            if return_code != 0:
                error_payload = json.dumps(
                    {
                        "type": "error",
                        "text": f"Güncelleme başarısız oldu (çıkış kodu: {return_code}).",
                    },
                    ensure_ascii=False,
                )
                await queue.put(f"data: {error_payload}\n\n")
                return

            commit_proc = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--short",
                "HEAD",
                cwd=str(project_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            commit_stdout, _ = await commit_proc.communicate()
            commit = commit_stdout.decode("utf-8", errors="replace").strip() or "unknown"
            version = get_app_version(project_dir)
            done_payload = json.dumps(
                {
                    "type": "done",
                    "text": "Güncelleme tamamlandı; servis doğrulanıyor...",
                    "commit": commit,
                    "version": version,
                },
                ensure_ascii=False,
            )
            await queue.put(f"data: {done_payload}\n\n")
        except Exception as exc:
            err_payload = json.dumps(
                {"type": "error", "text": f"Güncelleme hatası: {str(exc)}"},
                ensure_ascii=False,
            )
            await queue.put(f"data: {err_payload}\n\n")
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(run_updater())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
        await task

    return StreamingResponse(stream(), media_type="text/event-stream")


@admin_router.get("/downloads/status")
async def get_download_update_status(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Return durable status for the offline download publisher."""
    return download_update_manager.get_status()


@admin_router.post("/downloads/update", status_code=status.HTTP_202_ACCEPTED)
async def start_download_update(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Build and atomically publish the current offline bundle."""
    try:
        return download_update_manager.start()
    except DownloadUpdateDisabled as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except DownloadUpdateInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@admin_router.get("/downloads/{bundle_role}/status")
async def get_role_download_update_status(
    bundle_role: Literal["server", "worker"],
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Return durable status for one offline bundle role."""
    return download_update_manager.get_status(bundle_role)


@admin_router.post(
    "/downloads/{bundle_role}/update",
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_role_download_update(
    bundle_role: Literal["server", "worker"],
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Build and atomically publish one offline bundle role."""
    try:
        return download_update_manager.start(bundle_role)
    except DownloadUpdateDisabled as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except DownloadUpdateInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@admin_router.post("/downloads/clean")
async def clean_old_downloads(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Remove older offline bundles and temporary files to reclaim disk space."""
    try:
        return download_update_manager.clean_old_bundles()
    except DownloadUpdateInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
