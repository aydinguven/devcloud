import time
from dataclasses import dataclass

import httpx

from app.models.mlflow_settings import MlflowSettings
from app.schemas.mlflow import MlflowSettingsUpdate
from app.security.secrets import SecretDecryptionError, decrypt_secret


class MlflowConfigurationError(ValueError):
    pass


class MlflowConnectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MlflowConfig:
    enabled: bool
    base_url: str
    auth_type: str
    username: str
    secret: str
    validate_tls: bool
    ca_cert_file: str
    timeout_seconds: int


def config_from_record(record: MlflowSettings) -> MlflowConfig:
    try:
        secret = decrypt_secret(record.encrypted_secret)
    except SecretDecryptionError as exc:
        raise MlflowConfigurationError(
            "Kayıtlı MLflow parolası/token'ı çözülemedi; yeniden kaydedin."
        ) from exc
    return MlflowConfig(
        enabled=record.enabled,
        base_url=record.base_url,
        auth_type=record.auth_type,
        username=record.username,
        secret=secret,
        validate_tls=record.validate_tls,
        ca_cert_file=record.ca_cert_file,
        timeout_seconds=record.timeout_seconds,
    )


def config_from_update(update: MlflowSettingsUpdate, record: MlflowSettings | None = None) -> MlflowConfig:
    secret = update.secret or ""
    if not secret and record:
        try:
            secret = decrypt_secret(record.encrypted_secret)
        except SecretDecryptionError as exc:
            raise MlflowConfigurationError(
                "Kayıtlı MLflow parolası/token'ı çözülemedi; yeniden kaydedin."
            ) from exc
    return MlflowConfig(
        enabled=update.enabled,
        base_url=update.base_url,
        auth_type=update.auth_type,
        username=update.username,
        secret=secret,
        validate_tls=update.validate_tls,
        ca_cert_file=update.ca_cert_file,
        timeout_seconds=update.timeout_seconds,
    )


def validate_config(config: MlflowConfig, require_enabled: bool = False) -> None:
    if require_enabled and not config.enabled:
        raise MlflowConfigurationError("MLflow entegrasyonu etkin değil.")
    if not config.base_url.startswith(("http://", "https://")):
        raise MlflowConfigurationError("MLflow adresi http:// veya https:// ile başlamalıdır.")
    parsed = httpx.URL(config.base_url)
    if not parsed.host or parsed.username or parsed.password:
        raise MlflowConfigurationError("Geçerli ve kullanıcı bilgisi içermeyen bir MLflow adresi girin.")
    if config.auth_type == "basic" and (not config.username or not config.secret):
        raise MlflowConfigurationError("Basic authentication için kullanıcı adı ve parola gereklidir.")
    if config.auth_type == "bearer" and not config.secret:
        raise MlflowConfigurationError("Bearer authentication için token gereklidir.")
    if config.ca_cert_file and not config.validate_tls:
        raise MlflowConfigurationError("Özel CA kullanılırken TLS doğrulaması açık olmalıdır.")


class MlflowClient:
    def __init__(self, config: MlflowConfig):
        validate_config(config)
        self.config = config

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        auth = None
        if self.config.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.config.secret}"
        elif self.config.auth_type == "basic":
            auth = (self.config.username, self.config.secret)
        verify: bool | str = self.config.validate_tls
        if self.config.ca_cert_file:
            verify = self.config.ca_cert_file
        return httpx.AsyncClient(
            base_url=self.config.base_url,
            headers=headers,
            auth=auth,
            verify=verify,
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
        )

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            async with self._client() as client:
                response = await client.get(path, params=params)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MlflowConnectionError(f"MLflow API isteği başarısız: {exc}") from exc

    async def search_registered_models(
        self,
        search: str = "",
        page_token: str = "",
        max_results: int = 100,
    ) -> dict:
        params: dict[str, str | int | list[str]] = {
            "max_results": max(1, min(max_results, 200)),
            "order_by": ["last_updated_timestamp DESC"],
        }
        if search:
            safe_search = search.replace("'", "")
            params["filter"] = f"name LIKE '{safe_search}'"
        if page_token:
            params["page_token"] = page_token
        return await self._get("/api/2.0/mlflow/registered-models/search", params)

    async def get_registered_model(self, name: str) -> dict:
        return await self._get(
            "/api/2.0/mlflow/registered-models/get",
            {"name": name},
        )

    async def search_model_versions(self, name: str, max_results: int = 200) -> dict:
        if "'" in name:
            raise MlflowConfigurationError("Model adındaki tek tırnak API filtresiyle kullanılamıyor.")
        return await self._get(
            "/api/2.0/mlflow/model-versions/search",
            {
                "filter": f"name='{name}'",
                "max_results": max(1, min(max_results, 1000)),
                "order_by": ["version DESC"],
            },
        )

    async def test(self) -> tuple[int, int]:
        started = time.monotonic()
        payload = await self.search_registered_models(max_results=1)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        return len(payload.get("registered_models") or []), elapsed_ms


def normalize_model(model: dict) -> dict:
    versions = model.get("latest_versions") or []
    versions = sorted(
        versions,
        key=lambda item: int(item.get("version") or 0),
        reverse=True,
    )
    latest = versions[0] if versions else None
    aliases = model.get("aliases") or []
    if isinstance(aliases, dict):
        aliases = list(aliases.keys())
    return {
        **model,
        "tags_map": {item.get("key", ""): item.get("value", "") for item in model.get("tags") or []},
        "aliases_list": aliases,
        "latest_version": latest,
        "version_count": len(versions),
    }

