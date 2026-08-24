from typing import Annotated
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.base import AuthProvider
from app.auth.internal import InternalAuthProvider, decode_access_token
from app.auth.ad_placeholder import ActiveDirectoryAuthProvider
from app.config import settings
from app.database import get_db
from app.models.user import User, UserRole

http_bearer = HTTPBearer(auto_error=False)


def get_auth_provider() -> AuthProvider:
    """Factory returning configured AuthProvider."""
    if settings.AUTH_PROVIDER.lower() == "active_directory":
        return ActiveDirectoryAuthProvider()
    return InternalAuthProvider()


async def get_token_from_request(
    request: Request,
    bearer_creds: Annotated[HTTPAuthorizationCredentials | None, Depends(http_bearer)] = None,
) -> str | None:
    """Extract JWT token from Authorization header or cookie."""
    if bearer_creds and bearer_creds.credentials:
        return bearer_creds.credentials
    # Fallback to session cookie
    cookie_token = request.cookies.get(settings.COOKIE_NAME)
    if cookie_token:
        return cookie_token
    return None


async def get_current_user_optional(
    token: Annotated[str | None, Depends(get_token_from_request)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """Return current authenticated user if token is valid, otherwise None."""
    if not token:
        return None
    
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        return None
    
    user_id = payload.get("user_id")
    if not user_id:
        return None
    
    stmt = select(User).where(User.id == int(user_id))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    
    if not user or not user.is_active:
        return None
    return user


async def get_current_user(
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """Require an authenticated user or raise 401 Unauthorized."""
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Giriş yapmanız gerekiyor",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return current_user


async def get_current_admin_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Require an authenticated admin user or raise 403 Forbidden."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Yönetici yetkisi gerekiyor",
        )
    return current_user
