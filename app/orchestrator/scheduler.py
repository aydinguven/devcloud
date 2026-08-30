import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.manager import agent_manager
from app.models.node import Node, NodeStatus
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.workspace_image import WorkspaceImage
from app.orchestrator.flavors import Flavor, get_flavor


class NoSchedulableNode(RuntimeError):
    pass


@dataclass(frozen=True)
class NodeAllocation:
    cpu: float = 0
    memory_mb: int = 0
    workspaces: int = 0


@dataclass(frozen=True)
class AcceleratorPlacement:
    device_id: str
    cdi_name: str
    model: str
    kind: str
    slot: int
    memory_mb: int
    shared_slots: int


@dataclass(frozen=True)
class WorkspacePlacement:
    node: Node
    accelerator: AcceleratorPlacement | None = None


ACTIVE_ALLOCATION_STATUSES = {
    WorkspaceStatus.CREATING,
    WorkspaceStatus.STARTING,
    WorkspaceStatus.RUNNING,
}
CDI_DEVICE_PATTERN = re.compile(r"^nvidia\.com/gpu=[A-Za-z0-9_.:/-]+$")


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


async def _reserved_accelerator_slots(
    db: AsyncSession,
) -> dict[tuple[str, str], set[int]]:
    result = await db.execute(
        select(
            Workspace.node_id,
            Workspace.accelerator_device_id,
            Workspace.accelerator_slot,
        ).where(
            Workspace.accelerator_device_id.is_not(None),
            Workspace.accelerator_slot.is_not(None),
            Workspace.status != WorkspaceStatus.DELETED,
        )
    )
    reserved: dict[tuple[str, str], set[int]] = {}
    for node_id, device_id, slot in result.all():
        if not node_id or not device_id or slot is None:
            continue
        reserved.setdefault((str(node_id), str(device_id)), set()).add(int(slot))
    return reserved


def _matches_selector(node: Node, selector: dict[str, str] | None) -> bool:
    if not selector:
        return True
    try:
        labels = json.loads(node.labels_json or "{}")
    except Exception:
        labels = {}
    return all(str(labels.get(k, "")) == str(v) for k, v in selector.items())


def _capabilities(node: Node) -> dict:
    try:
        value = json.loads(node.capabilities_json or "{}")
    except ValueError:
        return {"workspace_images": []}
    return value if isinstance(value, dict) else {}


