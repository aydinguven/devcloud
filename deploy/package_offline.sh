#!/usr/bin/env bash
# ==============================================================================
# DevCloud Offline Packaging Script
# Run this on an internet-connected machine to build images, download wheels,
# and package a self-contained offline distribution tarball.
# ==============================================================================
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OFFLINE_DIR="${PROJECT_DIR}/offline"
WHEELS_DIR="${OFFLINE_DIR}/wheels"
IMAGES_DIR="${OFFLINE_DIR}/images"

echo "=== [1/4] Preparing offline bundle directories ==="
mkdir -p "${WHEELS_DIR}" "${IMAGES_DIR}"

echo "=== [2/4] Downloading Python Wheels for offline install ==="
python3 -m pip download --dest "${WHEELS_DIR}" -r "${PROJECT_DIR}/requirements.txt"

echo "=== [3/4] Building and Exporting Podman Container Images ==="
# Build images
bash "${PROJECT_DIR}/containers/build_images.sh"

# Export images to tar archives
echo "Saving devcloud-vscode-empty image..."
podman save -o "${IMAGES_DIR}/devcloud-vscode-empty.tar" localhost/devcloud-vscode-empty:latest

echo "Saving devcloud-vscode-python image..."
podman save -o "${IMAGES_DIR}/devcloud-vscode-python.tar" localhost/devcloud-vscode-python:latest

echo "Saving devcloud-jupyter-python image..."
podman save -o "${IMAGES_DIR}/devcloud-jupyter-python.tar" localhost/devcloud-jupyter-python:latest

echo "Saving devcloud-vscode-java image..."
podman save -o "${IMAGES_DIR}/devcloud-vscode-java.tar" localhost/devcloud-vscode-java:latest

echo "=== [4/4] Creating self-contained offline archive ==="
BUNDLE_FILE="${PROJECT_DIR}/devcloud-offline-bundle.tar.gz"
cd "${PROJECT_DIR}/.."
tar --exclude='.git' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    -czf "${BUNDLE_FILE}" \
    "$(basename "${PROJECT_DIR}")"

echo "=============================================================================="
echo "Offline bundle created successfully: ${BUNDLE_FILE}"
echo "Transfer this archive to your air-gapped Linux VM and run deploy/deploy_offline.sh"
echo "=============================================================================="
