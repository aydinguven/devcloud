from app.routes.auth_routes import auth_router
from app.routes.workspace_routes import workspace_router
from app.routes.admin_routes import admin_router
from app.routes.download_routes import download_router
from app.routes.view_routes import view_router
from app.routes.worker_bootstrap_routes import bootstrap_router

__all__ = [
    "auth_router",
    "workspace_router",
    "admin_router",
    "download_router",
    "bootstrap_router",
    "view_router",
]
