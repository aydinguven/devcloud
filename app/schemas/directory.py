from pydantic import BaseModel, Field, field_validator


class DirectorySettingsUpdate(BaseModel):
    enabled: bool = False
    server_host: str = Field(default="ldaps.tcmb.gov.tr", min_length=1, max_length=255)
    server_port: int = Field(default=686, ge=1, le=65535)
    use_ssl: bool = True
    validate_tls: bool = True
    ca_cert_file: str = Field(default="", max_length=512)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=60)
    bind_dn: str = Field(default="", max_length=512)
    bind_password: str | None = Field(default=None, max_length=1024)
    user_base_dn: str = Field(default="", max_length=512)
    user_filter: str = Field(
        default="(&(objectClass=user)(sAMAccountName={username}))", max_length=512
    )
    username_attribute: str = Field(default="sAMAccountName", max_length=128)
    email_attribute: str = Field(default="mail", max_length=128)
    display_name_attribute: str = Field(default="displayName", max_length=128)
    group_membership_attribute: str = Field(default="memberOf", max_length=128)
    required_group_dn: str = Field(default="", max_length=512)
    admin_group_dn: str = Field(default="", max_length=512)
    nested_group_search: bool = True

    @field_validator(
        "server_host",
        "ca_cert_file",
        "bind_dn",
        "user_base_dn",
        "user_filter",
        "username_attribute",
        "email_attribute",
        "display_name_attribute",
        "group_membership_attribute",
        "required_group_dn",
        "admin_group_dn",
        mode="after",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("user_filter")
    @classmethod
    def require_username_placeholder(cls, value: str) -> str:
        if "{username}" not in value:
            raise ValueError("Kullanıcı filtresi {username} yer tutucusunu içermelidir.")
        return value


class DirectorySettingsOut(BaseModel):
    enabled: bool
    server_host: str
    server_port: int
    use_ssl: bool
    validate_tls: bool
    ca_cert_file: str
    connect_timeout_seconds: int
    bind_dn: str
    has_bind_password: bool
    user_base_dn: str
    user_filter: str
    username_attribute: str
    email_attribute: str
    display_name_attribute: str
    group_membership_attribute: str
    required_group_dn: str
    admin_group_dn: str
    nested_group_search: bool


class DirectoryTestResult(BaseModel):
    success: bool
    message: str
    server: str
    response_time_ms: int
