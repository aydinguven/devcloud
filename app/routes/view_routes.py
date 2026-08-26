import asyncio
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
from app.models.directory_settings import DirectorySettings
from app.models.mlflow_settings import MlflowSettings
from app.models.node import Node
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.flavors import list_flavors
from app.orchestrator.templates import list_templates
from app.orchestrator.runtime_backend import runtime_for_node
from app.agents.manager import AgentUnavailable
from app.resource_usage import get_all_user_usage, get_system_usage, get_user_usage
from app.schemas.workspace import WorkspaceOut

templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.globals["app_version"] = settings.APP_VERSION

view_router = APIRouter(include_in_schema=False)


@view_router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Serve login page."""
    if current_user:
        return RedirectResponse(url="/", status_code=302)
    directory_settings = await db.get(DirectorySettings, 1)
    registration_enabled = not (
        directory_settings and directory_settings.enabled
    )
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "app_name": settings.APP_NAME,
            "user": None,
            "registration_cpu_quota": settings.DEFAULT_USER_CPU_QUOTA,
            "registration_memory_gb_quota": settings.DEFAULT_USER_MEMORY_MB_QUOTA / 1024,
            "registration_disk_gb_quota": settings.DEFAULT_USER_DISK_MB_QUOTA / 1024,
            "registration_enabled": registration_enabled,
            "directory_enabled": not registration_enabled,
        },
    )


@view_router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Serve register page."""
    if current_user:
        return RedirectResponse(url="/", status_code=302)
    directory_settings = await db.get(DirectorySettings, 1)
    if directory_settings and directory_settings.enabled:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "app_name": settings.APP_NAME,
            "user": None,
            "registration_cpu_quota": settings.DEFAULT_USER_CPU_QUOTA,
            "registration_memory_gb_quota": settings.DEFAULT_USER_MEMORY_MB_QUOTA / 1024,
            "registration_disk_gb_quota": settings.DEFAULT_USER_DISK_MB_QUOTA / 1024,
        },
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

    # Self-healing status reconciliation with Podman engine
    status_changed = False
    for ws in workspaces:
        if ws.status == WorkspaceStatus.CREATING and ws.container_name:
            try:
                actual_status = await runtime_for_node(ws.node_id).get_container_status(ws.container_name)
            except AgentUnavailable:
                continue
            if actual_status == "running":
                ws.status = WorkspaceStatus.RUNNING
                db.add(ws)
                status_changed = True
    if status_changed:
        await db.commit()

    ws_list = [WorkspaceOut.model_validate(ws) for ws in workspaces]
    for ws_out in ws_list:
        ws_out.web_url = f"/proxy/{ws_out.id}/"

    system_usage, user_usage = await asyncio.gather(
        asyncio.to_thread(get_system_usage),
        asyncio.to_thread(get_user_usage, current_user, workspaces),
    )

    from app.models.custom_template import CustomTemplate
    from app.orchestrator.templates import register_custom_template

    custom_res = await db.execute(select(CustomTemplate))
    custom_db_templates = custom_res.scalars().all()
    all_tpls = list_templates()
    for ct in custom_db_templates:
        tpl_obj = register_custom_template(
            template_id=ct.id,
            name=ct.name,
            description=ct.description,
            category=ct.category,
            image_tag=ct.image_tag,
            default_port=ct.default_port,
            ide_type=ct.ide_type,
            icon=ct.icon,
        )
        if not any(t.id == ct.id for t in all_tpls):
            all_tpls.append(tpl_obj.to_schema())

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "workspaces": ws_list,
            "templates": all_tpls,
            "flavors": list_flavors(),
            "system_usage": system_usage,
            "user_usage": user_usage,
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

    # Reconcile status with Podman if needed
    if workspace.container_name:
        try:
            actual_status = await runtime_for_node(workspace.node_id).get_container_status(workspace.container_name)
        except AgentUnavailable:
            actual_status = "worker-offline"
        if actual_status == "running" and workspace.status != WorkspaceStatus.RUNNING:
            workspace.status = WorkspaceStatus.RUNNING
            db.add(workspace)
            await db.commit()

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


@view_router.get("/models", response_class=HTMLResponse)
async def models_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    mlflow_settings = await db.get(MlflowSettings, 1)
    return templates.TemplateResponse(
        request=request,
        name="models.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "mlflow_enabled": bool(mlflow_settings and mlflow_settings.enabled),
            "mlflow_base_url": mlflow_settings.base_url if mlflow_settings else "",
        },
    )


@view_router.get("/models/{model_name}", response_class=HTMLResponse)
async def model_detail_page(
    model_name: str,
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="model_detail.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "model_name": model_name,
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

    usage_by_user = await asyncio.to_thread(
        get_all_user_usage, users, workspaces
    )
    directory_settings = await db.get(DirectorySettings, 1)
    mlflow_settings = await db.get(MlflowSettings, 1)
    nodes = (await db.execute(select(Node).order_by(Node.name))).scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "all_users": users,
            "all_workspaces": workspaces,
            "usage_by_user": usage_by_user,
            "directory_settings": directory_settings,
            "mlflow_settings": mlflow_settings,
            "nodes": nodes,
        },
    )
