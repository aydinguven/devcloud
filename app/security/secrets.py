import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class SecretDecryptionError(ValueError):
    pass


def _cipher() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(secret: str) -> str:
    if not secret:
        return ""
    return _cipher().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        return _cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptionError("Kayıtlı gizli değer çözülemedi.") from exc

