from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.custom_template import CustomTemplate
from app.models.flavor_settings import FlavorSettings
from app.models.workspace_image import WorkspaceImage
from app.orchestrator.flavors import get_flavor, list_flavors
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
    return [
        template
        for template in list_templates()
        if template.id not in managed or managed[template.id]
    ]


async def configured_flavors(db: AsyncSession) -> list[FlavorInfo]:
    settings = {
        record.flavor_id: record.enabled
        for record in (
            await db.execute(select(FlavorSettings))
        ).scalars().all()
    }
    return [
        flavor.model_copy(update={"enabled": settings.get(flavor.id, True)})
        for flavor in list_flavors()
    ]


async def list_enabled_flavors(db: AsyncSession) -> list[FlavorInfo]:
    return [flavor for flavor in await configured_flavors(db) if flavor.enabled]


async def flavor_enabled(db: AsyncSession, flavor_id: str) -> bool:
    flavor = get_flavor(flavor_id)
    if not flavor or not flavor.selectable:
        return False
    record = await db.get(FlavorSettings, flavor_id)
    return record is None or record.enabled
