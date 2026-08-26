import asyncio
import hashlib
import json
import secrets
from typing import Annotated
from fastapi import APIRouter, Depends, Form, HTTPException, status
from sqlalchemy import select, func
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
from app.models.mlflow_settings import MlflowSettings
from app.agents.manager import agent_manager
from app.orchestrator.podman_service import podman_service
from app.schemas.user import UserOut, UserQuotaUpdate
from app.schemas.directory import (
    DirectorySettingsOut,
    DirectorySettingsUpdate,
    DirectoryTestResult,
)
from app.schemas.workspace import WorkspaceOut
from app.schemas.node import NodeCreate, NodeCreated, NodeOut, NodeUpdate
from app.schemas.mlflow import MlflowSettingsOut, MlflowSettingsUpdate, MlflowTestResult
from app.security.secrets import encrypt_secret
from app.integrations.mlflow import (
    MlflowClient,
    MlflowConfigurationError,
    MlflowConnectionError,
    config_from_update as mlflow_config_from_update,
    validate_config as validate_mlflow_config,
)

admin_router = APIRouter(prefix="/api/admin", tags=["Admin"])


def _mlflow_settings_out(record: MlflowSettings) -> MlflowSettingsOut:
    return MlflowSettingsOut(
        enabled=record.enabled,
        base_url=record.base_url,
        auth_type=record.auth_type,
        username=record.username,
        has_secret=bool(record.encrypted_secret),
        validate_tls=record.validate_tls,
        ca_cert_file=record.ca_cert_file,
        timeout_seconds=record.timeout_seconds,
    )


async def _get_or_create_mlflow_settings(db: AsyncSession) -> MlflowSettings:
    record = await db.get(MlflowSettings, 1)
    if record:
        return record
    record = MlflowSettings(id=1)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


@admin_router.get("/mlflow-settings", response_model=MlflowSettingsOut)
async def get_mlflow_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _mlflow_settings_out(await _get_or_create_mlflow_settings(db))


@admin_router.put("/mlflow-settings", response_model=MlflowSettingsOut)
async def update_mlflow_settings(
    update: MlflowSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await _get_or_create_mlflow_settings(db)
    try:
        candidate = mlflow_config_from_update(update, record)
        if update.enabled:
            validate_mlflow_config(candidate)
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for field_name, value in update.model_dump(exclude={"secret"}).items():
        setattr(record, field_name, value)
    if update.secret:
        record.encrypted_secret = encrypt_secret(update.secret)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _mlflow_settings_out(record)


@admin_router.post("/mlflow-settings/test", response_model=MlflowTestResult)
async def test_mlflow_settings(
    update: MlflowSettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await _get_or_create_mlflow_settings(db)
    try:
        candidate = mlflow_config_from_update(update, record)
        validate_mlflow_config(candidate)
        count, elapsed_ms = await MlflowClient(candidate).test()
    except MlflowConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MlflowConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return MlflowTestResult(
        success=True,
        message="MLflow Model Registry bağlantısı başarılı.",
        response_time_ms=elapsed_ms,
        model_count=count,
    )


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
        labels=json.loads(node.labels_json or "{}"),
        capabilities=json.loads(node.capabilities_json or "{}"),
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
            image_tag = f"localhost/devcloud-{template_id}:latest"
            await emit(f"🔨 Starting build for [{image_tag}]...", "info")

            success, logs = await podman_service.build_image_from_content(
                containerfile_content=containerfile,
                image_tag=image_tag,
                progress_callback=emit,
            )

            if not success:
                err_payload = json.dumps({"type": "error", "text": f"Build failed:\n{logs}"})
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
        }
    except Exception as exc:
        return {
            "commit": "unknown",
            "branch": "unknown",
            "status": str(exc),
            "version": settings.APP_VERSION,
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


@admin_router.post("/downloads/clean")
async def clean_old_downloads(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Remove older offline bundles and temporary files to reclaim disk space."""
    return download_update_manager.clean_old_bundles()
