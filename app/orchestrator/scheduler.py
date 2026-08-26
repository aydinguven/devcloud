from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.node import Node, NodeStatus
from app.models.workspace import Workspace, WorkspaceStatus
from app.orchestrator.flavors import Flavor, get_flavor
from app.agents.manager import agent_manager


class NoSchedulableNode(RuntimeError):
    pass


@dataclass(frozen=True)
class NodeAllocation:
    cpu: float = 0
    memory_mb: int = 0
    workspaces: int = 0


ACTIVE_ALLOCATION_STATUSES = {
    WorkspaceStatus.CREATING,
    WorkspaceStatus.STARTING,
    WorkspaceStatus.RUNNING,
}


async def _allocations(db: AsyncSession) -> dict[str, NodeAllocation]:
    result = await db.execute(
        select(Workspace).where(
            Workspace.node_id.is_not(None),
            Workspace.status.in_(ACTIVE_ALLOCATION_STATUSES),
        )
    )
    totals: dict[str, NodeAllocation] = {}
    for workspace in result.scalars().all():
        flavor = get_flavor(workspace.flavor_id)
        if not flavor or not workspace.node_id:
            continue
        current = totals.get(workspace.node_id, NodeAllocation())
        totals[workspace.node_id] = NodeAllocation(
            cpu=current.cpu + flavor.cpus,
            memory_mb=current.memory_mb + flavor.memory_mb,
            workspaces=current.workspaces + 1,
        )
    return totals


async def select_worker_node(db: AsyncSession, flavor: Flavor) -> Node | None:
    """Pick an online worker, or preserve legacy local mode if none are registered."""
    nodes = (
        await db.execute(select(Node).where(Node.enabled.is_(True)))
    ).scalars().all()
    if not nodes:
        return None

    candidates = [
        node
        for node in nodes
        if node.schedulable
        and node.status == NodeStatus.ONLINE
        and agent_manager.is_connected(node.id)
        and node.cpu_total >= flavor.cpus
        and node.memory_total_mb >= flavor.memory_mb
    ]
    allocations = await _allocations(db)
    fitting: list[tuple[float, int, str, Node]] = []
    for node in candidates:
        used = allocations.get(node.id, NodeAllocation())
        if used.cpu + flavor.cpus > node.cpu_total:
            continue
        if used.memory_mb + flavor.memory_mb > node.memory_total_mb:
            continue
        cpu_ratio = used.cpu / node.cpu_total if node.cpu_total else 1
        memory_ratio = used.memory_mb / node.memory_total_mb if node.memory_total_mb else 1
        fitting.append((max(cpu_ratio, memory_ratio), used.workspaces, node.name, node))

    if not fitting:
        raise NoSchedulableNode(
            "Kayıtlı worker'ların hiçbiri bu kaynak profilini karşılayamıyor. "
            "Node bağlantılarını, drain durumunu ve boş CPU/RAM kapasitesini kontrol edin."
        )
    fitting.sort(key=lambda item: item[:3])
    return fitting[0][3]
