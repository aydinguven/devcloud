from datetime import datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class TcmbCodeSettingsUpdate(BaseModel):
    enabled: bool = False
    library_url: str = Field(default="", max_length=512)
    # Workspace AI catalog model IDs offered in TCMB Code; empty = whole catalog.
    model_ids: list[str] = Field(default_factory=list, max_length=50)
    indexing_enabled: bool = True
    indexing_model: str = Field(default="bge-m3", max_length=255)
    indexing_dimension: int = Field(default=1024, ge=1, le=16384)
    # None keeps the stored key; "" clears it.
    indexing_key: str | None = Field(default=None, max_length=4096)

    @field_validator("library_url", "indexing_model", mode="after")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("library_url")
    @classmethod
    def validate_library_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "TCMB Library URL http:// veya https:// ile başlayan geçerli bir adres olmalıdır."
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "TCMB Library URL kullanıcı bilgisi, query veya fragment içermemelidir."
            )
        return value.rstrip("/")

    @field_validator("model_ids", mode="after")
    @classmethod
    def validate_model_ids(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            model_id = str(value or "").strip()
            if not model_id:
                continue
            if any(character.isspace() or ord(character) < 32 for character in model_id):
                raise ValueError("Model kimliği boşluk veya kontrol karakteri içeremez.")
            if model_id not in result:
                result.append(model_id)
        return result

    @field_validator("indexing_model")
    @classmethod
    def validate_indexing_model(cls, value: str) -> str:
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("İndeksleme modeli boşluk veya kontrol karakteri içeremez.")
        return value

    @field_validator("indexing_key")
    @classmethod
    def validate_indexing_key(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("İndeksleme API anahtarı boşluk veya kontrol karakteri içeremez.")
        return value

    @model_validator(mode="after")
    def require_indexing_model(self):
        if self.indexing_enabled and not self.indexing_model:
            raise ValueError("Kod indeksleme etkinken embedding modeli zorunludur.")
        return self


class TcmbCodeSettingsOut(BaseModel):
    managed: bool
    enabled: bool
    library_url: str
    model_ids: list[str]
    indexing_enabled: bool
    indexing_model: str
    indexing_dimension: int
    has_indexing_key: bool
    # True when new TCMB Code workspaces receive a config right now.
    active: bool = False
    # Why the config is not delivered, or what is limited ("" when fine).
    warning: str = ""
    updated_at: datetime | None = None
