import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings and configuration."""
    
    APP_NAME: str = "DevCloud Workspace Manager"
    APP_VERSION: str = "1.0.0"
    ENV: str = "development"
    DEBUG: bool = True
    
    # Security
    SECRET_KEY: str = "devcloud-super-secret-key-change-in-production-1234567890"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    COOKIE_NAME: str = "devcloud_session"
    
    # Storage & Database
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/devcloud.db"
    STORAGE_ROOT: str = str(Path(__file__).resolve().parent.parent / "data" / "workspaces")
    
    # Default per-user resource quotas (admins can override each user).
    DEFAULT_USER_CPU_QUOTA: float = 4.0
    DEFAULT_USER_MEMORY_MB_QUOTA: int = 4096
    DEFAULT_USER_DISK_MB_QUOTA: int = 10240

    # Podman Configuration
    PODMAN_BIN: str = "podman"
    PODMAN_NETWORK: str = "bridge"
    USE_MOCK_PODMAN: bool = False
    PODMAN_RUN_TIMEOUT_SECONDS: float = 30.0
    
    # Container Port Range for internal forwarding
    PORT_RANGE_START: int = 10100
    PORT_RANGE_END: int = 12000
    
    # Auth Provider Config
    AUTH_PROVIDER: str = "internal"  # 'internal' or 'active_directory'
    
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

# Ensure required directories exist
os.makedirs(Path(settings.STORAGE_ROOT), exist_ok=True)
os.makedirs(Path(settings.BASE_DIR) / "data", exist_ok=True)
