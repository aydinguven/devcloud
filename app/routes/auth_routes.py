from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    AuthProvider,
    get_auth_provider,
    get_current_user,
    create_access_token,
)
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.directory_settings import DirectorySettings
from app.schemas.auth import TokenResponse
from app.schemas.user import UserCreate, UserLogin, UserOut, UserUpdate

auth_router = APIRouter(prefix="/api/auth", tags=["Authentication"])


def set_session_cookie(response: Response, token: str) -> None:
    """Set the browser session cookie with production-safe options."""
    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Expire the session cookie using the same attributes used to set it."""
    response.delete_cookie(
        key=settings.COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
    )


@auth_router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register_user(
    user_in: UserCreate,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth_provider: Annotated[AuthProvider, Depends(get_auth_provider)],
):
    """Register a new user account."""
    directory_settings = await db.get(DirectorySettings, 1)
    if directory_settings and directory_settings.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Kurumsal dizin etkin; yeni hesaplar ilk LDAP girişinde otomatik oluşturulur.",
        )

    # Check if username exists
    stmt_user = select(User).where(User.username == user_in.username.strip())
    existing_user = (await db.execute(stmt_user)).scalar_one_or_none()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bu kullanıcı adı zaten kullanılıyor.",
        )

    # Check if email exists
    stmt_email = select(User).where(User.email == user_in.email.strip().lower())
    existing_email = (await db.execute(stmt_email)).scalar_one_or_none()
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bu e-posta adresi zaten kayıtlı.",
        )

    user = await auth_provider.create_user(user_in, db)

    # Generate JWT token
    token = create_access_token({"sub": user.username, "user_id": user.id, "role": user.role.value})
    
    set_session_cookie(response, token)

    return TokenResponse(access_token=token, token_type="bearer", user=UserOut.model_validate(user))


@auth_router.post("/login", response_model=TokenResponse)
async def login_user(
    login_data: UserLogin,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth_provider: Annotated[AuthProvider, Depends(get_auth_provider)],
):
    """Authenticate user and return JWT access token."""
    user = await auth_provider.authenticate(login_data.username, login_data.password, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Kullanıcı adı veya parola hatalı.",
        )

    token = create_access_token({"sub": user.username, "user_id": user.id, "role": user.role.value})

    set_session_cookie(response, token)

    return TokenResponse(access_token=token, token_type="bearer", user=UserOut.model_validate(user))


@auth_router.post("/logout")
async def logout_user(response: Response):
    """Log out user by clearing the session cookie."""
    clear_session_cookie(response)
    return {"message": "Çıkış yapıldı."}


@auth_router.get("/me", response_model=UserOut)
async def get_me(current_user: Annotated[User, Depends(get_current_user)]):
    """Get current authenticated user profile."""
    return UserOut.model_validate(current_user)


@auth_router.put("/profile", response_model=UserOut)
async def update_profile(
    update_data: UserUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    auth_provider: Annotated[AuthProvider, Depends(get_auth_provider)],
):
    """Update current user profile information."""
    if update_data.email:
        stmt = select(User).where(User.email == update_data.email.lower(), User.id != current_user.id)
        if (await db.execute(stmt)).scalar_one_or_none():
            raise HTTPException(status_code=400, detail="E-posta adresi başka bir hesap tarafından kullanılıyor.")
        current_user.email = update_data.email.lower()

    if update_data.full_name is not None:
        current_user.full_name = update_data.full_name.strip()

    if update_data.password:
        changed = await auth_provider.change_password(
            current_user, update_data.password, db
        )
        if not changed:
            raise HTTPException(
                status_code=400,
                detail="Kurumsal dizin parolası DevCloud üzerinden değiştirilemez.",
            )

    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)

    return UserOut.model_validate(current_user)
