import ipaddress
import re
from urllib.parse import urlsplit


HOSTNAME_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$"
)


def normalize_public_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    hostname = parsed.hostname or ""
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Controller URL geçerli bir port içermelidir") from exc
    try:
        ipaddress.ip_address(hostname)
        valid_hostname = True
    except ValueError:
        valid_hostname = bool(HOSTNAME_PATTERN.fullmatch(hostname))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not valid_hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError(
            "Controller URL yalnızca http:// veya https:// ile başlayan host ve isteğe bağlı port içermelidir"
        )
    return normalized
