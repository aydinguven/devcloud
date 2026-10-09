"""Admin API: managed workspace images and their worker sync state."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.models.node import Node, NodeStatus
from app.models.workspace_image import WorkspaceImage
from app.models.mlflow_deployment import MlflowModelBuild
from app.models.custom_template import CustomTemplate
from app.schemas.workspace_image import (
    WorkspaceImageOut,
    WorkspaceImageRegistryImport,
    WorkspaceImageUpdate,
)
from app.config import settings
from app.orchestrator.templates import (
    BUILTIN_TEMPLATE_IDS,
    TEMPLATES,
    register_custom_template,
)
from app.workspace_image_service import (
    WorkspaceImageError,
    image_archive_path,
    image_storage_root,
    import_registry_image,
    import_uploaded_archive,
)

router = APIRouter()


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


async def register_workspace_image(
    db: AsyncSession,
    *,
    template_id: str,
    display_name: str,
    default_display_name: str,
    source_type: str,
    source_ref: str,
    metadata: dict[str, object],
    custom_template: CustomTemplate | None = None,
) -> WorkspaceImage:
    previous_images = (
        await db.execute(
            select(WorkspaceImage).where(WorkspaceImage.template_id == template_id)
        )
    ).scalars().all()
    for previous in previous_images:
        if not await _workspace_image_in_use(db, previous.id):
            previous.enabled = False
            db.add(previous)
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
    if custom_template is not None:
        db.add(custom_template)
    db.add(record)
    try:
        await db.commit()
        await db.refresh(record)
    except Exception:
        await db.rollback()
        image_archive_path(record.filename).unlink(missing_ok=True)
        raise
    return record


@router.get("/workspace-images", response_model=list[WorkspaceImageOut])
async def list_workspace_images(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    records = (
        await db.execute(select(WorkspaceImage).order_by(WorkspaceImage.created_at.desc()))
    ).scalars().all()
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    return [_workspace_image_out(record, nodes) for record in records]


@router.post(
    "/workspace-images/import", response_model=WorkspaceImageOut, status_code=status.HTTP_201_CREATED
)
async def import_workspace_image_from_registry(
    payload: WorkspaceImageRegistryImport,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    custom_template = None
    if payload.new_template is not None:
        definition = payload.new_template
        if definition.id in BUILTIN_TEMPLATE_IDS or await db.get(CustomTemplate, definition.id):
            raise HTTPException(status_code=409, detail="Bu şablon ID zaten kullanılıyor")
        image_ref = f"localhost/devcloud-{definition.id}:latest"
        template_name = definition.name
        custom_template = CustomTemplate(
            id=definition.id,
            name=definition.name,
            description=definition.description,
            category=definition.category,
            icon="cube",
            image_tag=image_ref,
            default_port=definition.default_port,
            ide_type=definition.ide_type,
            containerfile=f"FROM {payload.source_ref.removeprefix('docker://')}",
            is_ready=True,
        )
    else:
        template_name, image_ref = await _workspace_template(db, payload.template_id)
    try:
        metadata = await asyncio.to_thread(
            import_registry_image,
            image_ref=image_ref,
            source_ref=payload.source_ref,
            username=payload.username,
            password=payload.password,
        )
        record = await register_workspace_image(
            db,
            template_id=payload.template_id,
            display_name=payload.display_name,
            default_display_name=template_name,
            source_type="registry",
            source_ref=payload.source_ref.removeprefix("docker://"),
            metadata=metadata,
            custom_template=custom_template,
        )
    except WorkspaceImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Bu şablon ID zaten kullanılıyor") from exc
    if custom_template is not None:
        register_custom_template(
            template_id=custom_template.id,
            name=custom_template.name,
            description=custom_template.description,
            category=custom_template.category,
            image_tag=custom_template.image_tag,
            default_port=custom_template.default_port,
            ide_type=custom_template.ide_type,
            icon=custom_template.icon,
        )
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    return _workspace_image_out(record, nodes)


@router.post(
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
        record = await register_workspace_image(
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


async def _workspace_image_in_use(db: AsyncSession, image_id: str) -> bool:
    workspace_count = (
        await db.execute(
            select(func.count(Workspace.id)).where(Workspace.image_id == image_id)
        )
    ).scalar_one()
    build_count = (
        await db.execute(
            select(func.count(MlflowModelBuild.id)).where(
                MlflowModelBuild.workspace_image_id == image_id
            )
        )
    ).scalar_one()
    return bool(workspace_count or build_count)


@router.patch("/workspace-images/{image_id}", response_model=WorkspaceImageOut)
async def update_workspace_image(
    image_id: str,
    payload: WorkspaceImageUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await db.get(WorkspaceImage, image_id)
    if not record:
        raise HTTPException(status_code=404, detail="Workspace image bulunamadı")
    if not payload.enabled and await _workspace_image_in_use(db, record.id):
        raise HTTPException(
            status_code=409,
            detail="Aktif workspace veya model build tarafından kullanılan image devre dışı bırakılamaz.",
        )
    if payload.enabled:
        previous_images = (
            await db.execute(
                select(WorkspaceImage).where(
                    WorkspaceImage.template_id == record.template_id,
                    WorkspaceImage.id != record.id,
                )
            )
        ).scalars().all()
        for previous in previous_images:
            if not await _workspace_image_in_use(db, previous.id):
                previous.enabled = False
                db.add(previous)
    record.enabled = payload.enabled
    db.add(record)
    await db.commit()
    await db.refresh(record)
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()
    return _workspace_image_out(record, nodes)


@router.delete("/workspace-images/{image_id}")
async def delete_workspace_image(
    image_id: str,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await db.get(WorkspaceImage, image_id)
    if not record:
        raise HTTPException(status_code=404, detail="Workspace image bulunamadı")
    if await _workspace_image_in_use(db, record.id):
        raise HTTPException(
            status_code=409,
            detail="Aktif workspace veya model build tarafından kullanılan image silinemez.",
        )
    archive_path = image_archive_path(record.filename)
    await db.delete(record)
    await db.commit()
    archive_path.unlink(missing_ok=True)
    return {"deleted": True, "image_id": image_id}
