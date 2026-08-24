#!/usr/bin/env bash
# DevCloud Linux VM Deployment Script
set -e

echo "=== Starting DevCloud Platform Setup on Linux VM ==="

# 1. Detect Package Manager and Install Podman & Python
if command -v apt-get >/dev/null 2>&1; then
    echo "Updating apt repositories and installing Podman & Python dependencies..."
    sudo apt-get update
    sudo apt-get install -y podman python3 python3-pip python3-venv git curl
elif command -v dnf >/dev/null 2>&1; then
    echo "Installing Podman & Python via DNF..."
    sudo dnf install -y podman python3 python3-pip git curl
else
    echo "Unsupported package manager. Please ensure Podman and Python 3.11+ are installed."
fi

# 2. Setup Workspace Directory and Permissions
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACES_DIR="/var/lib/devcloud/workspaces"

echo "Creating workspace directory at: ${WORKSPACES_DIR}"
sudo mkdir -p "${WORKSPACES_DIR}"
sudo chown -R "$USER:$USER" "${WORKSPACES_DIR}"
sudo chmod 755 "${WORKSPACES_DIR}"

# 3. Create Python Virtual Environment & Install Dependencies
echo "Setting up Python virtual environment..."
cd "${PROJECT_DIR}"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Build Container Images
echo "Building local workspace container images..."
bash containers/build_images.sh

# 5. Setup Systemd Service
echo "Installing DevCloud systemd service..."
SERVICE_FILE="/etc/systemd/system/devcloud.service"
sudo sed -e "s|{{USER}}|$USER|g" \
         -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
         "${PROJECT_DIR}/deploy/devcloud.service" | sudo tee "${SERVICE_FILE}" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable devcloud
bash "${PROJECT_DIR}/deploy/restart.sh"

echo "=== DevCloud successfully installed and running! ==="
echo "Access the dashboard at: http://$(hostname -I | awk '{print $1}'):8000"
echo "Check service logs with: sudo journalctl -u devcloud -f"
