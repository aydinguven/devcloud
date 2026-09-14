"""VS Code Chat configuration generated from the managed AI gateway."""

from __future__ import annotations

import json

from app.cline import openai_compatible_base_url
from app.jupyter_ai import parse_model_catalog


DEFAULT_MAX_INPUT_TOKENS = 120_000
DEFAULT_MAX_OUTPUT_TOKENS = 8_000


def managed_vscode_chat_files(
    gateway_url: str,
    api_key: str,
    default_model_id: str,
    model_catalog_json: str,
) -> dict[str, str]:
    """Build VS Code's native Custom Endpoint model and user settings files."""
    base_url = openai_compatible_base_url(gateway_url)
    default_model_id = str(default_model_id or "").strip()
    api_key = str(api_key or "")
    if not base_url or not api_key or not default_model_id:
        return {}

    models = parse_model_catalog(model_catalog_json, default_model_id)
    endpoint_url = f"{base_url}/chat/completions"
    provider = {
        "name": "DevCloud Gateway",
        "vendor": "customendpoint",
        "apiKey": api_key,
        "apiType": "chat-completions",
        "models": [
            {
                "id": model["model_id"],
                "name": model["name"],
                "url": endpoint_url,
                "toolCalling": True,
                "vision": False,
                "streaming": True,
                "maxInputTokens": DEFAULT_MAX_INPUT_TOKENS,
                "maxOutputTokens": DEFAULT_MAX_OUTPUT_TOKENS,
            }
            for model in models
        ],
    }
    user_settings = {
        "extensions.autoCheckUpdates": False,
        "extensions.autoUpdate": False,
        "chat.defaultModel": default_model_id,
        "chat.byokUtilityModelDefault": "mainAgent",
        "chat.titleBar.signIn.enabled": False,
    }
    compact = {"ensure_ascii": False, "separators": (",", ":")}
    return {
        "chatLanguageModels.json": json.dumps([provider], **compact),
        "settings.json": json.dumps(user_settings, **compact),
    }
