from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.workspace import WorkspaceStatus, consumes_compute
from app.orchestrator.flavors import FLAVORS, get_flavor
from app.resource_usage import (
    BYTES_PER_MB,
    get_system_usage,
    get_user_usage,
    quota_violations,
)


def test_user_usage_tracks_allocations_disk_and_remaining_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    user_dir = tmp_path / "7" / "workspace"
    user_dir.mkdir(parents=True)
    (user_dir / "project.bin").write_bytes(b"x" * 4096)

    user = SimpleNamespace(
        id=7,
        cpu_quota=2.0,
        memory_mb_quota=2048,
        disk_mb_quota=1024,
    )
    workspaces = [
        SimpleNamespace(flavor_id="t1.nano"),
        SimpleNamespace(flavor_id="t1.micro"),
    ]

    usage = get_user_usage(user, workspaces)

    assert usage["workspace_count"] == 2
    assert usage["cpu"]["used"] == 1.5
    assert usage["cpu"]["remaining"] == 0.5
    assert usage["memory"]["used"] == 1536 * BYTES_PER_MB
    assert usage["memory"]["remaining"] == 512 * BYTES_PER_MB
    assert usage["disk"]["used"] == 4096


def test_quota_violations_reports_cpu_ram_and_full_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    user = SimpleNamespace(
        id=9,
        cpu_quota=0.5,
        memory_mb_quota=512,
        disk_mb_quota=0,
    )
    workspaces = [SimpleNamespace(flavor_id="t1.nano")]

    violations = quota_violations(user, workspaces, get_flavor("t1.nano"))

    assert any("CPU 1.0/0.5 olacak" in item for item in violations)
    assert any("RAM 1024/512 MB olacak" in item for item in violations)
    assert any("Disk kullanımı" in item for item in violations)


def test_system_usage_exposes_cpu_memory_and_disk_metrics():
    usage = get_system_usage()

    assert set(usage) == {"cpu", "memory", "disk"}
    for metric in usage.values():
        assert 0 <= metric["percent"] <= 100
        assert metric["used"] >= 0
        assert metric["limit"] >= 0



def test_stopped_workspaces_release_cpu_and_ram_but_not_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    user_dir = tmp_path / "11" / "workspace"
    user_dir.mkdir(parents=True)
    (user_dir / "project.bin").write_bytes(b"x" * 2048)

    user = SimpleNamespace(
        id=11,
        cpu_quota=4.0,
        memory_mb_quota=4096,
        disk_mb_quota=1024,
    )
    workspaces = [
        SimpleNamespace(flavor_id="t1.micro", status=WorkspaceStatus.RUNNING),
        SimpleNamespace(flavor_id="t1.micro", status=WorkspaceStatus.STOPPED),
    ]

    usage = get_user_usage(user, workspaces)

    # Only the running workspace is charged for compute.
    assert usage["cpu"]["used"] == 1.0
    assert usage["memory"]["used"] == 1024 * BYTES_PER_MB
    # The pair is still reported as allocated so the UI can explain the gap.
    assert usage["allocated"]["cpu"]["used"] == 2.0
    assert usage["allocated"]["memory"]["used"] == 2048 * BYTES_PER_MB
    assert usage["workspace_count"] == 2
    assert usage["running_workspace_count"] == 1
    # Disk is measured from the filesystem and ignores lifecycle status.
    assert usage["disk"]["used"] == 2048


def test_in_flight_and_error_statuses_still_charge_compute(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    user = SimpleNamespace(
        id=12, cpu_quota=16.0, memory_mb_quota=16384, disk_mb_quota=1024
    )
    workspaces = [
        SimpleNamespace(flavor_id="t1.micro", status=WorkspaceStatus.CREATING),
        SimpleNamespace(flavor_id="t1.micro", status=WorkspaceStatus.STARTING),
        SimpleNamespace(flavor_id="t1.micro", status=WorkspaceStatus.STOPPING),
        SimpleNamespace(flavor_id="t1.micro", status=WorkspaceStatus.ERROR),
        SimpleNamespace(flavor_id="t1.micro", status=WorkspaceStatus.STOPPED),
    ]

    usage = get_user_usage(user, workspaces)

    assert usage["cpu"]["used"] == 4.0
    assert usage["running_workspace_count"] == 4


def test_plain_string_status_is_recognized():
    """Serialized payloads carry the status value, not the enum member."""
    assert consumes_compute(SimpleNamespace(status="running")) is True
    assert consumes_compute(SimpleNamespace(status="stopped")) is False
    assert consumes_compute(SimpleNamespace(status=WorkspaceStatus.STOPPED)) is False
    # A pending reservation has no lifecycle row yet and is always charged.
    assert consumes_compute(SimpleNamespace(flavor_id="t1.nano")) is True


def test_stopped_gpu_workspaces_keep_charging_pinned_slots(tmp_path, monkeypatch):
    """A stopped workspace keeps its accelerator slot reserved on the worker."""
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    gpu_flavor = next(
        (flavor for flavor in FLAVORS.values() if flavor.accelerator_count), None
    )
    if gpu_flavor is None:
        pytest.skip("No GPU flavor in the catalog.")

    user = SimpleNamespace(
        id=13, cpu_quota=256.0, memory_mb_quota=1048576, disk_mb_quota=1024, gpu_quota=4
    )
    workspaces = [SimpleNamespace(flavor_id=gpu_flavor.id, status=WorkspaceStatus.STOPPED)]

    usage = get_user_usage(user, workspaces)

    assert usage["gpu"]["used"] == gpu_flavor.accelerator_count
    assert usage["cpu"]["used"] == 0.0


def test_quota_violations_can_skip_the_disk_gate(tmp_path, monkeypatch):
    """Resuming a workspace must not be blocked by a full disk."""
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    user = SimpleNamespace(id=14, cpu_quota=4.0, memory_mb_quota=4096, disk_mb_quota=0)
    workspaces = [SimpleNamespace(flavor_id="t1.nano", status=WorkspaceStatus.STOPPED)]

    with_disk = quota_violations(user, workspaces, get_flavor("t1.nano"))
    without_disk = quota_violations(
        user, workspaces, get_flavor("t1.nano"), include_disk=False
    )

    assert any("Disk kullanımı" in item for item in with_disk)
    assert without_disk == []