def _accelerators(node: Node) -> list[dict]:
    value = _capabilities(node).get("accelerators", [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _valid_cdi_name(value: object) -> str:
    name = str(value or "")
    if not CDI_DEVICE_PATTERN.fullmatch(name):
        return ""
    if name.rsplit("=", 1)[-1].lower() == "all":
        return ""
    return name


def gpu_slots_for_device(node: Node, device: dict) -> int:
    """Return the configured/automatic number of workspace slots."""
    if str(device.get("kind") or "") == "mig":
        return 1
    configured = int(node.gpu_slots_per_device or 0)
    if configured:
        return max(1, min(configured, 3))
    model = str(device.get("model") or "").lower()
    if "5090" in model:
        return 3
    if "4090" in model:
        return 2
    return 1


def _has_workspace_image(
    node: Node,
    image_ref: str | None,
    required_sha256: str | None = None,
) -> bool:
    if not image_ref:
        return required_sha256 is None
    capabilities = _capabilities(node)
    if "workspace_images" not in capabilities:
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


def _free_accelerator_placements(
    node: Node,
    flavor: Flavor,
    reserved: dict[tuple[str, str], set[int]],
) -> list[AcceleratorPlacement]:
    if not flavor.accelerator_count:
        return []
    placements: list[AcceleratorPlacement] = []
    for device in _accelerators(node):
        if str(device.get("vendor") or "").lower() != flavor.accelerator_vendor:
            continue
        if not device.get("healthy") or not device.get("allocatable"):
            continue
        kind = str(device.get("kind") or "")
        if kind not in {"physical", "mig"}:
            continue
        device_id = str(device.get("id") or "")[:160]
        cdi_name = _valid_cdi_name(device.get("cdi_name"))
        model = str(device.get("model") or "NVIDIA GPU")[:160]
        memory_mb = max(int(device.get("memory_mb") or 0), 0)
        if (
            not device_id
            or not cdi_name
            or memory_mb < flavor.accelerator_memory_mb
        ):
            continue
        slots = gpu_slots_for_device(node, device)
        used_slots = reserved.get((node.id, device_id), set())
        for slot in range(slots):
            if slot not in used_slots:
                placements.append(
                    AcceleratorPlacement(
                        device_id=device_id,
                        cdi_name=cdi_name,
                        model=model,
                        kind=kind,
                        slot=slot,
                        memory_mb=flavor.accelerator_memory_mb,
                        shared_slots=slots,
                    )
                )
    return placements


async def select_workspace_placement(
    db: AsyncSession,
    flavor: Flavor,
    node_selector: dict[str, str] | None = None,
    required_image: str | None = None,
) -> WorkspacePlacement:
    """Pick a connected worker and, for GPU flavors, one exact free CDI slot."""
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
    reserved = await _reserved_accelerator_slots(db)
    scored: list[
        tuple[int, float, float, int, int, str, int, WorkspacePlacement]
    ] = []
    for node in candidates:
        used = allocations.get(node.id, NodeAllocation())
        if used.cpu + flavor.cpus > node.cpu_total:
            continue
        if used.memory_mb + flavor.memory_mb > node.memory_total_mb:
            continue

        allocated_cpu_ratio = (
            (used.cpu + flavor.cpus) / node.cpu_total if node.cpu_total else 1.0
        )
        allocated_mem_ratio = (
            (used.memory_mb + flavor.memory_mb) / node.memory_total_mb
            if node.memory_total_mb
            else 1.0
        )
        live_cpu_ratio = (node.cpu_percent / 100.0) if node.cpu_percent else 0.0
        live_mem_ratio = (
            node.memory_used_mb / node.memory_total_mb
            if node.memory_total_mb and node.memory_used_mb
            else 0.0
        )
        load_score = (
            0.35 * allocated_cpu_ratio
            + 0.35 * allocated_mem_ratio
            + 0.15 * live_cpu_ratio
            + 0.15 * live_mem_ratio
        )
        total_containers = used.workspaces + (node.active_containers_count or 0)
        free_memory_mb = node.memory_total_mb - used.memory_mb

        if flavor.accelerator_count:
            device_placements = _free_accelerator_placements(
                node, flavor, reserved
            )
            for accelerator in device_placements:
                device = next(
                    (
                        item
                        for item in _accelerators(node)
                        if str(item.get("id") or "") == accelerator.device_id
                    ),
                    {},
                )
                gpu_utilization = float(
                    device.get("utilization_percent") or 0
                ) / 100.0
                kind_rank = 0 if accelerator.kind == "mig" else 1
                scored.append(
                    (
                        0,
                        load_score,
                        gpu_utilization,
                        kind_rank,
                        total_containers,
                        node.name,
                        accelerator.slot,
                        WorkspacePlacement(node=node, accelerator=accelerator),
                    )
                )
            continue

        # Preserve GPU capacity for GPU requests when an equivalent CPU-only
        # worker is available.
        gpu_worker_penalty = int(
            any(item.get("allocatable") for item in _accelerators(node))
        )
        scored.append(
            (
                gpu_worker_penalty,
                load_score,
                0.0,
                0,
                total_containers,
                node.name,
                -free_memory_mb,
                WorkspacePlacement(node=node),
            )
        )

    if not scored:
        if flavor.accelerator_count:
            raise NoSchedulableNode(
                "Bu GPU profili için boş ve sağlıklı NVIDIA CDI slotu bulunamadı. "
                "GPU kotasını, worker GPU politikasını, MIG/CDI durumunu, image "
                "senkronizasyonunu ve CPU/RAM kapasitesini kontrol edin."
            )
        raise NoSchedulableNode(
            "Kayıtlı worker'ların hiçbiri bu kaynak profilini ve etiket kriterlerini karşılayamıyor. "
            "Node bağlantılarını, image senkronizasyonunu, drain durumunu ve boş CPU/RAM kapasitesini kontrol edin."
        )

    scored.sort(key=lambda item: item[:7])
    return scored[0][7]


async def select_worker_node(
    db: AsyncSession,
    flavor: Flavor,
    node_selector: dict[str, str] | None = None,
    required_image: str | None = None,
) -> Node:
    """Compatibility wrapper for CPU-only build scheduling callers."""
    placement = await select_workspace_placement(
        db,
        flavor,
        node_selector=node_selector,
        required_image=required_image,
    )
    return placement.node


async def flavor_availability(
    db: AsyncSession, flavor: Flavor
) -> tuple[bool, str]:
    """Return whether a flavor has a placement now, without reserving it."""
    try:
        await select_workspace_placement(db, flavor)
    except NoSchedulableNode as exc:
        return False, str(exc)
    return True, "Kullanılabilir kapasite bulundu."
