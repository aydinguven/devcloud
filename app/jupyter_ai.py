"""Shared Jupyter AI model-catalog compatibility helpers."""

from __future__ import annotations

import json


DEFAULT_JUPYTER_AI_MODELS = (
    {
        "model_id": "qwen3.6-35b",
        "name": "Qwen 3.6 35B (On-Prem)",
        "description": "On-Prem",
    },
    {
        "model_id": "openrouter/z-ai/glm-5.2",
        "name": "GLM 5.2 (Online)",
        "description": "Online",
    },
    {
        "model_id": "openrouter/deepseek/deepseek-v4-pro",
        "name": "DeepSeek V4 Pro (Online)",
        "description": "Online",
    },
    {
        "model_id": "openrouter/qwen/qwen3-coder",
        "name": "Qwen 3 Coder (Online)",
        "description": "Online",
    },
    {
        "model_id": "openrouter/moonshotai/kimi-k2.6",
        "name": "Kimi k2.6 (Online)",
        "description": "Online",
    },
)


def default_model_catalog() -> list[dict[str, str]]:
    return [dict(model) for model in DEFAULT_JUPYTER_AI_MODELS]


def default_model_catalog_json() -> str:
    return json.dumps(default_model_catalog(), ensure_ascii=False)


def parse_model_catalog(raw: str, default_model: str = "") -> list[dict[str, str]]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        value = []
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("model_id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            models.append(
                {
                    "model_id": model_id,
                    "name": str(item.get("name") or model_id).strip() or model_id,
                    "description": str(item.get("description") or "").strip(),
                }
            )
    if default_model and default_model not in seen:
        models.insert(
            0,
            {
                "model_id": default_model,
                "name": default_model,
                "description": "Default",
            },
        )
    return models


def model_environment(
    default_model: str,
    models: list[dict[str, str]],
    *,
    discovery_enabled: bool,
) -> dict[str, str]:
    """Translate the central catalog into Claude Code picker configuration."""
    catalog = parse_model_catalog(json.dumps(models), default_model)
    if not default_model or not catalog:
        return {
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": (
                "1" if discovery_enabled else "0"
            )
        }

    environment = {
        "ANTHROPIC_MODEL": default_model,
        "ANTHROPIC_SMALL_FAST_MODEL": default_model,
        "CLAUDE_AVAILABLE_MODELS": ",".join(
            item["model_id"] for item in catalog
        ),
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": (
            "1" if discovery_enabled else "0"
        ),
    }
    slots = (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_CUSTOM_MODEL_OPTION",
    )
    for prefix, model in zip(slots, catalog):
        environment[prefix] = model["model_id"]
        environment[f"{prefix}_NAME"] = model["name"]
        environment[f"{prefix}_DESCRIPTION"] = model["description"]
        environment[f"{prefix}_SUPPORTED_CAPABILITIES"] = "none"
    if len(catalog) >= 4:
        environment["ANTHROPIC_SMALL_FAST_MODEL"] = catalog[3]["model_id"]
    return environment
