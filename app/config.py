from pathlib import Path
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict
from app import __version__


class Settings(BaseSettings):
    """Application settings and configuration."""
    
    APP_NAME: str = "DevCloud Çalışma Alanı Yönetimi"
    APP_VERSION: ClassVar[str] = __version__
    ENV: str = "development"
    DEBUG: bool = True
    
    # Security
    SECRET_KEY: str = "devcloud-super-secret-key-change-in-production-1234567890"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    COOKIE_NAME: str = "devcloud_session"
    COOKIE_SECURE: bool = False
    
    # Storage & Database
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/devcloud.db"
    STORAGE_ROOT: str = str(Path(__file__).resolve().parent.parent / "data" / "workspaces")
    AUTO_MIGRATE: bool = True

    # Deployment topology. All-in-one is a controller plus an ordinary worker
    # agent on the same host; the controller never runs workspace containers.
    DEVCLOUD_DEPLOYMENT_ROLE: str = "controller"
    DEVCLOUD_BOOTSTRAP_WORKER_ID: str = ""
    DEVCLOUD_BOOTSTRAP_WORKER_NAME: str = ""
    DEVCLOUD_BOOTSTRAP_WORKER_TOKEN_HASH: str = ""
    DEVCLOUD_REGISTRY_MODE: str = "preloaded"
    DEVCLOUD_REGISTRY_URL: str = ""
    
    # Quotas assigned to newly registered users (admins can override each user).
    DEFAULT_USER_CPU_QUOTA: float = 1.0
    DEFAULT_USER_MEMORY_MB_QUOTA: int = 1024
    DEFAULT_USER_DISK_MB_QUOTA: int = 10240

    # Offline bundle publishing (admin-only; disable updates on disconnected hosts).
    DOWNLOADS_ENABLED: bool = True
    DOWNLOAD_UPDATES_ENABLED: bool = True
    DOWNLOADS_ROOT: str = "/srv/devcloud-downloads"
    DOWNLOAD_BUILD_ROOT: str = str(BASE_DIR / "data" / "download-builds")
    DOWNLOAD_TARGET_PYTHON_VERSION: str = ""
    DOWNLOAD_PUBLIC_BASE_URL: str = "http://10.253.6.189"
    UPDATES_ENABLED: bool = True
    UPDATE_SOURCE_TYPE: str = "bundle"
    UPDATE_SOURCE: str = ""
    UPDATE_REF: str = "stable"
    UPDATE_QUEUE_ROOT: str = "/var/lib/devcloud/update-queue"
    UPDATE_MAX_UPLOAD_BYTES: int = 8 * 1024 * 1024 * 1024
    WORKSPACE_IMAGES_ROOT: str = "/srv/devcloud-downloads/workspace-images"
    WORKSPACE_IMAGE_MAX_UPLOAD_BYTES: int = 16 * 1024 * 1024 * 1024
    WORKSPACE_IMAGE_IMPORT_TIMEOUT_SECONDS: int = 1800
    SKOPEO_BIN: str = "skopeo"

    # Nginx ingress is applied by a root-owned, narrowly scoped helper.
    INGRESS_STAGING_ROOT: str = "/var/lib/devcloud/ingress"
    INGRESS_APPLY_COMMAND: str = "/usr/local/libexec/devcloud-apply-ingress"
    HTTPS_DEFAULT_HOSTNAME: str = "127.0.0.1"

    # Podman Configuration
    PODMAN_BIN: str = "podman"
    PODMAN_NETWORK: str = "bridge"
    USE_MOCK_PODMAN: bool = False
    PODMAN_RUN_TIMEOUT_SECONDS: float = 30.0
    
    # Container Port Range for internal forwarding
    PORT_RANGE_START: int = 10100
    PORT_RANGE_END: int = 12000
    
    # Initial Admin Seed
    ADMIN_USERNAME: str = "admin"
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "admin123"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
