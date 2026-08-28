#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(cd "${ROOT_DIR}" && python3 -c 'from app import __version__; print(__version__)')"
IMAGE="${DEVCLOUD_CONTROLLER_IMAGE:-localhost/devcloud-controller:${VERSION}}"
PYTHON_IMAGE="${DEVCLOUD_PYTHON_IMAGE:-registry.access.redhat.com/ubi10/python-312-minimal:latest}"

exec podman build \
    --build-arg "DEVCLOUD_VERSION=${VERSION}" \
    --build-arg "PYTHON_IMAGE=${PYTHON_IMAGE}" \
    --file "${ROOT_DIR}/containers/devcloud-controller/Containerfile" \
    --tag "${IMAGE}" \
    "${ROOT_DIR}"

