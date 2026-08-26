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
    worker_bootstrap_url: str
