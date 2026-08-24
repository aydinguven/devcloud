#!/usr/bin/env bash
# ==============================================================================
# DevCloud Air-Gapped / Offline Linux VM Deployment Script
# Specifically optimized for Fedora-based distros (Rocky Linux, RHEL, Fedora, CentOS)
# ==============================================================================
set -e

echo "=============================================================================="
echo "Starting DevCloud Offline Deployment (Fedora / Rocky / RHEL Ecosystem)"
echo "=============================================================================="

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OFFLINE_DIR="${PROJECT_DIR}/offline"
WHEELS_DIR="${OFFLINE_DIR}/wheels"
IMAGES_DIR="${OFFLINE_DIR}/images"
WORKSPACES_DIR="/var/lib/devcloud/workspaces"

# 1. Verify Prerequisites
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is not found. Please ensure Python 3.11+ is installed."
    exit 1
fi

if ! command -v podman >/dev/null 2>&1; then
    echo "ERROR: podman is not found. Please ensure Podman is installed (sudo dnf install -y podman)."
    exit 1
fi

if [ ! -d "${WHEELS_DIR}" ]; then
    echo "ERROR: Offline wheels directory (${WHEELS_DIR}) not found!"
    exit 1
fi

# Print Detected Environment Info
PYTHON_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
echo "--> Detected Python runtime: ${PYTHON_VER}"
echo "--> Detected Host OS: $(cat /etc/redhat-release 2>/dev/null || cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 || echo 'Linux')"

# 2. Setup Persistent Storage Directory and SELinux
echo "=== [1/5] Configuring workspace persistent storage directory ==="
sudo mkdir -p "${WORKSPACES_DIR}"
sudo chown -R "$USER:$USER" "${WORKSPACES_DIR}"
sudo chmod 755 "${WORKSPACES_DIR}"

# If SELinux is active (standard on Rocky/Fedora/RHEL), set container file label
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" != "Disabled" ]; then
    echo "--> SELinux detected ($(getenforce)). Setting container_file_t context on workspace storage..."
    sudo chcon -Rt container_file_t "${WORKSPACES_DIR}" 2>/dev/null || true
fi

# 3. Create Virtualenv and Install Cached Linux Wheels
echo "=== [2/5] Installing Python packages from offline wheels ==="
cd "${PROJECT_DIR}"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate

# Install from offline wheels directory without connecting to PyPI
pip install --no-index --find-links="${WHEELS_DIR}" -r requirements.txt

# 4. Load Container Images into Podman
echo "=== [3/5] Loading container images into Podman ==="
IMAGE_COUNT=0
if [ -d "${IMAGES_DIR}" ]; then
    for tar_file in "${IMAGES_DIR}"/*.tar "${IMAGES_DIR}"/*.tar.gz; do
        if [ -f "$tar_file" ]; then
            echo "--> Loading image: $(basename "$tar_file")..."
            podman load -i "$tar_file"
            IMAGE_COUNT=$((IMAGE_COUNT + 1))
        fi
    done
fi

if [ "$IMAGE_COUNT" -eq 0 ]; then
    echo "WARNING: No pre-exported container images found in ${IMAGES_DIR}."
    echo "If you have internet access or built images locally, building them now..."
    if [ -f "${PROJECT_DIR}/containers/build_images.sh" ]; then
        bash "${PROJECT_DIR}/containers/build_images.sh" || true
    fi
fi

echo "Available Podman images:"
podman images | grep -E "devcloud|REPOSITORY" || true

# 5. Configure Firewalld (Standard on Fedora / Rocky / RHEL)
echo "=== [4/5] Configuring firewall port 8000 ==="
if command -v firewall-cmd >/dev/null 2>&1 && sudo systemctl is-active --quiet firewalld; then
    echo "--> Adding port 8000/tcp to firewalld rules..."
    sudo firewall-cmd --permanent --add-port=8000/tcp 2>/dev/null || true
    sudo firewall-cmd --reload 2>/dev/null || true
fi

# 6. Install and Start Systemd Service
echo "=== [5/5] Configuring and starting systemd service ==="
SERVICE_FILE="/etc/systemd/system/devcloud.service"
sudo sed -e "s|{{USER}}|$USER|g" \
         -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
         "${PROJECT_DIR}/deploy/devcloud.service" | sudo tee "${SERVICE_FILE}" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable devcloud
bash "${PROJECT_DIR}/deploy/restart.sh"

IP_ADDR=$(hostname -I 2>/dev/null | awk '{print $1}' || echo '127.0.0.1')

echo "=============================================================================="
echo "DevCloud Deployment Completed Successfully on Fedora-based VM!"
echo "Status: Active and running on systemd service 'devcloud'"
echo "Dashboard URL: http://${IP_ADDR}:8000"
echo "Check logs: sudo journalctl -u devcloud -f"
echo "=============================================================================="
