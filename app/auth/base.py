from abc import ABC, abstractmethod
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.schemas.user import UserCreate


class AuthProvider(ABC):
    """Abstract interface for authentication providers (Internal DB, Active Directory, LDAP)."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name identifier of the auth provider."""
        pass

    @abstractmethod
    async def authenticate(
        self, username: str, password: str, db: AsyncSession
    ) -> User | None:
        """Authenticate user credentials and return the User model if valid."""
        pass

    @abstractmethod
    async def create_user(
        self, user_in: UserCreate, db: AsyncSession, is_admin: bool = False
    ) -> User:
        """Create a new user within the authentication system."""
        pass

    @abstractmethod
    async def change_password(
        self, user: User, new_password: str, db: AsyncSession
    ) -> bool:
        """Change the password for a user."""
        pass
