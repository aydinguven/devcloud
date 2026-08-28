from datetime import datetime
from pydantic import BaseModel, Field
from app.models.workspace import WorkspaceStatus


class FlavorInfo(BaseModel):
    id: str
    name: str
    description: str
    cpus: float
    memory_mb: int
    memory_display: str


class TemplateInfo(BaseModel):
    id: str
    name: str
    description: str
    category: str
    icon: str
    default_port: int
    image_tag: str
    features: list[str]


class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=60, pattern=r"^[a-zA-Z0-9_\-\ ]+$")
    description: str = Field(default="", max_length=255)
    template_id: str
    flavor_id: str
    auto_stop_minutes: int = Field(default=0, ge=0, le=1440)


class WorkspaceOut(BaseModel):
    id: str
    name: str
    description: str
    user_id: int
    node_id: str
    template_id: str
    flavor_id: str
    container_name: str
    host_port: int
    container_port: int
    status: WorkspaceStatus
    storage_path: str
    auto_stop_minutes: int = 0
    created_at: datetime
    last_started_at: datetime | None = None
    last_stopped_at: datetime | None = None
    error_message: str | None = None
    web_url: str | None = None

    model_config = {"from_attributes": True}


class WorkspaceStatusOut(BaseModel):
    id: str
    status: WorkspaceStatus
    container_id: str | None = None
    host_port: int
    is_running: bool
    logs: str | None = None
