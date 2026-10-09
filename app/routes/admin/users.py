"""Admin API: LDAP directory, users and team/müdürlük quotas."""

import asyncio
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.auth.ldap import (
    DirectoryConfigurationError,
    DirectoryConnectionError,
    DirectoryUnavailableError,
    config_from_update,
    encrypt_directory_secret,
    test_directory_configuration,
    validate_directory_config,
)
from app.database import get_db
from app.directory_sync import DirectorySyncDisabled, directory_teams_by_key, run_directory_sync
from app.models.user import User
from app.models.directory_settings import DirectorySettings
from app.schemas.user import GroupQuotaOut, GroupQuotaUpdate, UserOut, UserQuotaUpdate
from app.models.user_group_quota import DEFAULT_GROUP_KEY, UserGroupQuota
from app.quotas import (
    DEFAULT_GROUP_LABEL,
    QUOTA_FIELDS,
    build_group_views,
    build_hierarchy,
    clean_name,
    load_quota_groups,
    team_key,
    unit_key,
    user_out,
    user_out_for,
)
from app.schemas.directory import (
    DirectorySettingsOut,
    DirectorySettingsUpdate,
    DirectoryTestResult,
)

router = APIRouter()


def _directory_settings_out(record: DirectorySettings) -> DirectorySettingsOut:
    return DirectorySettingsOut(
        enabled=record.enabled,
        server_host=record.server_host,
        server_port=record.server_port,
        use_ssl=record.use_ssl,
        validate_tls=record.validate_tls,
        ca_cert_file=record.ca_cert_file,
        connect_timeout_seconds=record.connect_timeout_seconds,
        bind_dn=record.bind_dn,
        has_bind_password=bool(record.encrypted_bind_password),
        user_base_dn=record.user_base_dn,
        user_filter=record.user_filter,
        username_attribute=record.username_attribute,
        email_attribute=record.email_attribute,
        display_name_attribute=record.display_name_attribute,
        team_attribute=record.team_attribute,
        directorate_attribute=record.directorate_attribute,
        organization_unit_attribute=record.organization_unit_attribute,
        manager_attribute=record.manager_attribute,
        title_attribute=record.title_attribute,
        unit_head_titles=record.unit_head_titles,
        division_head_titles=record.division_head_titles,
        group_membership_attribute=record.group_membership_attribute,
        required_group_dn=record.required_group_dn,
        admin_group_dn=record.admin_group_dn,
        nested_group_search=record.nested_group_search,
    )


async def _get_or_create_directory_settings(
    db: AsyncSession,
) -> DirectorySettings:
    record = await db.get(DirectorySettings, 1)
    if record:
        return record
    record = DirectorySettings(id=1)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


