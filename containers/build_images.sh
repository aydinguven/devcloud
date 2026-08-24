#!/usr/bin/env bash
set -e

echo "====================================================="
echo "Building DevCloud Container Images with Podman"
echo "====================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "1/5 Building devcloud-vscode-empty..."
podman build -t localhost/devcloud-vscode-empty:latest "${SCRIPT_DIR}/vscode-empty"

echo "2/5 Building devcloud-vscode-python..."
podman build -t localhost/devcloud-vscode-python:latest "${SCRIPT_DIR}/vscode-python"

echo "3/5 Building devcloud-vscode-react..."
podman build -t localhost/devcloud-vscode-react:latest "${SCRIPT_DIR}/vscode-react"

echo "4/5 Building devcloud-jupyter-python..."
podman build -t localhost/devcloud-jupyter-python:latest "${SCRIPT_DIR}/jupyter-python"

echo "5/5 Building devcloud-vscode-java..."
podman build -t localhost/devcloud-vscode-java:latest "${SCRIPT_DIR}/vscode-java"

echo "====================================================="
echo "All DevCloud workspace images built successfully!"
echo "====================================================="
podman images | grep devcloud
