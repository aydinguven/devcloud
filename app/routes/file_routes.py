import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from app.config import settings
from app.agents.transfers import CHUNK_BYTES, upload_transfer, download_chunks, abort_transfer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.manager import AgentCommandError, AgentUnavailable, agent_manager
from app.auth.dependencies import get_current_user
from app.database import get_db, release_read_only_connection
from app.models.user import User, UserRole
from app.models.workspace import Workspace
from app.orchestrator.metrics_service import format_bytes_human


file_router = APIRouter(
    prefix="/api/workspaces/{workspace_id}/files", tags=["Files"]
)


async def get_accessible_workspace(
    workspace_id: str,
    current_user: User,
    db: AsyncSession,
) -> Workspace:
    workspace = (
        await db.execute(select(Workspace).where(Workspace.id == workspace_id))
    ).scalar_one_or_none()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")
    if not workspace.node_id or not workspace.storage_path:
        raise HTTPException(
            status_code=409,
            detail="Workspace worker placement or storage is not initialized.",
        )
    await release_read_only_connection(db)
    return workspace


async def _worker_request(
    workspace: Workspace,
    action: str,
    payload: dict,
    *,
    timeout: float = 60,
) -> dict:
    try:
        return await agent_manager.get(workspace.node_id).request(
            action,
            {"container_name": workspace.container_name, **payload},
            timeout=timeout,
        )
    except (AgentUnavailable, AgentCommandError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@file_router.get("")
async def list_files(
    workspace_id: str,
    path: str = Query("", description="Relative directory path"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await get_accessible_workspace(workspace_id, current_user, db)
    result = await _worker_request(workspace, "files.list", {"path": path})
    for item in result.get("items") or []:
        timestamp = item.pop("modified_timestamp", None)
        item["size_display"] = (
            format_bytes_human(item.get("size_bytes", 0))
            if not item.get("is_dir")
            else "--"
        )
        item["modified_at"] = (
            datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M"
            )
            if timestamp
            else "—"
        )
    return result


@file_router.post("/upload")
async def upload_files(
    workspace_id: str,
    path: Annotated[str, Form()] = "",
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await get_accessible_workspace(workspace_id, current_user, db)
    try:
        connection = agent_manager.get(workspace.node_id)
    except AgentUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    uploaded = []
    total = 0
    for file in files:
        if file.size is not None:
            total += file.size
    if total > settings.FILE_TRANSFER_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Upload exceeds file transfer limit.")
    remaining = settings.FILE_TRANSFER_MAX_BYTES
    for file in files:
        filename = Path(file.filename or "").name
        if not filename or filename in {".", ".."}:
            raise HTTPException(status_code=400, detail="Invalid filename")
        async def chunks():
            while chunk := await file.read(CHUNK_BYTES):
                yield chunk
        try:
            result = await upload_transfer(connection, chunks(), payload={
                "purpose": "upload", "container_name": workspace.container_name,
                "path": str(Path(path) / filename),
            }, limit=remaining)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except (AgentCommandError, AgentUnavailable, TimeoutError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        remaining -= result["size"]
        uploaded.append(result["name"])
    return {"message": f"Uploaded {len(uploaded)} file(s) successfully.", "files": uploaded}


@file_router.get("/download")
async def download_file(
    workspace_id: str,
    path: str = Query(..., description="Relative file path"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await get_accessible_workspace(workspace_id, current_user, db)
    try:
        connection = agent_manager.get(workspace.node_id)
    except AgentUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    transfer_id = str(uuid.uuid4())
    try:
        result = await connection.request("transfer.open", {
            "transfer_id": transfer_id, "purpose": "download",
            "container_name": workspace.container_name, "path": path,
        })
    except (AgentCommandError, AgentUnavailable, TimeoutError) as exc:
        await abort_transfer(connection, transfer_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except BaseException:
        await abort_transfer(connection, transfer_id)
        raise
    filename = Path(result["name"]).name
    return StreamingResponse(
        download_chunks(connection, transfer_id, result["size"]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
                 "Content-Length": str(result["size"])},
        background=BackgroundTask(abort_transfer, connection, transfer_id),
    )


@file_router.post("/mkdir")
async def create_directory(
    workspace_id: str,
    path: str = Query(..., description="Directory path to create"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await get_accessible_workspace(workspace_id, current_user, db)
    result = await _worker_request(workspace, "files.mkdir", {"path": path})
    return {
        "message": f"Directory '{result.get('name', '')}' created successfully."
    }


@file_router.delete("")
async def delete_file_or_dir(
    workspace_id: str,
    path: str = Query(..., description="Relative file or directory path"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await get_accessible_workspace(workspace_id, current_user, db)
    result = await _worker_request(workspace, "files.delete", {"path": path})
    return {"message": f"Deleted '{result.get('name', '')}' successfully."}
