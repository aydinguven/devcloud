from pydantic import BaseModel, Field


class CustomTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, min_length=1, max_length=50)
    default_port: int | None = Field(default=None, ge=1, le=65535)
