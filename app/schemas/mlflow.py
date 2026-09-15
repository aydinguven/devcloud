from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class MlflowSettingsUpdate(BaseModel):
    enabled: bool = False
    auth_type: str = "none"
    username: str = Field(default="", max_length=255)
    secret: str | None = Field(default=None, max_length=4096)

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"none", "basic", "bearer"}:
            raise ValueError("auth_type none, basic veya bearer olmalıdır")
        return normalized

class MlflowSettingsOut(BaseModel):
    enabled: bool
    base_url: str
    auth_type: str
    username: str
    has_secret: bool
    validate_tls: bool
    ca_cert_file: str
    timeout_seconds: int


class MlflowServerSettingsUpdate(BaseModel):
    enabled: bool = False
    base_url: str = Field(default="", max_length=1024)
    validate_tls: bool = True
    ca_cert_file: str = Field(default="", max_length=512)
    timeout_seconds: int = Field(default=10, ge=1, le=120)

    @field_validator("base_url", "ca_cert_file", mode="after")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MLflow URL geçerli bir http:// veya https:// adresi olmalıdır.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("MLflow URL kullanıcı bilgisi, query veya fragment içermemelidir.")
        return value.rstrip("/")

    @model_validator(mode="after")
    def require_url_when_enabled(self):
        if self.enabled and not self.base_url:
            raise ValueError("MLflow etkinleştirildiğinde sunucu URL'si zorunludur.")
        if self.ca_cert_file and not self.validate_tls:
            raise ValueError("Özel CA kullanılırken TLS doğrulaması açık olmalıdır.")
        return self


class MlflowServerSettingsOut(BaseModel):
    managed: bool
    enabled: bool
    base_url: str
    validate_tls: bool
    ca_cert_file: str
    timeout_seconds: int
    updated_at: datetime | None = None


class MlflowTestResult(BaseModel):
    success: bool
    message: str
    response_time_ms: int
    experiment_count: int
    model_count: int = 0
    server_version: str = ""
    tracking_available: bool = True
    registry_available: bool = False

