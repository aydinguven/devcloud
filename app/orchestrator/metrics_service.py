import os
from pathlib import Path
from datetime import datetime, timezone
from app.orchestrator.runtime_backend import runtime_for_node
from app.models.workspace import Workspace, WorkspaceStatus
from app.time_utils import ensure_utc


def get_dir_size_bytes(directory_path: str | Path) -> int:
    """Calculate the total disk space consumed by a directory in bytes."""
    total_size = 0
    path = Path(directory_path)
    if not path.exists() or not path.is_dir():
        return 0

    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total_size += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total_size += get_dir_size_bytes(entry.path)
    except (PermissionError, OSError):
        pass
    return total_size


def format_bytes_human(num_bytes: int) -> str:
    """Convert bytes into human readable KB, MB, GB."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


async def get_workspace_live_metrics(workspace: Workspace) -> dict:
    """Gather live CPU %, memory, disk usage, and uptime for a single workspace."""
    disk_bytes = 0
    runtime = runtime_for_node(workspace.node_id)
    if workspace.storage_path:
        disk_bytes = await runtime.get_storage_size(
            workspace.container_name, workspace.storage_path
        )

    uptime_seconds = 0
    uptime_display = "Stopped"
    
    if workspace.status == WorkspaceStatus.RUNNING and workspace.last_started_at:
        now = datetime.now(timezone.utc)
        uptime_seconds = max(int((now - ensure_utc(workspace.last_started_at)).total_seconds()), 0)
        if uptime_seconds < 60:
            uptime_display = f"{uptime_seconds}s"
        elif uptime_seconds < 3600:
            uptime_display = f"{uptime_seconds // 60}m {uptime_seconds % 60}s"
        else:
            hours = uptime_seconds // 3600
            mins = (uptime_seconds % 3600) // 60
            uptime_display = f"{hours}h {mins}m"

    stats = {
        "cpu_percent": 0.0,
        "mem_usage_display": "0 MB",
        "mem_percent": 0.0,
        "net_io": "--",
        "block_io": "--",
        "pids": 0,
    }

    if workspace.status == WorkspaceStatus.RUNNING and workspace.container_name:
        stats = await runtime.get_container_stats(workspace.container_name)

    return {
        "workspace_id": workspace.id,
        "status": workspace.status.value if hasattr(workspace.status, "value") else str(workspace.status),
        "cpu_percent": stats.get("cpu_percent", 0.0),
        "mem_usage_display": stats.get("mem_usage_display", "--"),
        "mem_percent": stats.get("mem_percent", 0.0),
        "net_io": stats.get("net_io", "--"),
        "block_io": stats.get("block_io", "--"),
        "disk_usage_bytes": disk_bytes,
        "disk_usage_display": format_bytes_human(disk_bytes),
        "uptime_seconds": uptime_seconds,
        "uptime_display": uptime_display,
    }
