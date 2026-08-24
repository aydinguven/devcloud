from types import SimpleNamespace

from app.config import settings
from app.orchestrator.flavors import get_flavor
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

    assert any("CPU would be 1.0/0.5 cores" in item for item in violations)
    assert any("RAM would be 1024/512 MB" in item for item in violations)
    assert any("disk usage" in item for item in violations)


def test_system_usage_exposes_cpu_memory_and_disk_metrics():
    usage = get_system_usage()

    assert set(usage) == {"cpu", "memory", "disk"}
    for metric in usage.values():
        assert 0 <= metric["percent"] <= 100
        assert metric["used"] >= 0
        assert metric["limit"] >= 0
