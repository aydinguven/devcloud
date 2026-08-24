#!/usr/bin/env python3
"""Cross-platform offline packaging utility for DevCloud.

Downloads all required pip wheels and orchestrates Podman image export.
"""

import os
import subprocess
import sys
import tarfile
from pathlib import Path


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(f"--> Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, check=True)
    if result.returncode != 0:
        sys.exit(result.returncode)


def main():
    root_dir = Path(__file__).resolve().parent.parent
    offline_dir = root_dir / "offline"
    wheels_dir = offline_dir / "wheels"
    images_dir = offline_dir / "images"

    wheels_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    print("==================================================")
    print("DevCloud Offline Packager")
    print("==================================================")

    # 1. Download Wheels
    print("\n[1/3] Downloading Python Wheels...")
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheels_dir),
            "-r",
            str(root_dir / "requirements.txt"),
        ]
    )

    # 2. Check Podman & export images if available
    print("\n[2/3] Checking Podman container images...")
    images = [
        ("devcloud-vscode-empty", "localhost/devcloud-vscode-empty:latest"),
        ("devcloud-vscode-python", "localhost/devcloud-vscode-python:latest"),
        ("devcloud-jupyter-python", "localhost/devcloud-jupyter-python:latest"),
        ("devcloud-vscode-java", "localhost/devcloud-vscode-java:latest"),
    ]

    podman_bin = os.getenv("PODMAN_BIN", "podman")
    podman_available = subprocess.run(
        [podman_bin, "--version"], capture_output=True
    ).returncode == 0

    if podman_available:
        print("Podman detected. Building and exporting images...")
        for name, tag in images:
            tar_path = images_dir / f"{name}.tar"
            print(f"Exporting {tag} -> {tar_path.name}...")
            run([podman_bin, "save", "-o", str(tar_path), tag])
    else:
        print("Podman not found in current environment.")
        print(f"Please ensure images are saved into '{images_dir}' before transferring.")
        # Create placeholder info
        (images_dir / "README.txt").write_text(
            "Place saved podman tarballs here: devcloud-vscode-empty.tar, devcloud-vscode-python.tar, devcloud-jupyter-python.tar, devcloud-vscode-java.tar",
            encoding="utf-8",
        )

    # 3. Create bundle archive
    print("\n[3/3] Creating offline distribution bundle...")
    bundle_path = root_dir / "devcloud-offline-bundle.tar.gz"
    
    excluded_names = {".git", ".venv", "__pycache__", ".pytest_cache"}

    def tar_filter(tarinfo):
        for excl in excluded_names:
            if excl in tarinfo.name.split(os.sep) or excl in tarinfo.name.split("/"):
                return None
        return tarinfo

    with tarfile.open(bundle_path, "w:gz") as tar:
        tar.add(str(root_dir), arcname="devcloud", filter=tar_filter)

    print("==================================================")
    print(f"Offline bundle ready: {bundle_path}")
    print("==================================================")


if __name__ == "__main__":
    main()
