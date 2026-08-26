import base64
import os
import shutil
from pathlib import Path
from urllib.parse import quote
from typing import Annotated
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User, UserRole
from app.models.workspace import Workspace
from app.orchestrator.metrics_service import format_bytes_human
from app.agents.manager import AgentCommandError, AgentUnavailable, agent_manager

file_router = APIRouter(prefix="/api/workspaces/{workspace_id}/files", tags=["Files"])


def get_safe_path(base_dir: str, rel_path: str = "") -> Path:
    """Resolve subpath securely inside the workspace storage directory to prevent path traversal."""
    clean_base = Path(base_dir).resolve()
    target = (clean_base / rel_path.lstrip("/\\")).resolve()
    if target != clean_base and clean_base not in target.parents:
        raise HTTPException(status_code=403, detail="Access denied: path traversal forbidden.")
    return target


async def get_accessible_workspace(
    workspace_id: str,
    current_user: User,
    db: AsyncSession,
) -> Workspace:
    """Ensure user owns workspace or is admin."""
    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    if workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Access denied.")
    if not workspace.storage_path or (
        not workspace.node_id and not os.path.exists(workspace.storage_path)
    ):
        raise HTTPException(status_code=404, detail="Workspace storage path is not initialized.")
    return workspace


@file_router.get("")
async def list_files(
    workspace_id: str,
    path: str = Query("", description="Relative directory path"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List directory contents within the workspace persistent directory."""
    ws = await get_accessible_workspace(workspace_id, current_user, db)
    if ws.node_id:
        try:
            result = await agent_manager.get(ws.node_id).request(
                "files.list", {"container_name": ws.container_name, "path": path}
            )
        except (AgentUnavailable, AgentCommandError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        for item in result.get("items") or []:
            timestamp = item.pop("modified_timestamp", None)
            item["size_display"] = format_bytes_human(item.get("size_bytes", 0)) if not item.get("is_dir") else "--"
            item["modified_at"] = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if timestamp else "—"
        return result
    target_dir = get_safe_path(ws.storage_path, path)

    if not target_dir.exists():
        raise HTTPException(status_code=404, detail="Directory not found.")
    if not target_dir.is_dir():
        raise HTTPException(status_code=400, detail="Target path is a file, not a directory.")

    items = []
    base_path = Path(ws.storage_path).resolve()

    try:
        for entry in sorted(os.scandir(target_dir), key=lambda e: (not e.is_dir(), e.name.lower())):
            stat = entry.stat()
            rel_entry_path = str(Path(entry.path).relative_to(base_path)).replace("\\", "/")
            items.append({
                "name": entry.name,
                "path": rel_entry_path,
                "is_dir": entry.is_dir(),
                "size_bytes": stat.st_size if entry.is_file() else 0,
                "size_display": format_bytes_human(stat.st_size) if entry.is_file() else "--",
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied reading directory.")

    current_rel = str(target_dir.relative_to(base_path)).replace("\\", "/")
    if current_rel == ".":
        current_rel = ""

    return {
        "current_path": current_rel,
        "items": items,
    }


@file_router.post("/upload")
async def upload_files(
    workspace_id: str,
    path: str = Form(""),
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload one or more files into the specified directory."""
    ws = await get_accessible_workspace(workspace_id, current_user, db)
    if ws.node_id:
        encoded_files = []
        for file in files:
            encoded_files.append(
                {
                    "name": Path(file.filename or "").name,
                    "content": base64.b64encode(await file.read()).decode("ascii"),
                }
            )
        try:
            result = await agent_manager.get(ws.node_id).request(
                "files.upload",
                {"container_name": ws.container_name, "path": path, "files": encoded_files},
                timeout=120,
            )
        except (AgentUnavailable, AgentCommandError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"message": f"Uploaded {len(result.get('files') or [])} file(s) successfully.", "files": result.get("files") or []}
    target_dir = get_safe_path(ws.storage_path, path)
    target_dir.mkdir(parents=True, exist_ok=True)

    uploaded = []
    for file in files:
        safe_filename = Path(file.filename).name
        dest = target_dir / safe_filename
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        uploaded.append(safe_filename)

    return {"message": f"Uploaded {len(uploaded)} file(s) successfully.", "files": uploaded}


@file_router.get("/download")
async def download_file(
    workspace_id: str,
    path: str = Query(..., description="Relative file path"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download a file from workspace storage."""
    ws = await get_accessible_workspace(workspace_id, current_user, db)
    if ws.node_id:
        try:
            result = await agent_manager.get(ws.node_id).request(
                "files.download", {"container_name": ws.container_name, "path": path}, timeout=120
            )
            content = base64.b64decode(result.get("content", ""), validate=True)
        except (AgentUnavailable, AgentCommandError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        filename = Path(result.get("name") or path).name
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )
    target_file = get_safe_path(ws.storage_path, path)

    if not target_file.exists() or not target_file.is_file():
        raise HTTPException(status_code=404, detail="File not found.")

    return FileResponse(
        path=str(target_file),
        filename=target_file.name,
        media_type="application/octet-stream",
    )


@file_router.post("/mkdir")
async def create_directory(
    workspace_id: str,
    path: str = Query(..., description="Directory path to create"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new folder in workspace storage."""
    ws = await get_accessible_workspace(workspace_id, current_user, db)
    if ws.node_id:
        try:
            result = await agent_manager.get(ws.node_id).request(
                "files.mkdir", {"container_name": ws.container_name, "path": path}
            )
        except (AgentUnavailable, AgentCommandError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"message": f"Directory '{result.get('name', '')}' created successfully."}
    target_dir = get_safe_path(ws.storage_path, path)
    target_dir.mkdir(parents=True, exist_ok=True)
    return {"message": f"Directory '{target_dir.name}' created successfully."}


@file_router.delete("")
async def delete_file_or_dir(
    workspace_id: str,
    path: str = Query(..., description="Relative path of file or directory to delete"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Permanently delete a file or directory inside workspace storage."""
    ws = await get_accessible_workspace(workspace_id, current_user, db)
    if ws.node_id:
        try:
            result = await agent_manager.get(ws.node_id).request(
                "files.delete", {"container_name": ws.container_name, "path": path}
            )
        except (AgentUnavailable, AgentCommandError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"message": f"Deleted '{result.get('name', '')}' successfully."}
    target = get_safe_path(ws.storage_path, path)

    if target == Path(ws.storage_path).resolve():
        raise HTTPException(status_code=400, detail="Cannot delete root storage directory.")

    if not target.exists():
        raise HTTPException(status_code=404, detail="Path does not exist.")

    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    return {"message": f"Deleted '{target.name}' successfully."}
