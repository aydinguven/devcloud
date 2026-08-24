import logging
from sqlalchemy.ext.asyncio import AsyncSession
from app.auth.base import AuthProvider
from app.models.user import User
from app.schemas.user import UserCreate

logger = logging.getLogger("devcloud.auth.ad")


class ActiveDirectoryAuthProvider(AuthProvider):
    """Active Directory / LDAP Authentication Provider (Ready for Enterprise AD Integration)."""

    def __init__(
        self,
        ldap_server: str = "ldap://ad.example.com",
        domain: str = "EXAMPLE.COM",
        base_dn: str = "dc=example,dc=com",
    ):
        self.ldap_server = ldap_server
        self.domain = domain
        self.base_dn = base_dn

    @property
    def provider_name(self) -> str:
        return "active_directory"

    async def authenticate(
        self, username: str, password: str, db: AsyncSession
    ) -> User | None:
        """Authenticate against Active Directory / LDAP.
        
        To connect to real AD:
        1. Install python-ldap or ldap3
        2. Bind to server using ldap_server + f"{username}@{self.domain}"
        3. Check/sync user into local DB with auth_source="active_directory"
        """
        logger.warning(
            "Active Directory provider invoked. Configure LDAP connection parameters in production."
        )
        # Placeholder returning None for unconfigured AD
        return None

    async def create_user(
        self, user_in: UserCreate, db: AsyncSession, is_admin: bool = False
    ) -> User:
        """User management is typically delegated to AD; placeholder for sync."""
        raise NotImplementedError("Users are provisioned via Active Directory domain controller.")

    async def change_password(
        self, user: User, new_password: str, db: AsyncSession
    ) -> bool:
        """Password changes are delegated to AD."""
        raise NotImplementedError("Password changes must be done via Active Directory.")
