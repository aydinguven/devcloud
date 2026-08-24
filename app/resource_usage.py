"""Host and per-user resource accounting for dashboard and quota checks."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.config import settings
from app.models.user import User
from app.models.workspace import Workspace
from app.orchestrator.flavors import Flavor, get_flavor

BYTES_PER_MB = 1024 * 1024
BYTES_PER_GB = 1024 * BYTES_PER_MB
_previous_cpu_sample: tuple[int, int] | None = None


def format_bytes(value: float) -> str:
    """Format bytes for compact dashboard labels."""
    value = max(float(value), 0.0)
    if value >= BYTES_PER_GB:
        return f"{value / BYTES_PER_GB:.1f} GB"
    return f"{value / BYTES_PER_MB:.1f} MB"


def format_cpu(value: float) -> str:
    return f"{max(float(value), 0.0):.1f} cores"


def _metric(used: float, limit: float, formatter) -> dict[str, Any]:
    used = max(float(used), 0.0)
    limit = max(float(limit), 0.0)
    remaining = max(limit - used, 0.0)
    percent = min((used / limit * 100.0) if limit else (100.0 if used else 0.0), 100.0)
    return {
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "percent": round(percent, 1),
        "used_display": formatter(used),
        "limit_display": formatter(limit),
        "remaining_display": formatter(remaining),
    }


def _read_cpu_sample() -> tuple[int, int] | None:
    try:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        values = [int(value) for value in fields[1:]]
    except (OSError, ValueError, IndexError):
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _cpu_percent(cpu_count: int) -> float:
    global _previous_cpu_sample
    current = _read_cpu_sample()
    previous = _previous_cpu_sample
    _previous_cpu_sample = current
    if current and previous:
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta > 0:
            return max(0.0, min((1.0 - idle_delta / total_delta) * 100.0, 100.0))
    try:
        load_1m = os.getloadavg()[0]
        return max(0.0, min(load_1m / max(cpu_count, 1) * 100.0, 100.0))
    except (AttributeError, OSError):
        return 0.0


def _memory_usage() -> tuple[int, int]:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw_value = line.split(":", 1)
            values[key] = int(raw_value.strip().split()[0]) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0))
        return max(total - available, 0), total
    except (OSError, ValueError, KeyError):
        return 0, 0


def get_system_usage() -> dict[str, dict[str, Any]]:
    """Return host CPU, RAM, and workspace-filesystem utilization."""
    cpu_count = max(os.cpu_count() or 1, 1)
    cpu_percent = _cpu_percent(cpu_count)
    memory_used, memory_total = _memory_usage()
    try:
        disk = shutil.disk_usage(settings.STORAGE_ROOT)
        disk_used, disk_total = disk.used, disk.total
    except OSError:
        disk_used, disk_total = 0, 0
    return {
        "cpu": _metric(cpu_count * cpu_percent / 100.0, cpu_count, format_cpu),
        "memory": _metric(memory_used, memory_total, format_bytes),
        "disk": _metric(disk_used, disk_total, format_bytes),
    }


def directory_size(path: Path) -> int:
    """Return regular-file bytes below path without following symlinks."""
    total = 0
    if not path.exists():
        return total
    for root, _, filenames in os.walk(path, followlinks=False):
        for filename in filenames:
            try:
                total += (Path(root) / filename).stat(follow_symlinks=False).st_size
            except (OSError, FileNotFoundError):
                continue
    return total


def workspace_allocations(workspaces: Iterable[Workspace]) -> tuple[float, int, int]:
    """Return reserved CPU, RAM MB, and recognized workspace count."""
    cpu_used = 0.0
    memory_mb_used = 0
    count = 0
    for workspace in workspaces:
        flavor = get_flavor(workspace.flavor_id)
        if not flavor:
            continue
        cpu_used += flavor.cpus
        memory_mb_used += flavor.memory_mb
        count += 1
    return cpu_used, memory_mb_used, count


def get_user_usage(user: User, workspaces: Iterable[Workspace]) -> dict[str, Any]:
    """Return a user's allocations, actual disk use, and remaining quota."""
    cpu_used, memory_mb_used, workspace_count = workspace_allocations(workspaces)
    disk_used = directory_size(Path(settings.STORAGE_ROOT) / str(user.id))
    return {
        "cpu": _metric(cpu_used, user.cpu_quota, format_cpu),
        "memory": _metric(
            memory_mb_used * BYTES_PER_MB,
            user.memory_mb_quota * BYTES_PER_MB,
            format_bytes,
        ),
        "disk": _metric(
            disk_used,
            user.disk_mb_quota * BYTES_PER_MB,
            format_bytes,
        ),
        "workspace_count": workspace_count,
    }


def get_all_user_usage(
    users: Iterable[User],
    workspaces: Iterable[Workspace],
) -> dict[int, dict[str, Any]]:
    """Build per-user summaries while grouping workspace allocations once."""
    grouped: dict[int, list[Workspace]] = {}
    for workspace in workspaces:
        grouped.setdefault(workspace.user_id, []).append(workspace)
    return {
        user.id: get_user_usage(user, grouped.get(user.id, []))
        for user in users
    }


def quota_violations(
    user: User,
    workspaces: Iterable[Workspace],
    requested_flavor: Flavor,
) -> list[str]:
    """Describe quota limits a new workspace allocation would exceed."""
    usage = get_user_usage(user, workspaces)
    violations = []
    requested_cpu = usage["cpu"]["used"] + requested_flavor.cpus
    requested_memory_mb = usage["memory"]["used"] / BYTES_PER_MB + requested_flavor.memory_mb
    if requested_cpu > user.cpu_quota:
        violations.append(
            f"CPU {requested_cpu:.1f}/{user.cpu_quota:.1f} olacak"
        )
    if requested_memory_mb > user.memory_mb_quota:
        violations.append(
            f"RAM {requested_memory_mb:.0f}/{user.memory_mb_quota} MB olacak"
        )
    if usage["disk"]["used"] >= usage["disk"]["limit"]:
        violations.append(
            f"Disk kullanımı {usage['disk']['used_display']}/{usage['disk']['limit_display']}"
        )
    return violations
