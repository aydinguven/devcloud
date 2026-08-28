#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="$(cd "${ROOT_DIR}" && python3 -c 'from app import __version__; print(__version__)')"
OUTPUT_DIR="${1:-${ROOT_DIR}/dist/container-images}"
CONTROLLER_IMAGE="${DEVCLOUD_CONTROLLER_IMAGE:-localhost/devcloud-controller:${VERSION}}"
POSTGRES_IMAGE="${DEVCLOUD_POSTGRES_IMAGE:-registry.redhat.io/rhel10/postgresql-16:latest}"

mkdir -p "${OUTPUT_DIR}"
podman image exists "${CONTROLLER_IMAGE}" || {
    echo "ERROR: Controller image is missing: ${CONTROLLER_IMAGE}" >&2
    exit 1
}
podman image exists "${POSTGRES_IMAGE}" || {
    echo "ERROR: PostgreSQL image is missing: ${POSTGRES_IMAGE}" >&2
    exit 1
}

podman save --format oci-archive \
    --output "${OUTPUT_DIR}/devcloud-controller.tar" \
    "${CONTROLLER_IMAGE}"
podman save --format oci-archive \
    --output "${OUTPUT_DIR}/devcloud-postgresql-16.tar" \
    "${POSTGRES_IMAGE}"

(
    cd "${OUTPUT_DIR}"
    sha256sum ./*.tar > SHA256SUMS
)

echo "Offline controller images exported to ${OUTPUT_DIR}"
