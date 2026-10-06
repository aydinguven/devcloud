"""LLM usage statistics from LiteLLM for the admin panel and the GenAI stats page.

Two admin calls cover everything: ``/user/daily/activity`` without a user
filter (daily totals, per-user and per-model breakdown) and
``/team/daily/activity`` (per-team breakdown with aliases). Both paginate over
raw spend rows, so pages are merged by date. Results are cached briefly so
page loads do not hammer LiteLLM.
"""

import asyncio
import time
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.genai import GenAiUnavailable, _client, litellm_user_id
from app.integrations.litellm import LiteLLMClient, LiteLLMConnectionError
from app.models.genai_account import GenAiAccount
from app.models.user import User
from app.quotas import DEFAULT_GROUP_KEY, team_key

METRICS = (
    "spend",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "api_requests",
    "successful_requests",
    "failed_requests",
)
CACHE_SECONDS = 60
MAX_PAGES = 50

_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = asyncio.Lock()


def _empty() -> dict:
    return {name: 0 for name in METRICS} | {"spend": 0.0}


def _add(target: dict, metrics: dict | None) -> None:
    for name in METRICS:
        value = (metrics or {}).get(name) or 0
        try:
            target[name] += float(value) if name == "spend" else int(value)
        except (TypeError, ValueError):
            continue


def _rounded(metrics: dict) -> dict:
    return metrics | {"spend": round(float(metrics.get("spend") or 0), 6)}


async def _activity(client: LiteLLMClient, path: str, start: date, end: date) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        payload = await client._request(
            "GET",
            path,
            params={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "page": page,
                "page_size": 1000,
            },
        )
        rows.extend(item for item in payload.get("results") or [] if isinstance(item, dict))
        if not (payload.get("metadata") or {}).get("has_more"):
            break
    return rows


def _merge(rows: list[dict]) -> dict:
    daily: dict[str, dict] = {}
    entities: dict[str, dict] = {}
    entity_daily: dict[str, dict[str, dict]] = {}
    entity_meta: dict[str, dict] = {}
    models: dict[str, dict] = {}
    for row in rows:
        day = str(row.get("date") or "")[:10]
        if not day:
            continue
        _add(daily.setdefault(day, _empty()), row.get("metrics"))
        breakdown = row.get("breakdown") or {}
        for entity, item in (breakdown.get("entities") or {}).items():
            if not isinstance(item, dict):
                continue
            _add(entities.setdefault(entity, _empty()), item.get("metrics"))
            _add(entity_daily.setdefault(entity, {}).setdefault(day, _empty()), item.get("metrics"))
            if isinstance(item.get("metadata"), dict) and item["metadata"]:
                entity_meta[entity] = item["metadata"]
        for model, item in (breakdown.get("models") or {}).items():
            if isinstance(item, dict):
                _add(models.setdefault(model, _empty()), item.get("metrics"))
    return {
        "daily": daily,
        "entities": entities,
        "entity_daily": entity_daily,
        "entity_meta": entity_meta,
        "models": models,
    }


async def _fetch(client: LiteLLMClient, start: date, end: date) -> dict:
    users = _merge(await _activity(client, "/user/daily/activity", start, end))
    try:
        teams = _merge(await _activity(client, "/team/daily/activity", start, end))
    except LiteLLMConnectionError:
        # Older LiteLLM releases have no team analytics; users still work.
        teams = None
    return {"users": users, "teams": teams}


async def usage_stats(db: AsyncSession, days: int = 30) -> dict:
    """Aggregated usage for the last ``days`` days, mapped to devcloud users."""
    days = max(1, min(int(days), 90))
    end = date.today()
    start = end - timedelta(days=days - 1)
    try:
        client, _record = await _client(db)
    except GenAiUnavailable as exc:
        return {"available": False, "error": str(exc), "days": days}
    key = (client.config.base_url, start, end)
    async with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            raw = cached[1]
        else:
            try:
                raw = await _fetch(client, start, end)
            except LiteLLMConnectionError as exc:
                return {"available": False, "error": str(exc), "days": days}
            _cache.clear()
            _cache[key] = (time.monotonic(), raw)
    return await _shape(db, raw, start, end, days)


