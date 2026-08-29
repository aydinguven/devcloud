from pydantic import BaseModel, Field, field_validator

from app.download_config import normalize_public_base_url


class DownloadSettingsUpdate(BaseModel):
    public_base_url: str = Field(min_length=1, max_length=1024)

    @field_validator("public_base_url")
    @classmethod
    def normalize_public_base_url(cls, value: str) -> str:
        return normalize_public_base_url(value)


class DownloadSettingsOut(BaseModel):
    public_base_url: str
    https_enabled: bool
    https_hostname: str
    http_fallback_enabled: bool
    certificate_uploaded: bool
    certificate_subject: str | None = None
    certificate_not_after: str | None = None
    certificate_sha256: str | None = None
