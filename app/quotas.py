"""Effective per-user quota: user override -> team -> müdürlük -> default group.

Teams come from the directory ``department`` attribute synced to
``User.team``, müdürlüks from ``User.organization_unit``. A group quota applies
to each member individually. A NULL team field inherits the müdürlük value and
a NULL müdürlük field inherits the default group.
"""

from __future__ import annotations

from collections import Counter
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
SOURCE_UNIT = "unit"
SOURCE_DEFAULT = "default"

# Müdürlük quotas share the user_group_quotas table with teams. The prefix
# keeps a müdürlük apart from its own team, which usually has the same name.
UNIT_KEY_PREFIX = "unit:"


def team_key(team: str | None) -> str:
    """Normalize a directory team name so spacing/case variants share a group."""
    return " ".join((team or "").split()).casefold()


def unit_key(unit: str | None) -> str:
    """Group key of a müdürlük quota; empty when the user has no müdürlük."""
    key = team_key(unit)
    return f"{UNIT_KEY_PREFIX}{key}" if key else ""


def is_unit_key(key: str) -> bool:
    return key.startswith(UNIT_KEY_PREFIX)


def clean_name(value: str | None) -> str:
    return " ".join((value or "").split())


def _most_common(values: Iterable[str]) -> str:
    counts = Counter(value for value in values if value)
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


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

    def unit_inherited(
        self, unit: str | None
    ) -> tuple[dict[str, float | int], dict[str, str]]:
        """Values (and their source) a team in ``unit`` gets without its own quota."""
        key = unit_key(unit)
        row = self.by_key.get(key) if key else None
        values: dict[str, float | int] = {}
        sources: dict[str, str] = {}
        for name in QUOTA_FIELDS:
            unit_value = getattr(row, name) if row is not None else None
            if unit_value is not None:
                values[name] = unit_value
                sources[name] = SOURCE_UNIT
            else:
                values[name] = self.default[name]
                sources[name] = SOURCE_DEFAULT
        return values, sources

    def inherited_for(
        self, team: str | None, unit: str | None = ""
    ) -> tuple[dict[str, float | int], dict[str, str]]:
        """Values (and their source) a member of ``team`` gets without overrides."""
        values, sources = self.unit_inherited(unit)
        key = team_key(team)
        group = self.by_key.get(key) if key else None
        for name in QUOTA_FIELDS:
            group_value = getattr(group, name) if group is not None else None
            if group_value is not None:
                values[name] = group_value
                sources[name] = SOURCE_GROUP
        return values, sources

    def resolve(self, user: User) -> EffectiveQuota:
        values, sources = self.inherited_for(user.team, user.organization_unit)
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


def _raw_values(row: UserGroupQuota | None) -> dict[str, float | int | None]:
    return {name: (getattr(row, name) if row is not None else None) for name in QUOTA_FIELDS}


def _empty_totals() -> dict[str, float]:
    return {"cpu": 0.0, "memory": 0.0, "gpu": 0.0, "running": 0}


