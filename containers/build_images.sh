#!/usr/bin/env bash
set -e

echo "====================================================="
echo "Building DevCloud Container Images with Podman"
echo "====================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The TCMB Code VSIX URL and SHA-256 are pinned in the Containerfile.
echo "1/3 Building devcloud-vscode-tcmb..."
podman build -t localhost/devcloud-vscode-tcmb:latest "${SCRIPT_DIR}/vscode-tcmb"

echo "2/3 Building devcloud-jupyter-python..."
podman build -t localhost/devcloud-jupyter-python:latest "${SCRIPT_DIR}/jupyter-python"

echo "3/3 Building devcloud-terminal-rocky..."
podman build -t localhost/devcloud-terminal-rocky:latest "${SCRIPT_DIR}/terminal-rocky"

echo "====================================================="
echo "All DevCloud workspace images built successfully!"
echo "====================================================="
podman images | grep devcloud
