import subprocess

from app.worker_gpu import (
    discover_nvidia_capabilities,
    nvidia_hardware_present,
    parse_cdi_devices,
    parse_nvidia_smi_csv,
    parse_nvidia_smi_list,
)


def completed(command, *, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_nvidia_hardware_detection_uses_vendor_and_display_class(tmp_path):
    gpu = tmp_path / "pci-device-0"
    gpu.mkdir()
    (gpu / "vendor").write_text("0x10de\n", encoding="ascii")
    (gpu / "class").write_text("0x030200\n", encoding="ascii")

    assert nvidia_hardware_present(tmp_path) is True

    (gpu / "class").write_text("0x020000\n", encoding="ascii")
    assert nvidia_hardware_present(tmp_path) is False


def test_nvidia_parsers_cover_physical_mig_and_cdi_devices():
    physical = parse_nvidia_smi_csv(
        "0, GPU-parent, NVIDIA B300, 285696, 1024, 12, 41, 590.00, Enabled\n"
    )
    mig = parse_nvidia_smi_list(
        "GPU 0: NVIDIA B300 (UUID: GPU-parent)\n"
        "  MIG 2g.67gb Device 0: (UUID: MIG-GPU-parent/1/0)\n"
    )
    cdi = parse_cdi_devices(
        "nvidia.com/gpu=GPU-parent\nnvidia.com/gpu=MIG-GPU-parent/1/0\n"
    )

    assert physical[0]["memory_mb"] == 285696
    assert physical[0]["mig_enabled"] is True
    assert mig[0]["parent_id"] == "GPU-parent"
    assert mig[0]["memory_mb"] == 67 * 1024
    assert "nvidia.com/gpu=MIG-GPU-parent/1/0" in cdi


def test_discovery_marks_mig_slice_allocatable_and_parent_non_allocatable(tmp_path):
    def runner(command, **_kwargs):
        if "--query-gpu=index" in command[1]:
            return completed(
                command,
                stdout=(
                    "0, GPU-parent, NVIDIA B300, 285696, 2048, 10, 42, "
                    "590.00, Enabled\n"
                ),
            )
        if command[-1] == "-L":
            return completed(
                command,
                stdout=(
                    "GPU 0: NVIDIA B300 (UUID: GPU-parent)\n"
                    "  MIG 2g.67gb Device 0: (UUID: MIG-GPU-parent/1/0)\n"
                ),
            )
        if command[-1] == "--version":
            return completed(command, stdout="NVIDIA Container Toolkit CLI 1.18.0\n")
        return completed(
            command,
            stdout=(
                "nvidia.com/gpu=GPU-parent\n"
                "nvidia.com/gpu=MIG-GPU-parent/1/0\n"
            ),
        )

    snapshot = discover_nvidia_capabilities(
        runner=runner,
        which=lambda command: f"/usr/bin/{command}",
        devices_root=tmp_path,
    )

    runtime = snapshot["accelerator_runtime"]["nvidia"]
    physical, mig = snapshot["accelerators"]
    assert runtime["status"] == "ready"
    assert runtime["physical_device_count"] == 1
    assert runtime["mig_device_count"] == 1
    assert physical["allocatable"] is False
    assert mig["allocatable"] is True
    assert mig["cdi_name"] == "nvidia.com/gpu=MIG-GPU-parent/1/0"
    assert mig["model"] == "NVIDIA B300"


def test_discovery_reports_missing_container_toolkit(tmp_path):
    def runner(command, **_kwargs):
        if command[-1] == "-L":
            return completed(command, stdout="GPU 0: RTX 5090 (UUID: GPU-5090)\n")
        return completed(
            command,
            stdout="0, GPU-5090, NVIDIA GeForce RTX 5090, 32607, 0, 0, 35, 590.00, N/A\n",
        )

    snapshot = discover_nvidia_capabilities(
        runner=runner,
        which=lambda command: "/usr/bin/nvidia-smi" if command == "nvidia-smi" else None,
        devices_root=tmp_path,
    )

    runtime = snapshot["accelerator_runtime"]["nvidia"]
    assert runtime["status"] == "error"
    assert runtime["cdi_ready"] is False
    assert "Container Toolkit" in runtime["message"]


def test_discovery_reports_cpu_worker_without_gpu_tools(tmp_path):
    snapshot = discover_nvidia_capabilities(
        which=lambda _command: None,
        devices_root=tmp_path,
    )

    assert snapshot["accelerators"] == []
    assert snapshot["accelerator_runtime"]["nvidia"]["status"] == "not_detected"
