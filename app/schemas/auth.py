from pydantic import BaseModel
from app.schemas.user import UserOut


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class TokenPayload(BaseModel):
    sub: str  # user id or username
    user_id: int
    role: str
    exp: int
