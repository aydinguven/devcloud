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
    role: UserRole
    auth_source: str
    is_active: bool
    cpu_quota: float
    memory_mb_quota: int
    disk_mb_quota: int
    gpu_quota: int
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = None
    password: str | None = Field(default=None, min_length=6, max_length=100)


class UserQuotaUpdate(BaseModel):
    cpu_quota: float = Field(..., ge=0, le=256)
    memory_mb_quota: int = Field(..., ge=0, le=1048576)
    disk_mb_quota: int = Field(..., ge=0, le=1073741824)
    gpu_quota: int | None = Field(default=None, ge=0, le=64)
