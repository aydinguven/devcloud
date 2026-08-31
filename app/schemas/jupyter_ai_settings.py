from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class JupyterAiSettingsUpdate(BaseModel):
    enabled: bool = False
    gateway_url: str = Field(default="", max_length=512)
    model_id: str = Field(default="", max_length=255)
    shared_token: str | None = Field(default=None, max_length=4096)

    @field_validator("gateway_url", "model_id", mode="after")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("gateway_url")
    @classmethod
    def validate_gateway_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "Gateway URL http:// veya https:// ile başlayan geçerli bir adres olmalıdır."
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Gateway URL kullanıcı bilgisi, query veya fragment içermemelidir."
            )
        return value.rstrip("/")

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("Model kimliği boşluk içeremez.")
        return value

    @model_validator(mode="after")
    def require_enabled_fields(self):
        if self.enabled and (not self.gateway_url or not self.model_id):
            raise ValueError(
                "Jupyter AI etkinleştirildiğinde gateway URL ve model kimliği zorunludur."
            )
        return self


class JupyterAiSettingsOut(BaseModel):
    managed: bool
    enabled: bool
    gateway_url: str
    model_id: str
    has_shared_token: bool
    updated_at: datetime | None = None


class WorkerJupyterAiSettings(BaseModel):
    managed: bool
    enabled: bool
    gateway_url: str = ""
    model_id: str = ""
    shared_token: str = ""
    updated_at: datetime | None = None
