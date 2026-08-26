from pydantic import BaseModel, Field, field_validator


class MlflowSettingsUpdate(BaseModel):
    enabled: bool = False
    base_url: str = Field(default="", max_length=1024)
    auth_type: str = "none"
    username: str = Field(default="", max_length=255)
    secret: str | None = Field(default=None, max_length=4096)
    validate_tls: bool = True
    ca_cert_file: str = Field(default="", max_length=512)
    timeout_seconds: int = Field(default=10, ge=1, le=120)

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"none", "basic", "bearer"}:
            raise ValueError("auth_type none, basic veya bearer olmalıdır")
        return normalized

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")


class MlflowSettingsOut(BaseModel):
    enabled: bool
    base_url: str
    auth_type: str
    username: str
    has_secret: bool
    validate_tls: bool
    ca_cert_file: str
    timeout_seconds: int


class MlflowTestResult(BaseModel):
    success: bool
    message: str
    response_time_ms: int
    model_count: int

