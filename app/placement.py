"""Workspace placement tags: datacenters, custom tags and GPU/CPU worker class.

Admins define the tag catalog (``PlacementTag``) and assign tags to workers.
Users pick a datacenter (or "Any") and optional custom tags when creating a
workspace; the scheduler only places the workspace on a worker that matches.
Whether a worker is a GPU or CPU worker is never a tag an admin types: it is
derived from the hardware the worker reports in its heartbeat.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.placement_tag import (
    TAG_KIND_CUSTOM,
    TAG_KIND_DATACENTER,
    PlacementTag,
)

TAG_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$"
_TAG_NAME_RE = re.compile(TAG_NAME_PATTERN)
# "Any" is the absence of a datacenter request; GPU/CPU is detected hardware.
RESERVED_TAG_NAMES = frozenset({"any", "gpu", "cpu"})
DEFAULT_DATACENTERS = (
    ("IDM", "IDM veri merkezi"),
    ("ODM", "ODM veri merkezi"),
)
MAX_WORKSPACE_TAGS = 10


class PlacementRequestError(ValueError):
    """A requested datacenter or tag is unknown, disabled or of the wrong kind."""


@dataclass(frozen=True)
class PlacementConstraints:
    datacenter: str = ""
    tags: tuple[str, ...] = ()

    def describe(self) -> str:
        parts = []
        if self.datacenter:
            parts.append(f"{self.datacenter} veri merkezi")
        if self.tags:
            parts.append("etiketler: " + ", ".join(self.tags))
        return "; ".join(parts) or "Herhangi bir veri merkezi"


def decode_tags(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return sorted({str(item) for item in value if str(item)})


def encode_tags(tags) -> str:
    return json.dumps(sorted({str(tag) for tag in tags if str(tag)}), ensure_ascii=False)


def valid_tag_name(name: str) -> bool:
    return bool(_TAG_NAME_RE.fullmatch(name or "")) and name.lower() not in RESERVED_TAG_NAMES


def is_gpu_worker(node) -> bool:
    """True when the worker reported NVIDIA hardware, healthy or not.

    A detected but unhealthy stack still counts so a transient driver/CDI
    fault does not suddenly open the GPU worker to CPU workspaces.
    """
    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(capabilities, dict):
        return False
    accelerators = capabilities.get("accelerators")
    if isinstance(accelerators, list) and any(isinstance(item, dict) for item in accelerators):
        return True
    runtime = capabilities.get("accelerator_runtime")
    nvidia = runtime.get("nvidia") if isinstance(runtime, dict) else None
    return isinstance(nvidia, dict) and nvidia.get("status") in {"ready", "error"}


def node_matches(node, constraints: PlacementConstraints | None) -> bool:
    if not constraints:
        return True
    if constraints.datacenter and (node.datacenter or "") != constraints.datacenter:
        return False
    return set(constraints.tags) <= set(decode_tags(node.tags_json))


async def load_tag_catalog(db: AsyncSession) -> dict[str, PlacementTag]:
    rows = (await db.execute(select(PlacementTag).order_by(PlacementTag.name))).scalars().all()
    return {row.name: row for row in rows}


def _normalize_datacenter(value: str | None) -> str:
    value = (value or "").strip()
    return "" if value.lower() == "any" else value


def _check(
    catalog: dict[str, PlacementTag],
    datacenter: str,
    tags: list[str],
    *,
    require_enabled: bool,
) -> None:
    if datacenter:
        entry = catalog.get(datacenter)
        if (
            entry is None
            or entry.kind != TAG_KIND_DATACENTER
            or (require_enabled and not entry.enabled)
        ):
            raise PlacementRequestError(f"Geçersiz veri merkezi: {datacenter}")
    for tag in tags:
        entry = catalog.get(tag)
        if (
            entry is None
            or entry.kind != TAG_KIND_CUSTOM
            or (require_enabled and not entry.enabled)
        ):
            raise PlacementRequestError(f"Geçersiz etiket: {tag}")


async def resolve_workspace_constraints(
    db: AsyncSession, datacenter: str | None, tags: list[str] | None
) -> PlacementConstraints:
    """Validate a user's create-form request against the enabled catalog."""
    datacenter = _normalize_datacenter(datacenter)
    clean_tags = sorted({tag.strip() for tag in tags or [] if tag.strip()})
    if len(clean_tags) > MAX_WORKSPACE_TAGS:
        raise PlacementRequestError(f"En fazla {MAX_WORKSPACE_TAGS} etiket seçilebilir.")
    _check(await load_tag_catalog(db), datacenter, clean_tags, require_enabled=True)
    return PlacementConstraints(datacenter=datacenter, tags=tuple(clean_tags))


async def validate_node_placement(
    db: AsyncSession, datacenter: str | None, tags: list[str] | None
) -> tuple[str, list[str]]:
    """Validate an admin's worker assignment; disabled tags remain assignable."""
    datacenter = _normalize_datacenter(datacenter)
    clean_tags = sorted({tag.strip() for tag in tags or [] if tag.strip()})
    _check(await load_tag_catalog(db), datacenter, clean_tags, require_enabled=False)
    return datacenter, clean_tags


async def placement_options(db: AsyncSession) -> dict[str, list[dict[str, str]]]:
    """Enabled datacenters and custom tags offered on the create form."""
    catalog = await load_tag_catalog(db)
    options: dict[str, list[dict[str, str]]] = {"datacenters": [], "tags": []}
    for entry in catalog.values():
        if not entry.enabled:
            continue
        key = "datacenters" if entry.kind == TAG_KIND_DATACENTER else "tags"
        options[key].append({"name": entry.name, "description": entry.description})
    return options
