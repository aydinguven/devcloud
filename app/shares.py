"""Port-scoped sharing credentials, separate from DevCloud login tokens."""
import time
import jwt
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import release_read_only_connection
from app.models.workspace_share import WorkspaceShare
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.user import User

COOKIE_PREFIX = "devcloud_share_"


def sign_share(record: WorkspaceShare, purpose: str = "share-link") -> str:
    payload = {"share_id": record.id, "purpose": purpose}
    if purpose == "share-grant":
        payload["exp"] = min(record.expires_at or int(time.time()) + 86400, int(time.time()) + 86400)
    elif record.expires_at:
        payload["exp"] = record.expires_at
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


async def read_share(db: AsyncSession, token: str, purpose: str) -> WorkspaceShare:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("purpose") != purpose:
            raise ValueError("wrong purpose")
        record = await db.get(WorkspaceShare, payload["share_id"], populate_existing=True)
    except (jwt.PyJWTError, ValueError, KeyError, TypeError):
        raise HTTPException(404, "Paylaşım bağlantısı geçersiz.")
    if not record or record.revoked or (record.expires_at and record.expires_at <= time.time()):
        raise HTTPException(404, "Paylaşım bağlantısı geçersiz veya süresi dolmuş.")
    return record


def cookie_name(workspace_id: str, port: int) -> str:
    return f"{COOKIE_PREFIX}{workspace_id}_{port}"


async def shared_workspace(db: AsyncSession, cookies, workspace_id: str, port: int) -> Workspace:
    token = cookies.get(cookie_name(workspace_id, port), "")
    record = await read_share(db, token, "share-grant")
    if record.workspace_id != workspace_id or record.port != port:
        raise HTTPException(403, "Paylaşım bu port için geçerli değil.")
    workspace = await db.get(Workspace, workspace_id, populate_existing=True)
    owner = await db.get(User, workspace.user_id, populate_existing=True) if workspace else None
    if not workspace or not owner or not owner.is_active or workspace.status == WorkspaceStatus.DELETED:
        raise HTTPException(404, "Paylaşım kullanılamıyor.")
    if workspace.status != WorkspaceStatus.RUNNING:
        raise HTTPException(503, "Çalışma alanı çalışmıyor.")
    await release_read_only_connection(db)
    return workspace
