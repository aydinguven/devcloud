from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user_optional
from app.config import settings
from app.database import get_db
from app.models.user import User, UserRole
from app.models.workspace import Workspace
from app.orchestrator.flavors import list_flavors
from app.orchestrator.templates import list_templates
from app.schemas.workspace import WorkspaceOut

templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

view_router = APIRouter(include_in_schema=False)


@view_router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """Serve login page."""
    if current_user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"app_name": settings.APP_NAME, "user": None},
    )


@view_router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """Serve register page."""
    if current_user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"app_name": settings.APP_NAME, "user": None},
    )


@view_router.get("/", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Serve main workspace dashboard."""
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    stmt = (
        select(Workspace)
        .where(Workspace.user_id == current_user.id)
        .order_by(Workspace.created_at.desc())
    )
    result = await db.execute(stmt)
    workspaces = result.scalars().all()

    ws_list = [WorkspaceOut.model_validate(ws) for ws in workspaces]

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "workspaces": ws_list,
            "templates": list_templates(),
            "flavors": list_flavors(),
        },
    )


@view_router.get("/workspaces/{workspace_id}", response_class=HTMLResponse)
async def workspace_detail_page(
    workspace_id: str,
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Serve workspace detail and IDE frame page."""
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    stmt = select(Workspace).where(Workspace.id == workspace_id)
    result = await db.execute(stmt)
    workspace = result.scalar_one_or_none()

    if not workspace or (
        workspace.user_id != current_user.id and current_user.role != UserRole.ADMIN
    ):
        return RedirectResponse(url="/", status_code=302)

    ws_out = WorkspaceOut.model_validate(workspace)
    ws_out.web_url = f"/proxy/{workspace.id}/"

    return templates.TemplateResponse(
        request=request,
        name="workspace_detail.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "workspace": ws_out,
            "raw_workspace": workspace,
        },
    )


@view_router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """Serve user profile page."""
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
        },
    )


@view_router.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Serve admin dashboard page."""
    if not current_user or current_user.role != UserRole.ADMIN:
        return RedirectResponse(url="/", status_code=302)

    users_stmt = select(User).order_by(User.id.asc())
    users = (await db.execute(users_stmt)).scalars().all()

    ws_stmt = select(Workspace).order_by(Workspace.created_at.desc())
    workspaces = (await db.execute(ws_stmt)).scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "all_users": users,
            "all_workspaces": workspaces,
        },
    )
