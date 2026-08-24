#!/usr/bin/env bash
# ==============================================================================
# DevCloud Air-Gapped / Offline Linux VM Deployment Script
# Installs DevCloud and loads all container images WITHOUT any internet access.
# ==============================================================================
set -e

echo "=== Starting DevCloud Offline Deployment ==="

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OFFLINE_DIR="${PROJECT_DIR}/offline"
WHEELS_DIR="${OFFLINE_DIR}/wheels"
IMAGES_DIR="${OFFLINE_DIR}/images"
WORKSPACES_DIR="/var/lib/devcloud/workspaces"

# 1. Verify Prerequisites
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is not installed. Please pre-install Python 3.11+ on this VM."
    exit 1
fi

if ! command -v podman >/dev/null 2>&1; then
    echo "ERROR: podman is not installed. Please pre-install Podman on this VM."
    exit 1
fi

if [ ! -d "${WHEELS_DIR}" ]; then
    echo "ERROR: Offline wheels directory (${WHEELS_DIR}) not found!"
    exit 1
fi

# 2. Setup Persistent Storage Directory
echo "=== [1/4] Configuring persistent storage directory ==="
sudo mkdir -p "${WORKSPACES_DIR}"
sudo chown -R "$USER:$USER" "${WORKSPACES_DIR}"
sudo chmod 755 "${WORKSPACES_DIR}"

# 3. Create Virtualenv and Install Cached Wheels
echo "=== [2/4] Installing Python packages from offline wheels ==="
cd "${PROJECT_DIR}"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

# Activate virtual environment
source .venv/bin/activate

# Install from offline wheels directory without connecting to PyPI
pip install --no-index --find-links="${WHEELS_DIR}" -r requirements.txt

# 4. Load Container Images into Podman
echo "=== [3/4] Loading container images into Podman ==="
if [ -d "${IMAGES_DIR}" ]; then
    for tar_file in "${IMAGES_DIR}"/*.tar "${IMAGES_DIR}"/*.tar.gz; do
        if [ -f "$tar_file" ]; then
            echo "Loading image: $(basename "$tar_file")..."
            podman load -i "$tar_file"
        fi
    done
else
    echo "WARNING: No offline/images directory found. If images were preloaded, skipping."
fi

# Verify loaded images
echo "Available Podman images:"
podman images | grep -E "devcloud|REPOSITORY" || true

# 5. Install and Start Systemd Service
echo "=== [4/4] Configuring and starting systemd service ==="
SERVICE_FILE="/etc/systemd/system/devcloud.service"
sudo sed -e "s|{{USER}}|$USER|g" \
         -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
         "${PROJECT_DIR}/deploy/devcloud.service" | sudo tee "${SERVICE_FILE}" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable devcloud
sudo systemctl restart devcloud

echo "=============================================================================="
echo "DevCloud Offline Deployment Completed Successfully!"
echo "Status: Active and running on systemd service 'devcloud'"
echo "Dashboard URL: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '127.0.0.1'):8000"
echo "Check logs: sudo journalctl -u devcloud -f"
echo "=============================================================================="
