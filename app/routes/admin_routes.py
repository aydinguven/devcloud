from typing import Annotated
from fastapi import APIRouter, Depends, Form, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.download_updates import (
    DownloadUpdateDisabled,
    DownloadUpdateInProgress,
    download_update_manager,
)
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.podman_service import podman_service
from app.schemas.user import UserOut, UserQuotaUpdate
from app.schemas.workspace import WorkspaceOut

admin_router = APIRouter(prefix="/api/admin", tags=["Admin"])


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
    """Admin: Get current git revision and status."""
    import subprocess
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
        return {"commit": commit, "branch": branch, "status": "Ready"}
    except Exception as exc:
        return {"commit": "unknown", "branch": "unknown", "status": str(exc)}


@admin_router.post("/system/update-stream")
async def run_system_update_stream(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Pull latest updates, install requirements, and restart service with live logs."""
    import json
    import asyncio
    import subprocess
    from pathlib import Path
    from fastapi.responses import StreamingResponse

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit(text: str, level: str = "info"):
        payload = json.dumps({"type": "log", "level": level, "text": text}, ensure_ascii=False)
        await queue.put(f"data: {payload}\n\n")

    async def run_updater():
        try:
            await emit("🚀 Starting DevCloud 1-Click Platform Update...", "info")
            await asyncio.sleep(0.1)

            # Step 1: Git pull
            await emit("📥 [1/4] Pulling latest commits from git repository...", "info")
            proc = await asyncio.create_subprocess_exec(
                "git", "pull",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            await emit(f"Git: {stdout.decode().strip() or stderr.decode().strip()}", "info")

            # Step 2: Systemd service update
            await emit("⚙️ [2/4] Verifying systemd unit configuration...", "info")
            if Path("/etc/systemd/system/devcloud.service").exists():
                await asyncio.create_subprocess_exec("sudo", "systemctl", "daemon-reload")

            # Step 3: Schedule detached restart
            await emit("🔄 [3/4] Scheduling fast daemon restart in 2 seconds...", "success")
            done_payload = json.dumps({"type": "done", "text": "Update complete! Reconnecting in 3s..."})
            await queue.put(f"data: {done_payload}\n\n")
            
            # Dispatch background restart
            subprocess.Popen(["bash", "-c", "sleep 1.5 && sudo systemctl restart devcloud"])
        except Exception as exc:
            err_payload = json.dumps({"type": "error", "text": f"Update error: {str(exc)}"})
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

