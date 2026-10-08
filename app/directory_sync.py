"""Bulk Active Directory sync: teams, müdürlüks and the users' profiles.

Login only refreshes the user who logs in. The sync reads every directory
person in one paged search with the service account, places each AD
``department`` (team) under its müdürlük and refreshes the organization fields
of the existing DevCloud directory users. Nobody is created or deactivated.

A team's müdürlük is the majority of its members' manager chains, so a member
whose own chain is broken still gets the team's müdürlük. A team whose chain
reaches the Genel Müdür first belongs directly to the Genel Müdürlük.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ldap import (
    CHAIN_DIVISION_HEAD,
    CHAIN_UNIT,
    DirectoryConfig,
    DirectoryConnectionError,
    DirectoryPerson,
    DirectoryUnavailableError,
    _bound_connection,
    _ldap3,
    _safe_unbind,
    fold_directory_text,
    parse_unit_head_titles,
    validate_directory_config,
    walk_manager_chain,
)
from app.models.directory_settings import DirectorySettings
from app.models.directory_team import (
    TEAM_STATUS_DIVISION,
    TEAM_STATUS_TEAM,
    TEAM_STATUS_UNASSIGNED,
    TEAM_STATUS_UNIT,
    DirectoryTeam,
)
from app.models.user import User
from app.quotas import clean_name, team_key

logger = logging.getLogger("devcloud.directory_sync")

PAGE_SIZE = 500
DIRECTORY_AUTH_SOURCE = "active_directory"
SYNCED_USER_FIELDS = ("team", "directorate", "organization_unit", "managed_unit", "full_name")


@dataclass(frozen=True)
class DirectoryEntry:
    """One directory person as read by the bulk search."""

    dn: str
    username: str
    full_name: str
    team: str
    title: str
    directorate: str
    manager_dn: str
    # Value of the configured müdürlük attribute, if any.
    organization_unit: str = ""

    def as_person(self) -> DirectoryPerson:
        return DirectoryPerson(
            dn=self.dn,
            team=self.team,
            title=self.title,
            directorate=self.directorate,
            manager_dn=self.manager_dn,
        )


@dataclass(frozen=True)
class PersonPlacement:
    team: str
    directorate: str
    organization_unit: str
    managed_unit: str
    full_name: str


@dataclass
class TeamPlacement:
    key: str
    name: str
    directorate: str = ""
    organization_unit: str = ""
    status: str = TEAM_STATUS_UNASSIGNED
    reason: str = ""
    member_count: int = 0
    head_username: str = ""
    head_name: str = ""


@dataclass
class OrgSnapshot:
    people: dict[str, PersonPlacement] = field(default_factory=dict)  # username key
    teams: dict[str, TeamPlacement] = field(default_factory=dict)  # team key


def _top(counter: Counter) -> str:
    return max(counter.items(), key=lambda item: (item[1], item[0]))[0] if counter else ""


def username_key(username: str | None) -> str:
    return (username or "").strip().casefold()


def build_org_snapshot(
    entries: list[DirectoryEntry],
    unit_head_titles: str,
    division_head_titles: str,
    use_unit_attribute: bool = False,
) -> OrgSnapshot:
    """Place every directory person and team (pure, no I/O)."""
    head_titles = parse_unit_head_titles(unit_head_titles)
    division_titles = parse_unit_head_titles(division_head_titles)
    by_dn = {entry.dn.strip().casefold(): entry for entry in entries}
    people = {key: entry.as_person() for key, entry in by_dn.items()}

    def lookup(dn: str) -> DirectoryPerson | None:
        return people.get(dn.strip().casefold())

    own_unit: dict[str, str] = {}  # dn key -> unit from the person's own chain
    team_names: dict[str, Counter] = defaultdict(Counter)
    team_directorates: dict[str, Counter] = defaultdict(Counter)
    team_units: dict[str, Counter] = defaultdict(Counter)
    team_reasons: dict[str, Counter] = defaultdict(Counter)
    unit_heads: dict[str, Counter] = defaultdict(Counter)  # unit key -> head dn key
    division_heads: dict[str, Counter] = defaultdict(Counter)  # team key -> head dn key
    member_counts: Counter = Counter()

    for dn_key, entry in by_dn.items():
        tkey = team_key(entry.team)
        chain = walk_manager_chain(people[dn_key], lookup, head_titles, division_titles)
        if use_unit_attribute:
            # The configured müdürlük attribute wins; the chain still finds heads.
            unit = clean_name(entry.organization_unit)
            reason = CHAIN_UNIT if unit else chain.reason
        else:
            unit, reason = clean_name(chain.unit), chain.reason
        own_unit[dn_key] = unit
        if chain.head is not None and unit and team_key(chain.unit) == team_key(unit):
            unit_heads[team_key(unit)][chain.head.dn.strip().casefold()] += 1
        if not tkey:
            continue
        member_counts[tkey] += 1
        team_names[tkey][clean_name(entry.team)] += 1
        if entry.directorate:
            team_directorates[tkey][clean_name(entry.directorate)] += 1
        if unit:
            team_units[tkey][unit] += 1
        else:
            team_reasons[tkey][reason] += 1
            if reason == CHAIN_DIVISION_HEAD and chain.head is not None:
                division_heads[tkey][chain.head.dn.strip().casefold()] += 1

    snapshot = OrgSnapshot()
    for tkey, count in member_counts.items():
        team = TeamPlacement(
            key=tkey,
            name=_top(team_names[tkey]),
            directorate=_top(team_directorates[tkey]),
            member_count=count,
        )
        head_dn = ""
        if team_units[tkey]:
            team.organization_unit = _top(team_units[tkey])
            team.status = (
                TEAM_STATUS_UNIT if team_key(team.organization_unit) == tkey else TEAM_STATUS_TEAM
            )
            head_dn = _top(unit_heads[team_key(team.organization_unit)])
        elif team_reasons[tkey][CHAIN_DIVISION_HEAD]:
            team.status = TEAM_STATUS_DIVISION
            team.reason = CHAIN_DIVISION_HEAD
            head_dn = _top(division_heads[tkey])
        else:
            team.reason = _top(team_reasons[tkey])
        head = by_dn.get(head_dn)
        if head is not None:
            team.head_username = head.username
            team.head_name = head.full_name or head.username
        snapshot.teams[tkey] = team

    for dn_key, entry in by_dn.items():
        key = username_key(entry.username)
        if not key:
            continue
        team = snapshot.teams.get(team_key(entry.team))
        # A broken personal chain falls back to the team's müdürlük.
        unit = own_unit[dn_key] or (team.organization_unit if team else "")
        is_head = fold_directory_text(entry.title) in head_titles
        snapshot.people[key] = PersonPlacement(
            team=clean_name(entry.team),
            directorate=clean_name(entry.directorate) or (team.directorate if team else ""),
            organization_unit=unit,
            managed_unit=unit if is_head and unit else "",
            full_name=entry.full_name,
        )
    return snapshot


def _sync_filter(config: DirectoryConfig) -> str:
    """The login filter with every username, e.g. (sAMAccountName=*)."""
    return config.user_filter.replace("{username}", "*")


def fetch_directory_entries(config: DirectoryConfig) -> list[DirectoryEntry]:
    """Read every directory person under the user base DN (blocking)."""
    validate_directory_config(config)
    ldap3, LDAPException, _ = _ldap3()
    try:
        connection = _bound_connection(config, config.bind_dn, config.bind_password)
    except DirectoryConnectionError as exc:
        raise DirectoryUnavailableError(str(exc)) from exc
    attributes = [
        attribute
        for attribute in dict.fromkeys(
            [
                config.username_attribute,
                config.display_name_attribute,
                config.team_attribute,
                config.directorate_attribute,
                config.title_attribute,
                config.manager_attribute,
                config.organization_unit_attribute,
            ]
        )
        if attribute
    ]

    def value(attributes_map: dict, name: str) -> str:
        if not name:
            return ""
        raw = attributes_map.get(name)
        if isinstance(raw, list):
            raw = raw[0] if raw else ""
        return str(raw or "").strip()

    entries: list[DirectoryEntry] = []
    try:
        for item in connection.extend.standard.paged_search(
            search_base=config.user_base_dn,
            search_filter=_sync_filter(config),
            search_scope=ldap3.SUBTREE,
            attributes=attributes,
            paged_size=PAGE_SIZE,
            generator=True,
        ):
            if item.get("type") != "searchResEntry":
                continue
            found = item.get("attributes") or {}
            username = value(found, config.username_attribute)
            if not username:
                continue
            entries.append(
                DirectoryEntry(
                    dn=str(item.get("dn") or ""),
                    username=username,
                    full_name=value(found, config.display_name_attribute),
                    team=value(found, config.team_attribute),
                    title=value(found, config.title_attribute),
                    directorate=value(found, config.directorate_attribute),
                    manager_dn=value(found, config.manager_attribute),
                    organization_unit=value(found, config.organization_unit_attribute),
                )
            )
    except (LDAPException, OSError) as exc:
        logger.error("Bulk directory search failed: %s", exc)
        raise DirectoryUnavailableError(str(exc)) from exc
    finally:
        _safe_unbind(connection)
    return entries


def _clip(value: str, column: str) -> str:
    return (value or "")[: User.__table__.columns[column].type.length]


async def apply_snapshot(db: AsyncSession, snapshot: OrgSnapshot, ad_people: int) -> dict:
    """Store the team placements and refresh existing directory users."""
    now = datetime.now(timezone.utc)
    await db.execute(delete(DirectoryTeam))
    for team in snapshot.teams.values():
        db.add(
            DirectoryTeam(
                team_key=team.key[:255],
                name=team.name[:255],
                directorate=team.directorate[:255],
                organization_unit=team.organization_unit[:255],
                status=team.status,
                reason=team.reason,
                member_count=team.member_count,
                head_username=team.head_username[:64],
                head_name=team.head_name[:128],
                synced_at=now,
            )
        )

    updated: list[str] = []
    missing: list[str] = []
    users = (
        await db.execute(select(User).where(User.auth_source == DIRECTORY_AUTH_SOURCE))
    ).scalars().all()
    for user in users:
        placement = snapshot.people.get(username_key(user.username))
        if placement is None:
            missing.append(user.username)
            continue
        changed = False
        for name in SYNCED_USER_FIELDS:
            new = _clip(getattr(placement, name), name)
            if name == "full_name" and not new:
                continue
            if getattr(user, name) != new:
                setattr(user, name, new)
                changed = True
        if changed:
            updated.append(user.username)

    teams = list(snapshot.teams.values())
    unassigned = sorted(
        (team for team in teams if team.status == TEAM_STATUS_UNASSIGNED),
        key=lambda team: (-team.member_count, team.name),
    )
    summary = {
        "synced_at": now.isoformat(),
        "ad_people": ad_people,
        "teams": len(teams),
        "units": len({team_key(team.organization_unit) for team in teams if team.organization_unit}),
        "division_teams": sum(1 for team in teams if team.status == TEAM_STATUS_DIVISION),
        "unassigned_teams": [
            {"name": team.name, "reason": team.reason, "members": team.member_count}
            for team in unassigned[:50]
        ],
        "unassigned_count": len(unassigned),
        "devcloud_users": len(users),
        "updated_users": len(updated),
        "missing_users": sorted(missing)[:50],
        "missing_count": len(missing),
    }
    record = await db.get(DirectorySettings, 1)
    if record is not None:
        record.last_sync_at = now
        record.last_sync_summary = json.dumps(summary, ensure_ascii=False)
        db.add(record)
    await db.commit()
    return summary


def parse_summary(record: DirectorySettings | None) -> dict | None:
    if record is None or not record.last_sync_summary:
        return None
    try:
        return json.loads(record.last_sync_summary)
    except ValueError:
        return None


async def directory_teams_by_key(db: AsyncSession) -> dict[str, DirectoryTeam]:
    return {row.team_key: row for row in (await db.execute(select(DirectoryTeam))).scalars()}
