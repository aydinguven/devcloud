import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.runtime_backend import runtime_for_node
from app.time_utils import ensure_utc

logger = logging.getLogger("devcloud.reaper")


async def run_idle_reaper_cycle() -> int:
    """Check running workspaces and auto-stop any that have exceeded their auto-stop timeout."""
    stopped_count = 0
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        stmt = select(Workspace).where(
            Workspace.status == WorkspaceStatus.RUNNING,
            Workspace.auto_stop_minutes > 0,
        )
        result = await db.execute(stmt)
        workspaces = result.scalars().all()

        for ws in workspaces:
            if not ws.last_started_at:
                continue

            elapsed_minutes = (now - ensure_utc(ws.last_started_at)).total_seconds() / 60.0
            if elapsed_minutes >= ws.auto_stop_minutes:
                logger.info(
                    f"Auto-stopping idle workspace [{ws.name}] (ID: {ws.id}) "
                    f"after {elapsed_minutes:.1f}m of activity (limit: {ws.auto_stop_minutes}m)."
                )
                try:
                    await runtime_for_node(ws.node_id).stop_container(ws.container_name)
                    ws.status = WorkspaceStatus.STOPPED
                    ws.last_stopped_at = now
                    db.add(ws)
                    stopped_count += 1
                except Exception as exc:
                    logger.error(f"Error auto-stopping workspace {ws.id}: {exc}")

        if stopped_count > 0:
            await db.commit()

    return stopped_count


async def idle_reaper_background_worker(check_interval_seconds: int = 60) -> None:
    """Continuous background worker loop for the idle reaper."""
    logger.info("DevCloud Idle Inactivity Reaper background worker started.")
    while True:
        try:
            await asyncio.sleep(check_interval_seconds)
            await run_idle_reaper_cycle()
        except asyncio.CancelledError:
            logger.info("Idle Inactivity Reaper received cancellation. Shutting down.")
            break
        except Exception as exc:
            logger.error(f"Unexpected error in idle reaper loop: {exc}")
