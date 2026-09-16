from datetime import datetime

from pydantic import BaseModel, Field, field_validator


def _normalize_registry_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if "://" in value:
        raise ValueError(
            "Registry adresi http:// veya https:// içermemelidir; host:port kullanın."
        )
    if any(ord(character) < 33 for character in value):
        raise ValueError("Registry adresi boşluk veya kontrol karakteri içeremez.")
    if "@" in value or "?" in value or "#" in value:
        raise ValueError("Registry adresi kimlik bilgisi, query veya fragment içeremez.")
    host, *path_parts = value.split("/")
    if not host or host in {".", ".."} or host.startswith(":"):
        raise ValueError("Geçerli bir registry host:port adresi girin.")
    if any(not part or part in {".", ".."} or ":" in part for part in path_parts):
        raise ValueError("Registry namespace yolu geçersiz.")
    return value


class ModelContainerRegistrySettingsUpdate(BaseModel):
    enabled: bool = False
    registry_url: str = Field(default="", max_length=1024)
    username: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=4096)

    @field_validator("registry_url", mode="after")
    @classmethod
    def validate_registry_url(cls, value: str) -> str:
        return _normalize_registry_url(value)

    @field_validator("username", mode="after")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if any(ord(character) < 32 for character in value):
            raise ValueError("Registry kullanıcı adı kontrol karakteri içeremez.")
        return value


class ModelContainerRegistrySettingsOut(BaseModel):
    managed: bool
    enabled: bool
    registry_url: str
    username: str
    has_password: bool
    updated_at: datetime | None = None


class ModelContainerRegistryTestRequest(BaseModel):
    registry_url: str = Field(min_length=1, max_length=1024)
    username: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=4096)

    @field_validator("registry_url", mode="after")
    @classmethod
    def validate_registry_url(cls, value: str) -> str:
        normalized = _normalize_registry_url(value)
        if not normalized:
            raise ValueError("Registry adresi zorunludur.")
        return normalized

    @field_validator("username", mode="after")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if any(ord(character) < 32 for character in value):
            raise ValueError("Registry kullanıcı adı kontrol karakteri içeremez.")
        return value


class ModelContainerRegistryTargetResult(BaseModel):
    target_id: str
    target_name: str
    target_kind: str
    ok: bool
    status_code: int | None = None
    latency_ms: int | None = None
    message: str


class ModelContainerRegistryTestResult(BaseModel):
    ok: bool
    registry_url: str
    targets: list[ModelContainerRegistryTargetResult]
