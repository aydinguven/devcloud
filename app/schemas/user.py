from datetime import datetime
from pydantic import BaseModel, EmailStr, Field
from app.models.user import UserRole


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_\-]+$")
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=100)
    full_name: str = Field(default="", max_length=100)


class UserLogin(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    full_name: str
    team: str
    directorate: str
    organization_unit: str = ""
    managed_unit: str = ""
    role: UserRole
    auth_source: str
    is_active: bool
    # Effective quota (override -> team -> müdürlük -> default); see app/quotas.py.
    cpu_quota: float
    memory_mb_quota: int
    disk_mb_quota: int
    gpu_quota: int
    cpu_quota_override: float | None = None
    memory_mb_quota_override: int | None = None
    disk_mb_quota_override: int | None = None
    gpu_quota_override: int | None = None
    quota_sources: dict[str, str] = Field(default_factory=dict)
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = None
    password: str | None = Field(default=None, min_length=6, max_length=100)


class UserQuotaUpdate(BaseModel):
    """Per-user overrides. An explicit ``null`` inherits the team/default value;
    an omitted field keeps the current override."""

    cpu_quota: float | None = Field(default=None, ge=0, le=256)
    memory_mb_quota: int | None = Field(default=None, ge=0, le=1048576)
    disk_mb_quota: int | None = Field(default=None, ge=0, le=1073741824)
    gpu_quota: int | None = Field(default=None, ge=0, le=64)


class GroupQuotaUpdate(BaseModel):
    """Per-member quota for one team or müdürlük; both empty is the default group.

    Set ``team`` for a team or ``unit`` for a müdürlük, not both. ``null``
    inherits the next level (team -> müdürlük -> default); the default group
    needs every value.
    """

    team: str = Field(default="", max_length=255)
    unit: str = Field(default="", max_length=255)
    cpu_quota: float | None = Field(default=None, ge=0, le=256)
    memory_mb_quota: int | None = Field(default=None, ge=0, le=1048576)
    disk_mb_quota: int | None = Field(default=None, ge=0, le=1073741824)
    gpu_quota: int | None = Field(default=None, ge=0, le=64)


class GroupQuotaOut(BaseModel):
    id: int | None = None
    key: str
    display_name: str
    is_default: bool
    configured: bool
    kind: str = "team"  # default | team | unit (müdürlük)
    organization_unit: str = ""
    # Raw group values; null inherits the next level.
    cpu_quota: float | None = None
    memory_mb_quota: int | None = None
    disk_mb_quota: int | None = None
    gpu_quota: int | None = None
    # What a member without overrides receives.
    inherited: dict[str, float | int]
    member_count: int
    override_count: int