async def _directory(db: AsyncSession) -> dict[str, dict]:
    """LiteLLM user id -> devcloud identity."""
    accounts = {
        row.user_id: row.litellm_user_id
        for row in (await db.execute(select(GenAiAccount))).scalars()
    }
    people: dict[str, dict] = {}
    for user in (await db.execute(select(User))).scalars():
        try:
            litellm_id = accounts.get(user.id) or litellm_user_id(user)
        except Exception:  # noqa: BLE001 - usernames LiteLLM cannot represent
            continue
        people[litellm_id] = {
            "user_id": user.id,
            "username": user.username,
            "full_name": user.full_name or "",
            "team": " ".join((user.team or "").split()),
            "team_key": team_key(user.team),
        }
    return people


async def _shape(db: AsyncSession, raw: dict, start: date, end: date, days: int) -> dict:
    users_raw = raw["users"]
    teams_raw = raw["teams"]
    people = await _directory(db)
    dates = [(start + timedelta(days=offset)).isoformat() for offset in range(days)]

    totals = _empty()
    for metrics in users_raw["daily"].values():
        _add(totals, metrics)

    users = []
    groups: dict[str, dict] = {}
    for entity, metrics in users_raw["entities"].items():
        person = people.get(entity)
        per_day = users_raw["entity_daily"].get(entity, {})
        series = {
            "daily_tokens": [per_day.get(day, {}).get("total_tokens", 0) for day in dates],
            "daily_requests": [per_day.get(day, {}).get("api_requests", 0) for day in dates],
        }
        users.append(
            {
                "litellm_user_id": entity,
                "username": person["username"] if person else entity,
                "full_name": person["full_name"] if person else "",
                "team": person["team"] if person else "",
                "devcloud_user_id": person["user_id"] if person else None,
                **_rounded(metrics),
                **series,
            }
        )
        group_key = person["team_key"] if person else DEFAULT_GROUP_KEY
        group = groups.setdefault(
            group_key,
            {
                "key": group_key,
                "name": (person["team"] if person else "") or "Takımsız",
                "members": 0,
                **_empty(),
                "daily_tokens": [0] * len(dates),
                "daily_requests": [0] * len(dates),
            },
        )
        group["members"] += 1
        _add(group, metrics)
        for index in range(len(dates)):
            group["daily_tokens"][index] += series["daily_tokens"][index]
            group["daily_requests"][index] += series["daily_requests"][index]
    users.sort(key=lambda item: item["total_tokens"], reverse=True)

    teams = []
    team_series = []
    if teams_raw is not None:
        for entity, metrics in teams_raw["entities"].items():
            alias = str((teams_raw["entity_meta"].get(entity) or {}).get("team_alias") or "")
            name = alias or ("Takımsız anahtarlar" if entity == "Unassigned" else entity)
            teams.append({"team_id": entity, "name": name, **_rounded(metrics)})
            per_day = teams_raw["entity_daily"].get(entity, {})
            team_series.append(
                {
                    "name": name,
                    "values": [_rounded(per_day.get(day, _empty())) for day in dates],
                }
            )
        teams.sort(key=lambda item: item["total_tokens"], reverse=True)
        order = [team["name"] for team in teams]
        team_series.sort(key=lambda series: order.index(series["name"]))

    models = [
        {"model": model, **_rounded(metrics)}
        for model, metrics in users_raw["models"].items()
    ]
    models.sort(key=lambda item: item["total_tokens"], reverse=True)

    return {
        "available": True,
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "totals": _rounded(totals) | {"active_users": len(users)},
        "daily": [
            {"date": day, **_rounded(users_raw["daily"].get(day, _empty()))} for day in dates
        ],
        "users": users,
        "user_breakdown": bool(users) or not totals["api_requests"],
        "groups": sorted(
            (_rounded(group) for group in groups.values()),
            key=lambda item: item["total_tokens"],
            reverse=True,
        ),
        "teams": teams,
        "team_breakdown": teams_raw is not None,
        "team_series": team_series,
        "models": models,
    }