@router.get("/directory-settings", response_model=DirectorySettingsOut)
async def get_directory_settings(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Return LDAP configuration without returning the bind password."""
    return _directory_settings_out(await _get_or_create_directory_settings(db))


@router.put("/directory-settings", response_model=DirectorySettingsOut)
async def update_directory_settings(
    update: DirectorySettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Store LDAP configuration and encrypt the bind password at rest."""
    record = await _get_or_create_directory_settings(db)
    try:
        candidate = config_from_update(update, record)
        if update.enabled:
            validate_directory_config(candidate)
    except DirectoryConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    values = update.model_dump(exclude={"bind_password"})
    for field_name, value in values.items():
        setattr(record, field_name, value)
    if update.bind_password:
        record.encrypted_bind_password = encrypt_directory_secret(
            update.bind_password
        )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _directory_settings_out(record)


@router.post("/directory-settings/test", response_model=DirectoryTestResult)
async def test_directory_settings(
    update: DirectorySettingsUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: Test the submitted (or saved) bind credentials and search base."""
    record = await _get_or_create_directory_settings(db)
    try:
        candidate = config_from_update(update, record)
        message, elapsed_ms = await asyncio.to_thread(
            test_directory_configuration, candidate
        )
    except (DirectoryConfigurationError, DirectoryConnectionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    scheme = "ldaps" if candidate.use_ssl else "ldap"
    return DirectoryTestResult(
        success=True,
        message=message,
        server=f"{scheme}://{candidate.server_host}:{candidate.server_port}",
        response_time_ms=elapsed_ms,
    )


@router.post("/directory-sync")
async def sync_directory(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: read every AD person, place teams under müdürlüks, refresh users."""
    try:
        return await run_directory_sync(db, trigger="manual")
    except DirectorySyncDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DirectoryConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DirectoryUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"Dizin okunamadı: {exc}") from exc


@router.get("/users", response_model=list[UserOut])
async def list_all_users(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: List all registered users."""
    stmt = select(User).order_by(User.id.asc())
    users = (await db.execute(stmt)).scalars().all()
    groups = await load_quota_groups(db)
    return [user_out(u, groups.resolve(u)) for u in users]


@router.put("/users/{user_id}/quota", response_model=UserOut)
async def update_user_quota(
    user_id: int,
    quota: UserQuotaUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: set per-user quota overrides; ``null`` inherits the team quota."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")
    for name in quota.model_fields_set:
        setattr(user, f"{name}_override", getattr(quota, name))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return await user_out_for(db, user)


@router.get("/user-groups", response_model=list[GroupQuotaOut])
async def list_user_groups(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: the default group, directory teams and müdürlüks with their quotas."""
    return [_group_quota_out(view) for view in await _quota_group_views(db)]


async def _quota_group_views(db: AsyncSession) -> list[dict]:
    users = (await db.execute(select(User))).scalars().all()
    groups = await load_quota_groups(db)
    views = build_group_views(users, groups, directory_teams=await directory_teams_by_key(db))
    return views + build_hierarchy(views, groups)["units"]


@router.put("/user-groups", response_model=GroupQuotaOut)
async def upsert_user_group_quota(
    payload: GroupQuotaUpdate,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: set the per-member quota of a team, a müdürlük or the default group."""
    if clean_name(payload.team) and clean_name(payload.unit):
        raise HTTPException(
            status_code=422, detail="Takım veya müdürlükten yalnızca biri seçilmelidir."
        )
    if clean_name(payload.unit):
        key, label = unit_key(payload.unit), clean_name(payload.unit)
    else:
        key, label = team_key(payload.team), clean_name(payload.team)
    values = {name: getattr(payload, name) for name in QUOTA_FIELDS}
    if key == DEFAULT_GROUP_KEY and any(value is None for value in values.values()):
        raise HTTPException(
            status_code=422,
            detail="Varsayılan grup için CPU, RAM, disk ve GPU değerlerinin tümü gereklidir.",
        )
    row = (
        await db.execute(select(UserGroupQuota).where(UserGroupQuota.group_key == key))
    ).scalar_one_or_none()
    if row is None:
        row = UserGroupQuota(
            group_key=key,
            display_name=DEFAULT_GROUP_LABEL if key == DEFAULT_GROUP_KEY else label,
        )
    for name, value in values.items():
        setattr(row, name, value)
    db.add(row)
    await db.commit()
    views = await _quota_group_views(db)
    return _group_quota_out(next(view for view in views if view["key"] == key))


@router.delete("/user-groups/{group_id}", status_code=204)
async def delete_user_group_quota(
    group_id: int,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Admin: remove a team or müdürlük quota; members fall back to the next level."""
    row = await db.get(UserGroupQuota, group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Grup kotası bulunamadı.")
    if row.group_key == DEFAULT_GROUP_KEY:
        raise HTTPException(status_code=400, detail="Varsayılan grup silinemez.")
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)


def _group_quota_out(view: dict) -> GroupQuotaOut:
    return GroupQuotaOut(
        id=view["id"],
        key=view["key"],
        display_name=view["display_name"],
        is_default=view["is_default"],
        configured=view["configured"],
        kind=view["kind"],
        organization_unit=view["organization_unit"],
        inherited=view["inherited"],
        member_count=view["member_count"],
        override_count=view["override_count"],
        **view["values"],
    )
