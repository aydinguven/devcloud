import asyncio
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user_optional
from app.config import settings
from app.database import get_db
import json
import logging
from app.models.user import User, UserRole
from app.models.directory_settings import DirectorySettings
from app.models.mlflow_settings import MlflowSettings
from app.models.download_settings import DownloadSettings
from app.models.node import Node
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.flavors import list_flavors
from app.orchestrator.templates import list_templates
from app.orchestrator.runtime_backend import runtime_for_node
from app.agents.manager import AgentUnavailable
from app.resource_usage import get_all_user_usage, get_system_usage, get_user_usage
from app.schemas.workspace import WorkspaceOut
from app.static_assets import STATIC_ASSET_VERSION

logger = logging.getLogger("devcloud.views")

templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
templates.env.globals["app_version"] = settings.APP_VERSION
templates.env.globals["static_version"] = STATIC_ASSET_VERSION

def _safe_from_json(val):
    if not val:
        return {}
    if isinstance(val, dict):
        return val
    try:
        data = json.loads(val)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

templates.env.filters["from_json"] = _safe_from_json

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


ADMIN_SECTIONS = {
    "overview": {
        "title": "Genel Bakış",
        "description": "Platform sağlığını ve yönetim alanlarını tek bakışta izleyin.",
    },
    "users": {
        "title": "Kullanıcılar & Erişim",
        "description": "Kullanıcı kotalarını, hesap durumlarını ve kurumsal dizin erişimini yönetin.",
    },
    "workspaces": {
        "title": "Çalışma Alanları & Şablonlar",
        "description": "Tüm çalışma alanlarını denetleyin ve özel ortam şablonları oluşturun.",
    },
    "workers": {
        "title": "Worker Node'ları",
        "description": "CPU worker kayıtlarını, bağlantı durumlarını ve planlamayı yönetin.",
    },
    "integrations": {
        "title": "Entegrasyonlar",
        "description": "Harici platform bağlantılarını ve servis kimlik bilgilerini yönetin.",
    },
    "system": {
        "title": "Sistem & Dağıtım",
        "description": "Platform güncellemelerini, offline paketleri, disk alanını ve HTTPS ayarlarını yönetin.",
    },
}


@view_router.get("/admin", response_class=HTMLResponse)
@view_router.get("/admin/{section}", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
    db: Annotated[AsyncSession, Depends(get_db)],
    section: str = "overview",
):
    """Serve one focused admin category without loading unrelated datasets."""
    if not current_user or current_user.role != UserRole.ADMIN:
        return RedirectResponse(url="/", status_code=302)
    if section not in ADMIN_SECTIONS:
        raise HTTPException(status_code=404, detail="Yönetim bölümü bulunamadı.")

    context = {
        "app_name": settings.APP_NAME,
        "user": current_user,
        "admin_section": section,
        "admin_meta": ADMIN_SECTIONS[section],
        "admin_sections": ADMIN_SECTIONS,
    }

    if section == "overview":
        context["admin_stats"] = {
            "users": (await db.execute(select(func.count(User.id)))).scalar_one(),
            "workspaces": (
                await db.execute(select(func.count(Workspace.id)))
            ).scalar_one(),
            "running_workspaces": (
                await db.execute(
                    select(func.count(Workspace.id)).where(
                        Workspace.status == WorkspaceStatus.RUNNING
                    )
                )
            ).scalar_one(),
            "workers": (await db.execute(select(func.count(Node.id)))).scalar_one(),
        }
    elif section == "users":
        users = (
            await db.execute(select(User).order_by(User.id.asc()))
        ).scalars().all()
        workspaces = (
            await db.execute(select(Workspace).order_by(Workspace.created_at.desc()))
        ).scalars().all()
        context.update(
            {
                "all_users": users,
                "usage_by_user": await asyncio.to_thread(
                    get_all_user_usage, users, workspaces
                ),
                "directory_settings": await db.get(DirectorySettings, 1),
            }
        )
    elif section == "workspaces":
        context["all_workspaces"] = (
            await db.execute(select(Workspace).order_by(Workspace.created_at.desc()))
        ).scalars().all()
    elif section == "workers":
        download_settings = None
        try:
            download_settings = await db.get(DownloadSettings, 1)
        except Exception:
            pass

        try:
            nodes_res = await db.execute(select(Node).order_by(Node.name))
            nodes = nodes_res.scalars().all()
        except Exception as exc:
            logger.warning("Error fetching nodes for admin workers view: %s. Attempting schema ensure...", exc)
            try:
                from app.database import ensure_node_columns
                # Run dynamic migration via connection
                conn = await db.connection()
                await ensure_node_columns(conn)
                nodes_res = await db.execute(select(Node).order_by(Node.name))
                nodes = nodes_res.scalars().all()
            except Exception as retry_exc:
                logger.error("Failed to recover nodes query: %s", retry_exc)
                nodes = []

        context["nodes"] = nodes
        context["download_public_base_url"] = (
            download_settings.public_base_url
            if download_settings and download_settings.public_base_url
            else (settings.DOWNLOAD_PUBLIC_BASE_URL or str(request.base_url).rstrip("/"))
        )
    elif section == "integrations":
        context["mlflow_settings"] = await db.get(MlflowSettings, 1)
    elif section == "system":
        download_settings = await db.get(DownloadSettings, 1)
        context.update(
            {
                "download_public_base_url": (
                    download_settings.public_base_url
                    if download_settings
                    else settings.DOWNLOAD_PUBLIC_BASE_URL
                ),
                "download_settings": download_settings,
                "https_default_hostname": settings.HTTPS_DEFAULT_HOSTNAME,
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context=context,
    )
