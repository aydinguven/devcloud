#!/usr/bin/env bash
# ==============================================================================
# DevCloud Platform 1-Click Self-Update Script
# ==============================================================================
set -e

PROJECT_DIR="/opt/devcloud"
if [ -d "$PROJECT_DIR" ]; then
    cd "$PROJECT_DIR"
fi

echo "--> [1/4] Fetching latest commits from git.aydin.cloud..."
git fetch origin
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
git pull origin "$CURRENT_BRANCH"

echo "--> [2/4] Updating Python dependencies..."
if [ -f ".venv/bin/pip" ]; then
    .venv/bin/pip install --upgrade pip --no-warn-script-location
    if [ -f "requirements.txt" ]; then
        .venv/bin/pip install -r requirements.txt --no-warn-script-location
    fi
fi

echo "--> [3/4] Updating systemd unit file..."
if [ -f "deploy/devcloud.service" ]; then
    sudo cp deploy/devcloud.service /etc/systemd/system/devcloud.service
    sudo sed -i "s|{{USER}}|$USER|g" /etc/systemd/system/devcloud.service
    sudo sed -i "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" /etc/systemd/system/devcloud.service
    sudo systemctl daemon-reload
fi

echo "--> [4/4] Scheduling fast daemon restart..."
(sleep 1 && sudo systemctl restart devcloud) &

echo "--> Update completed successfully! Service is reloading in background."
