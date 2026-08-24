#!/usr/bin/env python3
"""Cross-platform offline packaging utility for DevCloud.

Downloads Linux-compatible wheels (manylinux_2_28 / manylinux2014) for Python 3.11, 3.12, and 3.13,
and exports all Podman container images so the bundle installs seamlessly on any Fedora-based
Linux VM (Rocky Linux, RHEL, CentOS Stream, Fedora, AlmaLinux).
"""

import os
import subprocess
import sys
import tarfile
from pathlib import Path


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(f"--> Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        print(f"WARNING: Command returned non-zero code {result.returncode}")


def main():
    root_dir = Path(__file__).resolve().parent.parent
    offline_dir = root_dir / "offline"
    wheels_dir = offline_dir / "wheels"
    images_dir = offline_dir / "images"

    wheels_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    print("==================================================================")
    print("DevCloud Universal Linux / Fedora Offline Packager")
    print("==================================================================")

    # 1. Download Linux-compatible Wheels for Fedora/Rocky/RHEL Python versions (3.11, 3.12, 3.13)
    target_py_versions = ["311", "312", "313"]
    req_file = str(root_dir / "requirements.txt")

    print("\n[1/3] Downloading Linux x86_64 Wheels for Fedora/Rocky/RHEL...")
    for py_ver in target_py_versions:
        print(f"\n---> Fetching Linux wheels for Python {py_ver[0]}.{py_ver[1:]} (manylinux_2_28_x86_64)...")
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--dest",
                str(wheels_dir),
                "--platform",
                "manylinux_2_28_x86_64",
                "--platform",
                "manylinux2014_x86_64",
                "--platform",
                "manylinux_2_17_x86_64",
                "--implementation",
                "cp",
                "--python-version",
                py_ver,
                "--only-binary=:all:",
                "-r",
                req_file,
            ]
        )

    # Also download any pure-python wheels
    print("\n---> Fetching universal pure-python wheels...")
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheels_dir),
            "--no-deps",
            "-r",
            req_file,
        ]
    )

    # 2. Check Podman & export images if available
    print("\n[2/3] Checking Podman container images...")
    images = [
        ("devcloud-vscode-empty", "localhost/devcloud-vscode-empty:latest"),
        ("devcloud-vscode-python", "localhost/devcloud-vscode-python:latest"),
        ("devcloud-vscode-react", "localhost/devcloud-vscode-react:latest"),
        ("devcloud-jupyter-python", "localhost/devcloud-jupyter-python:latest"),
        ("devcloud-vscode-java", "localhost/devcloud-vscode-java:latest"),
    ]

    podman_bin = os.getenv("PODMAN_BIN", "podman")
    podman_available = (
        subprocess.run([podman_bin, "--version"], capture_output=True).returncode == 0
    )

    if podman_available:
        print(f"Podman detected. Ensuring all {len(images)} images are built and exported...")
        for name, tag in images:
            # Check if image exists; if not, build it
            exists_res = subprocess.run([podman_bin, "image", "exists", tag])
            if exists_res.returncode != 0:
                short_name = name.replace("devcloud-", "")
                container_path = root_dir / "containers" / short_name
                if container_path.exists():
                    print(f"Building {tag} from {container_path.name}...")
                    run([podman_bin, "build", "-t", tag, str(container_path)])

            tar_path = images_dir / f"{name}.tar"
            print(f"Exporting {tag} -> {tar_path.name}...")
            run([podman_bin, "save", "-o", str(tar_path), tag])
    else:
        print("Podman not found on packaging host.")
        print(f"Note: Ensure images are saved into '{images_dir}' before deploying offline.")
        (images_dir / "README.txt").write_text(
            "Place saved podman tarballs here: devcloud-vscode-empty.tar, devcloud-vscode-python.tar, devcloud-vscode-react.tar, devcloud-jupyter-python.tar, devcloud-vscode-java.tar",
            encoding="utf-8",
        )

    # 3. Create bundle archive
    print("\n[3/3] Creating self-contained offline distribution bundle...")
    bundle_path = root_dir / "devcloud-offline-bundle.tar.gz"

    excluded_names = {".git", ".venv", "__pycache__", ".pytest_cache"}

    def tar_filter(tarinfo):
        for excl in excluded_names:
            if excl in tarinfo.name.split(os.sep) or excl in tarinfo.name.split("/"):
                return None
        return tarinfo

    with tarfile.open(bundle_path, "w:gz") as tar:
        tar.add(str(root_dir), arcname="devcloud", filter=tar_filter)

    print("==================================================================")
    print(f"Universal Linux offline bundle ready: {bundle_path}")
    print("Compatible with: Rocky Linux 9/10, RHEL 9/10, Fedora 39/40/41/42, CentOS Stream")
    print("==================================================================")


if __name__ == "__main__":
    main()
