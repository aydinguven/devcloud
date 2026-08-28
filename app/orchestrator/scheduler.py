import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.node import Node, NodeStatus
from app.models.workspace_image import WorkspaceImage
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


def _matches_selector(node: Node, selector: dict[str, str] | None) -> bool:
    if not selector:
        return True
    try:
        labels = json.loads(node.labels_json or "{}")
    except Exception:
        labels = {}
    return all(str(labels.get(k, "")) == str(v) for k, v in selector.items())


def _has_workspace_image(
    node: Node,
    image_ref: str | None,
    required_sha256: str | None = None,
) -> bool:
    if not image_ref:
        return required_sha256 is None
    try:
        capabilities = json.loads(node.capabilities_json or "{}")
    except ValueError:
        return False
    if not isinstance(capabilities, dict):
        return False
    if "workspace_images" not in capabilities:
        # Upgrade compatibility for old agents is safe only until an exact
        # controller-managed archive has been enabled for this image.
        return required_sha256 is None
    images = capabilities.get("workspace_images")
    return isinstance(images, list) and any(
        isinstance(item, dict)
        and item.get("image_ref") == image_ref
        and (
            required_sha256 is None
            or item.get("sha256") == required_sha256
        )
        for item in images
    )


async def select_worker_node(
    db: AsyncSession,
    flavor: Flavor,
    node_selector: dict[str, str] | None = None,
    required_image: str | None = None,
) -> Node:
    """Pick the best connected worker using capacity-aware load balancing."""
    nodes = (
        await db.execute(select(Node).where(Node.enabled.is_(True)))
    ).scalars().all()
    if not nodes:
        raise NoSchedulableNode(
            "Henüz kayıtlı worker yok. Bir worker kurup controller'a bağlayın."
        )

    required_sha256 = None
    if required_image:
        required_sha256 = (
            await db.execute(
                select(WorkspaceImage.sha256).where(
                    WorkspaceImage.enabled.is_(True),
                    WorkspaceImage.image_ref == required_image,
                )
            )
        ).scalar_one_or_none()

    candidates = [
        node
        for node in nodes
        if node.schedulable
        and node.status == NodeStatus.ONLINE
        and agent_manager.is_connected(node.id)
        and node.cpu_total >= flavor.cpus
        and node.memory_total_mb >= flavor.memory_mb
        and _matches_selector(node, node_selector)
        and _has_workspace_image(node, required_image, required_sha256)
    ]
    allocations = await _allocations(db)
    scored_candidates: list[tuple[float, int, int, str, Node]] = []
    for node in candidates:
        used = allocations.get(node.id, NodeAllocation())
        if used.cpu + flavor.cpus > node.cpu_total:
            continue
        if used.memory_mb + flavor.memory_mb > node.memory_total_mb:
            continue

        allocated_cpu_ratio = (used.cpu + flavor.cpus) / node.cpu_total if node.cpu_total else 1.0
        allocated_mem_ratio = (used.memory_mb + flavor.memory_mb) / node.memory_total_mb if node.memory_total_mb else 1.0
        live_cpu_ratio = (node.cpu_percent / 100.0) if node.cpu_percent else 0.0
        live_mem_ratio = (node.memory_used_mb / node.memory_total_mb) if node.memory_total_mb and node.memory_used_mb else 0.0

        # Intelligent load balancer score: weighted combination of allocated reservations and live utilization
        load_score = (
            0.35 * allocated_cpu_ratio
            + 0.35 * allocated_mem_ratio
            + 0.15 * live_cpu_ratio
            + 0.15 * live_mem_ratio
        )
        total_containers = used.workspaces + (node.active_containers_count or 0)
        free_memory_mb = node.memory_total_mb - used.memory_mb

        scored_candidates.append(
            (load_score, total_containers, -free_memory_mb, node.name, node)
        )

    if not scored_candidates:
        raise NoSchedulableNode(
            "Kayıtlı worker'ların hiçbiri bu kaynak profilini ve etiket kriterlerini karşılayamıyor. "
            "Node bağlantılarını, image senkronizasyonunu, drain durumunu ve boş CPU/RAM kapasitesini kontrol edin."
        )

    scored_candidates.sort(key=lambda item: item[:4])
    return scored_candidates[0][4]
