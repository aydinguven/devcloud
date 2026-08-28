import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.manager import AgentCommandError, AgentUnavailable, agent_manager
from app.auth.dependencies import get_current_user
from app.database import get_db
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
    encoded_files = [
        {
            "name": Path(file.filename or "").name,
            "content": base64.b64encode(await file.read()).decode("ascii"),
        }
        for file in files
    ]
    result = await _worker_request(
        workspace,
        "files.upload",
        {"path": path, "files": encoded_files},
        timeout=120,
    )
    uploaded = result.get("files") or []
    return {
        "message": f"Uploaded {len(uploaded)} file(s) successfully.",
        "files": uploaded,
    }


@file_router.get("/download")
async def download_file(
    workspace_id: str,
    path: str = Query(..., description="Relative file path"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = await get_accessible_workspace(workspace_id, current_user, db)
    try:
        result = await _worker_request(
            workspace, "files.download", {"path": path}, timeout=120
        )
        content = base64.b64decode(result.get("content", ""), validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    filename = Path(result.get("name") or path).name
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
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
