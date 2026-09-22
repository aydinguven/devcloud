import ipaddress

from pydantic import BaseModel, Field, field_validator

from app.download_config import normalize_public_base_url


class DownloadSettingsUpdate(BaseModel):
    public_base_url: str = Field(min_length=1, max_length=1024)
    worker_fallback_ipv4: str = Field(default="", max_length=15)

    @field_validator("public_base_url")
    @classmethod
    def normalize_public_base_url(cls, value: str) -> str:
        return normalize_public_base_url(value)

    @field_validator("worker_fallback_ipv4")
    @classmethod
    def normalize_worker_fallback_ipv4(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError as exc:
            raise ValueError("Worker fallback adresi geçerli bir IPv4 olmalıdır") from exc
        if (
            address.version != 4
            or address.is_unspecified
            or address.is_multicast
            or address.is_loopback
        ):
            raise ValueError("Worker fallback adresi kullanılabilir bir IPv4 olmalıdır")
        return str(address)


class DownloadSettingsOut(BaseModel):
    public_base_url: str
    worker_fallback_ipv4: str = ""
    https_enabled: bool
    https_hostname: str
    http_fallback_enabled: bool
    certificate_uploaded: bool
    agent_ca_uploaded: bool = False
    certificate_subject: str | None = None
    certificate_not_after: str | None = None
    certificate_sha256: str | None = None
