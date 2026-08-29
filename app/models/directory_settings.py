from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DirectorySettings(Base):
    """Singleton configuration for the LDAP/Active Directory integration."""

    __tablename__ = "directory_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    server_host: Mapped[str] = mapped_column(
        String(255), default="ldaps.tcmb.gov.tr", nullable=False
    )
    server_port: Mapped[int] = mapped_column(Integer, default=686, nullable=False)
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    validate_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ca_cert_file: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    connect_timeout_seconds: Mapped[int] = mapped_column(
        Integer, default=10, nullable=False
    )

    bind_dn: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    encrypted_bind_password: Mapped[str] = mapped_column(
        Text, default="", nullable=False
    )

    user_base_dn: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    user_filter: Mapped[str] = mapped_column(
        String(512), default="(&(objectClass=user)(sAMAccountName={username}))", nullable=False
    )
    username_attribute: Mapped[str] = mapped_column(
        String(128), default="sAMAccountName", nullable=False
    )
    email_attribute: Mapped[str] = mapped_column(
        String(128), default="mail", nullable=False
    )
    display_name_attribute: Mapped[str] = mapped_column(
        String(128), default="displayName", nullable=False
    )
    team_attribute: Mapped[str] = mapped_column(
        String(128), default="department", server_default="department", nullable=False
    )
    directorate_attribute: Mapped[str] = mapped_column(
        String(128), default="division", server_default="division", nullable=False
    )
    group_membership_attribute: Mapped[str] = mapped_column(
        String(128), default="memberOf", nullable=False
    )

    required_group_dn: Mapped[str] = mapped_column(
        String(512), default="", nullable=False
    )
    admin_group_dn: Mapped[str] = mapped_column(
        String(512), default="", nullable=False
    )
    nested_group_search: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
