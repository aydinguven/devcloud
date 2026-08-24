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
    role: UserRole
    auth_source: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    full_name: str | None = None
    password: str | None = Field(default=None, min_length=6, max_length=100)
