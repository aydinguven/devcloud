"""Effective per-user quota: user override -> team group -> default group.

Teams come from the directory ``department`` attribute synced to
``User.team``. A group quota applies to each member individually.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import User
from app.models.user_group_quota import DEFAULT_GROUP_KEY, UserGroupQuota

QUOTA_FIELDS = ("cpu_quota", "memory_mb_quota", "disk_mb_quota", "gpu_quota")
SOURCE_USER = "user"
SOURCE_GROUP = "group"
SOURCE_DEFAULT = "default"


def team_key(team: str | None) -> str:
    """Normalize a directory team name so spacing/case variants share a group."""
    return " ".join((team or "").split()).casefold()


def settings_default_quota() -> dict[str, float | int]:
    return {
        "cpu_quota": float(settings.DEFAULT_USER_CPU_QUOTA),
        "memory_mb_quota": int(settings.DEFAULT_USER_MEMORY_MB_QUOTA),
        "disk_mb_quota": int(settings.DEFAULT_USER_DISK_MB_QUOTA),
        "gpu_quota": int(settings.DEFAULT_USER_GPU_QUOTA),
    }


@dataclass(frozen=True)
class EffectiveQuota:
    cpu_quota: float
    memory_mb_quota: int
    disk_mb_quota: int
    gpu_quota: int
    group_key: str = DEFAULT_GROUP_KEY
    sources: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, float | int]:
        return {name: getattr(self, name) for name in QUOTA_FIELDS}


@dataclass(frozen=True)
class QuotaGroups:
    default: dict[str, float | int]
    by_key: dict[str, UserGroupQuota]
    default_row: UserGroupQuota | None = None

    def inherited_for(self, team: str | None) -> tuple[dict[str, float | int], dict[str, str]]:
        """Values (and their source) a member of ``team`` gets without overrides."""
        group = self.by_key.get(team_key(team)) if team_key(team) else None
        values: dict[str, float | int] = {}
        sources: dict[str, str] = {}
        for name in QUOTA_FIELDS:
            group_value = getattr(group, name) if group is not None else None
            if group_value is not None:
                values[name] = group_value
                sources[name] = SOURCE_GROUP
            else:
                values[name] = self.default[name]
                sources[name] = SOURCE_DEFAULT
        return values, sources

    def resolve(self, user: User) -> EffectiveQuota:
        values, sources = self.inherited_for(user.team)
        for name in QUOTA_FIELDS:
            override = getattr(user, f"{name}_override", None)
            if override is not None:
                values[name] = override
                sources[name] = SOURCE_USER
        return EffectiveQuota(
            cpu_quota=float(values["cpu_quota"]),
            memory_mb_quota=int(values["memory_mb_quota"]),
            disk_mb_quota=int(values["disk_mb_quota"]),
            gpu_quota=int(values["gpu_quota"]),
            group_key=team_key(user.team),
            sources=sources,
        )


async def load_quota_groups(db: AsyncSession) -> QuotaGroups:
    rows = (await db.execute(select(UserGroupQuota))).scalars().all()
    by_key = {row.group_key: row for row in rows}
    default = settings_default_quota()
    default_row = by_key.pop(DEFAULT_GROUP_KEY, None)
    if default_row is not None:
        for name in QUOTA_FIELDS:
            value = getattr(default_row, name)
            if value is not None:
                default[name] = value
    return QuotaGroups(default=default, by_key=by_key, default_row=default_row)


async def effective_quota(db: AsyncSession, user: User) -> EffectiveQuota:
    return (await load_quota_groups(db)).resolve(user)


async def effective_quotas(
    db: AsyncSession, users: Iterable[User]
) -> dict[int, EffectiveQuota]:
    groups = await load_quota_groups(db)
    return {user.id: groups.resolve(user) for user in users}


def user_out(user: User, quota: EffectiveQuota):
    """Serialize a user with the effective (not the legacy column) quota."""
    from app.schemas.user import UserOut

    return UserOut.model_validate(user).model_copy(
        update={**quota.as_dict(), "quota_sources": dict(quota.sources)}
    )


async def user_out_for(db: AsyncSession, user: User):
    return user_out(user, await effective_quota(db, user))


DEFAULT_GROUP_LABEL = "Varsayılan"


def has_override(user: User) -> bool:
    return any(getattr(user, f"{name}_override", None) is not None for name in QUOTA_FIELDS)


def build_group_views(
    users: Iterable[User],
    groups: QuotaGroups,
    usage_by_user: dict[int, dict] | None = None,
) -> list[dict]:
    """Group users by directory team for the admin panel.

    The default group (users without a team) comes first, then teams sorted by
    name. Configured groups without members are kept so an admin can see and
    remove quotas of teams that were renamed in the directory.
    """
    buckets: dict[str, dict] = {DEFAULT_GROUP_KEY: {"names": {}, "members": []}}
    for key in groups.by_key:
        buckets.setdefault(key, {"names": {}, "members": []})
    for user in users:
        key = team_key(user.team)
        bucket = buckets.setdefault(key, {"names": {}, "members": []})
        bucket["members"].append(user)
        raw = " ".join((user.team or "").split())
        if raw:
            bucket["names"][raw] = bucket["names"].get(raw, 0) + 1

    views = []
    for key, bucket in buckets.items():
        is_default = key == DEFAULT_GROUP_KEY
        row = groups.default_row if is_default else groups.by_key.get(key)
        if is_default:
            display_name = DEFAULT_GROUP_LABEL
        elif row is not None and row.display_name:
            display_name = row.display_name
        elif bucket["names"]:
            display_name = max(bucket["names"].items(), key=lambda item: (item[1], item[0]))[0]
        else:
            display_name = key
        inherited, inherited_sources = groups.inherited_for(key)
        members = sorted(bucket["members"], key=lambda user: user.username.casefold())
        totals = {"cpu": 0.0, "memory": 0.0, "gpu": 0.0, "running": 0}
        for member in members:
            usage = (usage_by_user or {}).get(member.id)
            if usage:
                totals["cpu"] += usage["cpu"]["used"]
                totals["memory"] += usage["memory"]["used"]
                totals["gpu"] += usage["gpu"]["used"]
                totals["running"] += usage.get("running_workspace_count", 0)
        # Directorate/müdürlük context shown under the team name.
        units = sorted(
            {
                " · ".join(part for part in (m.organization_unit, m.directorate) if part)
                for m in members
                if (m.organization_unit or m.directorate)
            }
        )
        views.append(
            {
                "id": row.id if row is not None else None,
                "key": key,
                "display_name": display_name,
                "is_default": is_default,
                "configured": row is not None,
                "values": {
                    name: (getattr(row, name) if row is not None else None)
                    for name in QUOTA_FIELDS
                },
                "inherited": inherited,
                "inherited_sources": inherited_sources,
                "members": members,
                "member_count": len(members),
                "override_count": sum(1 for member in members if has_override(member)),
                "totals": totals,
                "units": units[:3],
            }
        )
    default_view, team_views = views[0], views[1:]
    team_views.sort(key=lambda view: view["display_name"].casefold())
    return [default_view, *team_views]
