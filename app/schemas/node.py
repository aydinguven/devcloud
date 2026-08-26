from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.node import NodeStatus


class NodeCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    schedulable: bool = True
    labels: dict[str, str] = Field(default_factory=dict)


class NodeUpdate(BaseModel):
    enabled: bool | None = None
    schedulable: bool | None = None
    labels: dict[str, str] | None = None


class NodeLabelsUpdate(BaseModel):
    labels: dict[str, str] = Field(default_factory=dict)


class NodeOut(BaseModel):
    id: str
    name: str
    hostname: str
    enabled: bool
    schedulable: bool
    status: NodeStatus
    cpu_total: float
    memory_total_mb: int
    disk_total_mb: int
    cpu_percent: float = 0.0
    memory_used_mb: int = 0
    disk_used_mb: int = 0
    active_containers_count: int = 0
    labels: dict[str, str]
    capabilities: dict
    agent_version: str
    last_seen_at: datetime | None
    created_at: datetime
    connected: bool = False


class NodeCreated(NodeOut):
    enrollment_token: str


class NodeHeartbeat(BaseModel):
    hostname: str = Field(default="", max_length=255)
    cpu_total: float = Field(ge=0, le=4096)
    memory_total_mb: int = Field(ge=0)
    disk_total_mb: int = Field(ge=0)
    cpu_percent: float = Field(default=0.0, ge=0, le=100)
    memory_used_mb: int = Field(default=0, ge=0)
    disk_used_mb: int = Field(default=0, ge=0)
    active_containers_count: int = Field(default=0, ge=0)
    capabilities: dict = Field(default_factory=dict)
    agent_version: str = Field(default="", max_length=64)

    @field_validator("capabilities")
    @classmethod
    def capabilities_must_be_object(cls, value: dict) -> dict:
        return value

