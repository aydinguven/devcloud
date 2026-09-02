from pydantic import BaseModel, Field


class FlavorSettingsUpdate(BaseModel):
    enabled: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=255)
    cpus: float | None = Field(default=None, gt=0, le=256)
    memory_mb: int | None = Field(default=None, ge=128, le=1048576)
    accelerator_count: int | None = Field(default=None, ge=0, le=16)
    accelerator_vendor: str | None = Field(default=None, max_length=50)
    accelerator_memory_mb: int | None = Field(default=None, ge=0, le=1048576)


class FlavorCreate(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=255)
    cpus: float = Field(gt=0, le=256)
    memory_mb: int = Field(ge=128, le=1048576)
    accelerator_count: int = Field(default=0, ge=0, le=16)
    accelerator_vendor: str = Field(default="", max_length=50)
    accelerator_memory_mb: int = Field(default=0, ge=0, le=1048576)
    enabled: bool = True
