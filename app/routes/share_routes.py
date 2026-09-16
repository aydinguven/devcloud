import asyncio
import hashlib
import time
from typing import Annotated
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.auth import get_current_user, hash_password, verify_password
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.workspace_share import WorkspaceShare
from app.proxy.router import get_authorized_workspace
from app.shares import read_share, sign_share, cookie_name

share_router = APIRouter(tags=["Workspace sharing"])
Db = Annotated[AsyncSession, Depends(get_db)]
Actor = Annotated[User, Depends(get_current_user)]


class ShareCreate(BaseModel):
    port: int = Field(strict=True, ge=1, le=65535)
    password: str | None = Field(default=None, min_length=1, max_length=256)
    expires_in_minutes: int | None = Field(default=None, strict=True, ge=1, le=525600)


def output(record):
    return {"id": record.id, "port": record.port, "password_protected": bool(record.password_hash),
            "expires_at": record.expires_at, "revoked": record.revoked,
            "url": f"/share/{sign_share(record)}"}


def password_digest(password):
    return hashlib.sha256(password.encode()).hexdigest()


@share_router.post("/api/workspaces/{workspace_id}/shares", status_code=201)
async def create_share(workspace_id: str, payload: ShareCreate, db: Db, user: Actor):
    await get_authorized_workspace(workspace_id, db, user, require_running=False)
    record = WorkspaceShare(workspace_id=workspace_id, port=payload.port,
        expires_at=int(time.time()) + payload.expires_in_minutes * 60 if payload.expires_in_minutes else None,
        password_hash=await asyncio.to_thread(hash_password, password_digest(payload.password)) if payload.password else None)
    db.add(record)
    await db.commit()
    return output(record)


@share_router.get("/api/workspaces/{workspace_id}/shares")
async def list_shares(workspace_id: str, db: Db, user: Actor):
    await get_authorized_workspace(workspace_id, db, user, require_running=False)
    rows = (await db.execute(select(WorkspaceShare).where(WorkspaceShare.workspace_id == workspace_id, WorkspaceShare.revoked.is_(False)))).scalars()
    return [output(row) for row in rows]


@share_router.delete("/api/workspaces/{workspace_id}/shares/{share_id}", status_code=204)
async def revoke_share(workspace_id: str, share_id: str, db: Db, user: Actor):
    await get_authorized_workspace(workspace_id, db, user, require_running=False)
    record = await db.get(WorkspaceShare, share_id)
    if not record or record.workspace_id != workspace_id:
        raise HTTPException(404, "Paylaşım bulunamadı.")
    record.revoked = True
    await db.commit()


def password_page(error=False):
    return HTMLResponse('''<!doctype html><html lang="tr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>DevCloud Paylaşımı</title>
<body style="font-family:system-ui;max-width:420px;margin:12vh auto;padding:24px">
<h1>Paylaşılan uygulama</h1><p>Bu bağlantıya erişmek için parolayı girin.</p>'''
        + ('<p role="alert">Parola hatalı.</p>' if error else '') + '''
<form method="post"><label>Parola <input type="password" name="password" required maxlength="256" autocomplete="current-password"></label>
<button type="submit">Uygulamayı aç</button></form></body></html>''',
        status_code=401 if error else 200,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"})


def grant(record, request):
    path = f"/proxy/{record.workspace_id}/port/{record.port}/"
    response = RedirectResponse(path, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    response.set_cookie(cookie_name(record.workspace_id, record.port), sign_share(record, "share-grant"),
        httponly=True, secure=settings.COOKIE_SECURE or request.url.scheme == "https", samesite="lax", path=path,
        max_age=max(1, min(86400, record.expires_at - int(time.time()) if record.expires_at else 86400)))
    return response


@share_router.get("/share/{token}")
async def open_share(token: str, request: Request, db: Db):
    record = await read_share(db, token, "share-link")
    return password_page() if record.password_hash else grant(record, request)


@share_router.post("/share/{token}")
async def unlock_share(token: str, request: Request, db: Db, password: Annotated[str, Form(max_length=256)]):
    record = await read_share(db, token, "share-link")
    if record.locked_until > time.time():
        raise HTTPException(429, "Çok fazla deneme. Bir dakika sonra tekrar deneyin.", headers={"Retry-After": "60"})
    if record.password_hash and not await asyncio.to_thread(verify_password, password_digest(password), record.password_hash):
        await db.execute(update(WorkspaceShare).where(WorkspaceShare.id == record.id).values(failed_attempts=WorkspaceShare.failed_attempts + 1))
        await db.refresh(record)
        if record.failed_attempts >= 5:
            record.locked_until = int(time.time()) + 60
            record.failed_attempts = 0
        await db.commit()
        return password_page(True)
    record.failed_attempts = 0
    await db.commit()
    return grant(record, request)
