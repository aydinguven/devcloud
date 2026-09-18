import asyncio
import hashlib
import math
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, hash_password, verify_password
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.workspace_share import WorkspaceShare
from app.proxy.router import get_authorized_workspace
from app.share_pages import render_share_error, render_share_page
from app.shares import (
    ShareAccessError,
    cookie_name,
    read_share,
    share_workspace_and_owner,
    sign_share,
)

share_router = APIRouter(tags=["Workspace sharing"])
Db = Annotated[AsyncSession, Depends(get_db)]
Actor = Annotated[User, Depends(get_current_user)]


class ShareCreate(BaseModel):
    port: int = Field(strict=True, ge=1, le=65535)
    password: str | None = Field(default=None, min_length=1, max_length=256)
    expires_in_minutes: int | None = Field(
        default=None, strict=True, ge=1, le=525600
    )


def output(record):
    return {
        "id": record.id,
        "port": record.port,
        "password_protected": bool(record.password_hash),
        "expires_at": record.expires_at,
        "revoked": record.revoked,
        "url": f"/share/{sign_share(record)}",
    }


def password_digest(password):
    return hashlib.sha256(password.encode()).hexdigest()


def format_remaining(expires_at: int | None, now: float | None = None) -> str:
    if expires_at is None:
        return "Süresiz"
    remaining_seconds = max(0, expires_at - (time.time() if now is None else now))
    total_minutes = math.ceil(remaining_seconds / 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} saat {minutes} dk kaldı"


def sharer_label(user: User) -> str:
    full_name = (user.full_name or "").strip()
    return f"{user.username} ({full_name})" if full_name else user.username


async def _share_page_context(db: AsyncSession, record: WorkspaceShare):
    _workspace, owner = await share_workspace_and_owner(
        db, record, require_running=True
    )
    return sharer_label(owner), format_remaining(record.expires_at)


def _access_page(
    request: Request,
    record: WorkspaceShare,
    owner_label: str,
    remaining_text: str,
    *,
    status_code: int = 200,
    error_message: str | None = None,
    extra_headers: dict[str, str] | None = None,
):
    password_required = bool(record.password_hash)
    return render_share_page(
        request,
        state="access",
        title="Paylaşıma güvenli erişim",
        message=(
            "Paylaşılan uygulamayı açmak için parolayı girin."
            if password_required
            else "Paylaşılan uygulamaya devam etmek için aşağıdaki düğmeyi kullanın."
        ),
        icon="→",
        status_code=status_code,
        sharer_label=owner_label,
        remaining_text=remaining_text,
        password_required=password_required,
        error_message=error_message,
        extra_headers=extra_headers,
    )


@share_router.post("/api/workspaces/{workspace_id}/shares", status_code=201)
async def create_share(
    workspace_id: str, payload: ShareCreate, db: Db, user: Actor
):
    await get_authorized_workspace(
        workspace_id, db, user, require_running=False
    )
    record = WorkspaceShare(
        workspace_id=workspace_id,
        port=payload.port,
        expires_at=(
            int(time.time()) + payload.expires_in_minutes * 60
            if payload.expires_in_minutes
            else None
        ),
        password_hash=(
            await asyncio.to_thread(
                hash_password, password_digest(payload.password)
            )
            if payload.password
            else None
        ),
    )
    db.add(record)
    await db.commit()
    return output(record)


@share_router.get("/api/workspaces/{workspace_id}/shares")
async def list_shares(workspace_id: str, db: Db, user: Actor):
    await get_authorized_workspace(
        workspace_id, db, user, require_running=False
    )
    rows = (
        await db.execute(
            select(WorkspaceShare).where(
                WorkspaceShare.workspace_id == workspace_id,
                WorkspaceShare.revoked.is_(False),
            )
        )
    ).scalars()
    return [output(row) for row in rows]


@share_router.delete(
    "/api/workspaces/{workspace_id}/shares/{share_id}", status_code=204
)
async def revoke_share(
    workspace_id: str, share_id: str, db: Db, user: Actor
):
    await get_authorized_workspace(
        workspace_id, db, user, require_running=False
    )
    record = await db.get(WorkspaceShare, share_id)
    if not record or record.workspace_id != workspace_id:
        raise HTTPException(404, "Paylaşım bulunamadı.")
    record.revoked = True
    await db.commit()


def grant(record, request):
    path = f"/proxy/{record.workspace_id}/port/{record.port}/"
    response = RedirectResponse(
        path,
        status_code=303,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
    response.set_cookie(
        cookie_name(record.workspace_id, record.port),
        sign_share(record, "share-grant"),
        httponly=True,
        secure=settings.COOKIE_SECURE or request.url.scheme == "https",
        samesite="lax",
        path=path,
        max_age=max(
            1,
            min(
                86400,
                record.expires_at - int(time.time())
                if record.expires_at
                else 86400,
            ),
        ),
    )
    return response


@share_router.get("/share/{token}")
async def open_share(token: str, request: Request, db: Db):
    try:
        record = await read_share(db, token, "share-link")
        owner_label, remaining_text = await _share_page_context(db, record)
    except ShareAccessError as exc:
        return render_share_error(request, exc)
    return _access_page(
        request, record, owner_label, remaining_text
    )


@share_router.post("/share/{token}")
async def unlock_share(
    token: str,
    request: Request,
    db: Db,
    password: Annotated[str, Form(max_length=256)] = "",
):
    try:
        record = await read_share(db, token, "share-link")
        owner_label, remaining_text = await _share_page_context(db, record)
    except ShareAccessError as exc:
        return render_share_error(request, exc)

    if record.locked_until > time.time():
        return _access_page(
            request,
            record,
            owner_label,
            remaining_text,
            status_code=429,
            error_message="Çok fazla deneme. Bir dakika sonra tekrar deneyin.",
            extra_headers={"Retry-After": "60"},
        )
    if record.password_hash and not await asyncio.to_thread(
        verify_password, password_digest(password), record.password_hash
    ):
        await db.execute(
            update(WorkspaceShare)
            .where(WorkspaceShare.id == record.id)
            .values(failed_attempts=WorkspaceShare.failed_attempts + 1)
        )
        await db.refresh(record)
        if record.failed_attempts >= 5:
            record.locked_until = int(time.time()) + 60
            record.failed_attempts = 0
        await db.commit()
        return _access_page(
            request,
            record,
            owner_label,
            remaining_text,
            status_code=401,
            error_message="Parola hatalı.",
        )
    record.failed_attempts = 0
    await db.commit()
    return grant(record, request)