def build_group_views(
    users: Iterable[User],
    groups: QuotaGroups,
    usage_by_user: dict[int, dict] | None = None,
) -> list[dict]:
    """Group users by directory team for the admin panel.

    The default group (users without a team) comes first, then teams sorted by
    name. Configured groups without members are kept so an admin can see and
    remove quotas of teams that were renamed in the directory. Each team is
    placed in the müdürlük most of its members belong to.
    """
    buckets: dict[str, dict] = {DEFAULT_GROUP_KEY: {"names": [], "members": []}}
    for key in groups.by_key:
        if not is_unit_key(key):
            buckets.setdefault(key, {"names": [], "members": []})
    for user in users:
        key = team_key(user.team)
        bucket = buckets.setdefault(key, {"names": [], "members": []})
        bucket["members"].append(user)
        bucket["names"].append(clean_name(user.team))

    views = []
    for key, bucket in buckets.items():
        is_default = key == DEFAULT_GROUP_KEY
        row = groups.default_row if is_default else groups.by_key.get(key)
        if is_default:
            display_name = DEFAULT_GROUP_LABEL
        elif row is not None and row.display_name:
            display_name = row.display_name
        else:
            display_name = _most_common(bucket["names"]) or key
        members = sorted(bucket["members"], key=lambda user: user.username.casefold())
        organization_unit = (
            "" if is_default else _most_common(clean_name(m.organization_unit) for m in members)
        )
        inherited, inherited_sources = groups.inherited_for(key, organization_unit)
        parent_inherited, parent_sources = groups.unit_inherited(organization_unit)
        totals = _empty_totals()
        for member in members:
            usage = (usage_by_user or {}).get(member.id)
            if usage:
                totals["cpu"] += usage["cpu"]["used"]
                totals["memory"] += usage["memory"]["used"]
                totals["gpu"] += usage["gpu"]["used"]
                totals["running"] += usage.get("running_workspace_count", 0)
        # Directorate/müdürlük context shown under the team name; a müdürlük's
        # own team does not repeat its name.
        units = sorted(
            {
                " · ".join(
                    part
                    for part in (
                        "" if team_key(m.organization_unit) == key else clean_name(m.organization_unit),
                        clean_name(m.directorate),
                    )
                    if part
                )
                for m in members
            }
            - {""}
        )
        views.append(
            {
                "id": row.id if row is not None else None,
                "key": key,
                "kind": "default" if is_default else "team",
                "display_name": display_name,
                "is_default": is_default,
                "configured": row is not None,
                "values": _raw_values(row),
                "inherited": inherited,
                "inherited_sources": inherited_sources,
                # What a blank team field falls back to: müdürlük, then default.
                "parent_inherited": parent_inherited,
                "parent_sources": parent_sources,
                "members": members,
                "member_count": len(members),
                "override_count": sum(1 for member in members if has_override(member)),
                "totals": totals,
                "units": units[:3],
                "organization_unit": organization_unit,
                "unit_key": unit_key(organization_unit),
                "is_unit_team": bool(organization_unit) and team_key(organization_unit) == key,
            }
        )
    default_view, team_views = views[0], views[1:]
    team_views.sort(key=lambda view: view["display_name"].casefold())
    return [default_view, *team_views]


def build_hierarchy(team_views: list[dict], groups: QuotaGroups) -> dict:
    """Nest team views (from ``build_group_views``) under their müdürlük.

    Returns the default group, the müdürlüks sorted by name (each with its own
    team first, then its other teams) and the teams without a müdürlük.
    Configured müdürlük quotas without teams are kept so they can be removed.
    """
    default_view, teams = team_views[0], team_views[1:]
    buckets: dict[str, dict] = {
        key: {"names": [], "teams": []} for key in groups.by_key if is_unit_key(key)
    }
    standalone = []
    for view in teams:
        if not view["unit_key"]:
            standalone.append(view)
            continue
        bucket = buckets.setdefault(view["unit_key"], {"names": [], "teams": []})
        bucket["teams"].append(view)
        bucket["names"].append(view["organization_unit"])

    units = []
    for key, bucket in buckets.items():
        row = groups.by_key.get(key)
        display_name = (
            (row.display_name if row is not None else "")
            or _most_common(bucket["names"])
            or key.removeprefix(UNIT_KEY_PREFIX)
        )
        unit_teams = sorted(
            bucket["teams"],
            key=lambda view: (not view["is_unit_team"], view["display_name"].casefold()),
        )
        members = [member for view in unit_teams for member in view["members"]]
        inherited, inherited_sources = groups.unit_inherited(display_name)
        totals = _empty_totals()
        for view in unit_teams:
            for name in totals:
                totals[name] += view["totals"][name]
        units.append(
            {
                "id": row.id if row is not None else None,
                "key": key,
                "kind": "unit",
                "display_name": display_name,
                "is_default": False,
                "configured": row is not None,
                "values": _raw_values(row),
                "inherited": inherited,
                "inherited_sources": inherited_sources,
                "teams": unit_teams,
                "team_count": len(unit_teams),
                "members": members,
                "member_count": len(members),
                "override_count": sum(view["override_count"] for view in unit_teams),
                "configured_team_count": sum(1 for view in unit_teams if view["configured"]),
                "totals": totals,
                "directorates": sorted({clean_name(m.directorate) for m in members} - {""}),
                "organization_unit": display_name,
            }
        )
    units.sort(key=lambda unit: unit["display_name"].casefold())
    return {"default": default_view, "units": units, "standalone": standalone}
