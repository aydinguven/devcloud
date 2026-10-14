"""TCMB Code configuration generated from the controller-managed AI gateway.

TCMB Code is TCMB's build of Kilo Code. Its bundled CLI and the extension both
read the global config file ``~/.config/kilo/tcmb.jsonc``. The file written into
a workspace holds two keys:

* the workspace owner's own AI Factory (LiteLLM) key, for chat requests;
* the shared indexing key from Admin, for codebase-indexing embeddings.

Neither key nor any internal host name is baked into the image.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.cline import openai_compatible_base_url

TCMB_CODE_ASSISTANT = "tcmb-code"
# First worker release that accepts tcmb_code_config and knows the vscode-tcmb
# template; older workers silently start the image with no tcmb.jsonc.
MIN_AGENT_VERSION = "3.18.0"
EXTENSION_ID = "tcmb.tcmbcode"
CONFIG_SCHEMA = "https://app.kilo.ai/config.json"
CONFIG_DIR = "/home/coder/.config/kilo"
CONFIG_FILENAME = "tcmb.jsonc"
PROVIDER_ID = "litellm"
PROVIDER_NAME = "TCMB code"
# Shown in TCMB Code's model picker when the owner has no AI Factory key yet.
MISSING_KEY_PROVIDER_NAME = (
    "TCMB code (anahtar yok: GenAI sekmesinden AI Factory anahtarı talep edin)"
)
MAX_CONFIG_LENGTH = 65536
CONFIG_DIGEST_SALT = b"devcloud-tcmb-code-config-v2\x00"
DEFAULT_INDEXING_MODEL = "bge-m3"
DEFAULT_INDEXING_DIMENSION = 1024


def parse_model_ids(raw: str | None) -> list[str]:
    """Return the stored TCMB Code model allow-list; empty means the whole catalog."""
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        model_id = str(value or "").strip()
        if model_id and model_id not in result:
            result.append(model_id)
    return result


def select_models(
    catalog: list[dict[str, str]],
    model_ids: list[str],
    default_model: str,
) -> tuple[list[dict[str, str]], str]:
    """Pick the catalog models offered in TCMB Code and its default model."""
    models = [item for item in catalog if not model_ids or item["model_id"] in model_ids]
    if not models:
        models = list(catalog)
    if not models:
        return [], ""
    ids = [item["model_id"] for item in models]
    return models, default_model if default_model in ids else ids[0]


def render_config(
    *,
    gateway_url: str,
    models: list[dict[str, str]],
    default_model: str,
    user_key: str,
    library_url: str = "",
    indexing_enabled: bool = False,
    indexing_model: str = DEFAULT_INDEXING_MODEL,
    indexing_dimension: int = DEFAULT_INDEXING_DIMENSION,
    indexing_key: str = "",
) -> str:
    """Build the global tcmb.jsonc content as deterministic JSON (valid JSONC)."""
    base_url = openai_compatible_base_url(gateway_url)
    if not base_url or not models or not default_model:
        return ""
    options: dict[str, Any] = {"baseURL": base_url}
    if user_key:
        options["apiKey"] = user_key
    config: dict[str, Any] = {
        "$schema": CONFIG_SCHEMA,
        "disabled_providers": [],
        "model": f"{PROVIDER_ID}/{default_model}",
        "provider": {
            PROVIDER_ID: {
                "name": PROVIDER_NAME if user_key else MISSING_KEY_PROVIDER_NAME,
                "npm": "@ai-sdk/openai-compatible",
                "options": options,
                "models": {
                    item["model_id"]: {
                        "name": item.get("name") or item["model_id"],
                        "reasoning": True,
                    }
                    for item in models
                },
            }
        },
    }
    if library_url:
        config["tcmbLibrary"] = {"url": library_url.rstrip("/")}
    if indexing_enabled and indexing_key and indexing_model:
        config["indexing"] = {
            "provider": "openai-compatible",
            "openai-compatible": {"baseUrl": base_url, "apiKey": indexing_key},
            "dimension": int(indexing_dimension),
            "model": indexing_model,
            "vectorStore": "lancedb",
            "enabled": True,
        }
    return json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validated_config(raw: Any) -> str:
    """Return a controller-rendered config only when it is a bounded JSON object."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_CONFIG_LENGTH:
        return ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ""
    return raw if isinstance(parsed, dict) else ""


def config_digest(config: str) -> str:
    """Fingerprint of the rendered config; changes when a key or setting changes.

    Salted with a format version: 3.18.0/3.18.1 stored the digest of the config
    they SENT, even when a pre-3.18 worker dropped it. A new salt makes every
    such row mismatch once, so the next start rebuilds the container.
    """
    if not config:
        return ""
    return hashlib.sha256(
        CONFIG_DIGEST_SALT + config.encode("utf-8")
    ).hexdigest()


def managed_vscode_settings(config: str = "") -> str:
    """VS Code user settings: no auto-updates, no native Chat, our default model."""
    values: dict[str, Any] = {
        "extensions.autoCheckUpdates": False,
        "extensions.autoUpdate": False,
        "chat.disableAIFeatures": True,
    }
    model = ""
    if config:
        try:
            model = str(json.loads(config).get("model") or "")
        except (ValueError, AttributeError):
            model = ""
    provider_id, separator, model_id = model.partition("/")
    if separator and provider_id and model_id:
        # Without these the extension starts on Kilo's own cloud default model.
        values["kilo-code.new.model.providerID"] = provider_id
        values["kilo-code.new.model.modelID"] = model_id
    return json.dumps(values, separators=(",", ":"))
