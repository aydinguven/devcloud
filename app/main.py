from contextlib import asynccontextmanager
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.auth.internal import hash_password
from app.config import settings
from app.database import init_db, AsyncSessionLocal
from app.models.user import User, UserRole
from app.orchestrator.podman_service import podman_service
from app.proxy.router import proxy_router
from app.routes.admin_routes import admin_router
from app.routes.auth_routes import auth_router
from app.routes.download_routes import download_router
from app.routes.view_routes import view_router
from app.routes.workspace_routes import workspace_router

# Setup logging
logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("devcloud")


async def seed_initial_admin():
    """Seed initial administrator account if no admin exists."""
    async with AsyncSessionLocal() as db:
        stmt = select(User).where(User.username == settings.ADMIN_USERNAME)
        result = await db.execute(stmt)
        admin = result.scalar_one_or_none()

        if not admin:
            logger.info(f"Seeding default admin user: {settings.ADMIN_USERNAME}")
            admin_user = User(
                username=settings.ADMIN_USERNAME,
                email=settings.ADMIN_EMAIL,
                hashed_password=hash_password(settings.ADMIN_PASSWORD),
                full_name="Sistem Yöneticisi",
                role=UserRole.ADMIN,
                auth_source="internal",
                is_active=True,
            )
            db.add(admin_user)
            await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for startup and shutdown."""
    import asyncio
    from app.orchestrator.idle_reaper import idle_reaper_background_worker

    logger.info("Initializing DevCloud Database...")
    await init_db()
    await seed_initial_admin()
    logger.info(
        f"DevCloud ready. Podman mode: {'MOCK' if podman_service.is_mock else 'NATIVE'}"
    )

    # Start idle inactivity auto-stop worker
    reaper_task = asyncio.create_task(idle_reaper_background_worker(check_interval_seconds=60))

    yield
    reaper_task.cancel()
    logger.info("DevCloud shutting down...")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Self-hosted Cloud Development Environment Platform (Podman Orchestration)",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Include routers
from app.routes.file_routes import file_router

app.include_router(auth_router)
app.include_router(workspace_router)
app.include_router(file_router)
app.include_router(admin_router)
app.include_router(proxy_router)
app.include_router(download_router)
app.include_router(view_router)
