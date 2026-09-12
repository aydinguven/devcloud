from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.custom_template import CustomTemplate
from app.models.flavor_settings import FlavorSettings
from app.models.workspace_image import WorkspaceImage
from app.models.template_settings import TemplateSettings
from app.orchestrator.flavors import (
    BUILTIN_FLAVOR_IDS,
    Flavor,
    get_flavor,
    register_flavor,
)
from app.orchestrator.templates import list_templates, register_custom_template
from app.schemas.workspace import FlavorInfo, TemplateInfo


async def _load_custom_templates(db: AsyncSession) -> None:
    records = (await db.execute(select(CustomTemplate))).scalars().all()
    for record in records:
        register_custom_template(
            template_id=record.id,
            name=record.name,
            description=record.description,
            category=record.category,
            image_tag=record.image_tag,
            default_port=record.default_port,
            ide_type=record.ide_type,
            icon=record.icon,
        )


async def template_enabled(db: AsyncSession, template_id: str) -> bool:
    """Keep unmanaged templates visible; managed history requires an active image."""
    states = (
        await db.execute(
            select(WorkspaceImage.enabled).where(
                WorkspaceImage.template_id == template_id
            )
        )
    ).scalars().all()
    return not states or any(states)


async def list_enabled_templates(db: AsyncSession) -> list[TemplateInfo]:
    await _load_custom_templates(db)
    rows = (
        await db.execute(
            select(WorkspaceImage.template_id, WorkspaceImage.enabled)
        )
    ).all()
    managed: dict[str, bool] = {}
    for template_id, enabled in rows:
        managed[template_id] = managed.get(template_id, False) or enabled
    settings = {
        record.template_id: record
        for record in (await db.execute(select(TemplateSettings))).scalars().all()
    }
    templates = []
    for template in list_templates():
        record = settings.get(template.id)
        templates.append(
            template.model_copy(
                update={
                    "name": record.name or template.name,
                    "description": record.description or template.description,
                    "category": record.category or template.category,
                }
            )
            if record
            else template
        )
    return [
        template
        for template in templates
        if template.id not in managed or managed[template.id]
    ]


async def configured_templates(db: AsyncSession) -> list[TemplateInfo]:
    await _load_custom_templates(db)
    settings = {
        record.template_id: record
        for record in (await db.execute(select(TemplateSettings))).scalars().all()
    }
    return [
        template.model_copy(
            update={
                "name": settings[template.id].name or template.name,
                "description": settings[template.id].description or template.description,
                "category": settings[template.id].category or template.category,
            }
        )
        if template.id in settings
        else template
        for template in list_templates()
    ]


async def configured_flavors(db: AsyncSession) -> list[FlavorInfo]:
    records = (await db.execute(select(FlavorSettings))).scalars().all()
    settings = {record.flavor_id: record for record in records}
    configured: list[FlavorInfo] = []
    ordered_ids = [
        flavor_id
        for flavor_id in BUILTIN_FLAVOR_IDS
        if get_flavor(flavor_id) and get_flavor(flavor_id).selectable
    ] + sorted(
        record.flavor_id for record in records if record.is_custom
    )
    for flavor_id in ordered_ids:
        record = settings.get(flavor_id)
        base = get_flavor(flavor_id)
        if base is None and not record:
            continue
        if record and record.is_custom:
            base = Flavor(
                id=record.flavor_id,
                name=record.flavor_id,
                display_name=record.display_name,
                description=record.description,
                cpus=float(record.cpus or 1),
                memory_mb=int(record.memory_mb or 1024),
                memory_display=_memory_display(int(record.memory_mb or 1024)),
                accelerator_count=int(record.accelerator_count or 0),
                accelerator_vendor=record.accelerator_vendor,
                accelerator_memory_mb=int(record.accelerator_memory_mb or 0),
                accelerator_display=_accelerator_display(record),
            )
        elif record and base:
            base = Flavor(
                id=base.id,
                name=base.name,
                display_name=record.display_name or base.display_name,
                description=record.description or base.description,
                cpus=float(record.cpus if record.cpus is not None else base.cpus),
                memory_mb=int(record.memory_mb if record.memory_mb is not None else base.memory_mb),
                memory_display=_memory_display(int(record.memory_mb if record.memory_mb is not None else base.memory_mb)),
                accelerator_count=int(record.accelerator_count if record.accelerator_count is not None else base.accelerator_count),
                accelerator_vendor=record.accelerator_vendor or base.accelerator_vendor,
                accelerator_memory_mb=int(record.accelerator_memory_mb if record.accelerator_memory_mb is not None else base.accelerator_memory_mb),
                accelerator_display=base.accelerator_display,
                selectable=base.selectable,
            )
        if not base:
            continue
        register_flavor(base)
        configured.append(base.to_schema().model_copy(update={"enabled": record.enabled if record else True}))
    return configured


def _memory_display(memory_mb: int) -> str:
    return f"{memory_mb // 1024} GB" if memory_mb % 1024 == 0 else f"{memory_mb} MB"


def _accelerator_display(record: FlavorSettings) -> str:
    count = int(record.accelerator_count or 0)
    if not count:
        return ""
    memory = int(record.accelerator_memory_mb or 0)
    suffix = f" · {memory // 1024} GB+" if memory else ""
    return f"{count} {record.accelerator_vendor.upper() or 'GPU'} GPU{suffix}"


async def resolve_flavor(db: AsyncSession, flavor_id: str) -> Flavor | None:
    await configured_flavors(db)
    return get_flavor(flavor_id)


async def list_enabled_flavors(db: AsyncSession) -> list[FlavorInfo]:
    return [flavor for flavor in await configured_flavors(db) if flavor.enabled]


async def flavor_enabled(db: AsyncSession, flavor_id: str) -> bool:
    flavor = await resolve_flavor(db, flavor_id)
    if not flavor or not flavor.selectable:
        return False
    record = await db.get(FlavorSettings, flavor_id)
    return record is None or record.enabled
