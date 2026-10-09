"""Public status page and its JSON feed.

Anyone may read statuses and short summaries; administrators additionally get
messages, details and an uncached refresh. ``/healthz`` and ``/readyz`` stay
the process probes for systemd and Podman.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user_optional
from app.database import get_db
from app.health import DOWN, get_report, public_view
from app.models.user import User, UserRole
from app.routes.view_routes import templates

health_router = APIRouter(tags=["Health"])


def _is_admin(user: User | None) -> bool:
    return user is not None and user.role == UserRole.ADMIN


@health_router.get("/api/health")
async def health_report(
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
    refresh: bool = False,
):
    """Per-component health. HTTP 503 when a required component is down."""
    is_admin = _is_admin(current_user)
    report = await get_report(db, refresh=refresh and is_admin)
    return JSONResponse(
        report if is_admin else public_view(report),
        status_code=503 if report["status"] == DOWN else 200,
        headers={"Cache-Control": "no-store"},
    )


@health_router.get("/status", response_class=HTMLResponse, include_in_schema=False)
async def status_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    return templates.TemplateResponse(
        request=request,
        name="status.html",
        context={"is_admin": _is_admin(current_user)},
    )
