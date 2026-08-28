#!/usr/bin/env bash
# Download, verify, and install the current DevCloud worker release.
set -Eeuo pipefail

log() {
    printf '[devcloud-worker-bootstrap] %s\n' "$*"
}

fail() {
    printf '[devcloud-worker-bootstrap] ERROR: %s\n' "$*" >&2
    exit 1
}

[[ "$(id -u)" -eq 0 ]] || fail \
    "Run through sudo: curl -fsSL <controller>/download/install-worker.sh | sudo bash"

for required_command in curl sha256sum tar mktemp; do
    command -v "${required_command}" >/dev/null 2>&1 || fail \
        "Required base command is missing: ${required_command}"
done

DEFAULT_CONTROLLER_URL=__MASTER_URL__
BUNDLE_URL=__BUNDLE_URL__
CHECKSUM_URL=__CHECKSUM_URL__
BUNDLE_FILENAME=__BUNDLE_FILENAME__
CHECKSUM_FILENAME=__CHECKSUM_FILENAME__
WORKER_ENV_FILE="/etc/devcloud/worker.env"

[[ ! -f /var/lib/devcloud/installer/install-state.json ]] || fail \
    "A managed DevCloud installation already exists; run devcloud-setup update or repair."

TEMP_DIR="$(mktemp -d -t devcloud-worker-bootstrap.XXXXXX)"
cleanup() {
    rm -rf -- "${TEMP_DIR}"
}
trap cleanup EXIT

log "Downloading ${BUNDLE_FILENAME} from the controller..."
curl --fail --location --silent --show-error \
    --output "${TEMP_DIR}/${BUNDLE_FILENAME}" "${BUNDLE_URL}"
curl --fail --location --silent --show-error \
    --output "${TEMP_DIR}/${CHECKSUM_FILENAME}" "${CHECKSUM_URL}"

log "Verifying the published bundle checksum..."
(cd "${TEMP_DIR}" && sha256sum -c "${CHECKSUM_FILENAME}")
tar -xf "${TEMP_DIR}/${BUNDLE_FILENAME}" -C "${TEMP_DIR}"
[[ -d "${TEMP_DIR}/devcloud-worker" ]] || fail \
    "The archive does not contain the expected devcloud-worker directory."

CONTROLLER_URL="${DEVCLOUD_CONTROLLER_URL:-${DEVCLOUD_MASTER_URL:-${DEFAULT_CONTROLLER_URL}}}"
NODE_ID="${DEVCLOUD_NODE_ID:-}"
NODE_TOKEN="${DEVCLOUD_NODE_TOKEN:-}"

if [[ -f "${WORKER_ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${WORKER_ENV_FILE}"
    CONTROLLER_URL="${DEVCLOUD_CONTROLLER_URL:-${DEVCLOUD_MASTER_URL:-${CONTROLLER_URL}}}"
    NODE_ID="${DEVCLOUD_NODE_ID:-${NODE_ID}}"
    NODE_TOKEN="${DEVCLOUD_NODE_TOKEN:-${NODE_TOKEN}}"
fi

if [[ -z "${NODE_ID}" || -z "${NODE_TOKEN}" ]]; then
    [[ -r /dev/tty && -w /dev/tty ]] || fail \
        "Set DEVCLOUD_NODE_ID and DEVCLOUD_NODE_TOKEN when running without a terminal."
    printf 'Controller URL [%s]: ' "${CONTROLLER_URL}" >/dev/tty
    read -r entered_controller_url </dev/tty
    CONTROLLER_URL="${entered_controller_url:-${CONTROLLER_URL}}"
    if [[ -z "${NODE_ID}" ]]; then
        printf 'Worker node ID: ' >/dev/tty
        read -r NODE_ID </dev/tty
    fi
    if [[ -z "${NODE_TOKEN}" ]]; then
        printf 'Worker enrollment token: ' >/dev/tty
        read -r -s NODE_TOKEN </dev/tty
        printf '\n' >/dev/tty
    fi
fi

[[ "${CONTROLLER_URL}" =~ ^https?://[^[:space:]]+$ ]] || fail \
    "Controller URL must start with http:// or https:// and contain no spaces."
[[ -n "${NODE_ID}" && "${NODE_ID}" != *[[:space:]]* ]] || fail \
    "Worker node ID cannot be empty or contain spaces."
[[ -n "${NODE_TOKEN}" && "${NODE_TOKEN}" != *[[:space:]]* ]] || fail \
    "Worker node token cannot be empty or contain spaces."

TOKEN_FILE="${TEMP_DIR}/enrollment-token"
printf '%s\n' "${NODE_TOKEN}" > "${TOKEN_FILE}"
chmod 0600 "${TOKEN_FILE}"
export DEVCLOUD_INSTALL_CONTROLLER_URL="${CONTROLLER_URL}"
export DEVCLOUD_INSTALL_WORKER_ID="${NODE_ID}"
export DEVCLOUD_INSTALL_TOKEN_FILE="${TOKEN_FILE}"
export DEVCLOUD_INSTALL_WORKER_NAME="${DEVCLOUD_WORKER_NAME:-$(hostname)}"
export DEVCLOUD_INSTALL_WORKSPACE_ROOT="${STORAGE_ROOT:-/var/lib/devcloud/workspaces}"
export DEVCLOUD_INSTALL_PRELOAD_IMAGES=true

log "Starting the verified worker installer..."
bash "${TEMP_DIR}/devcloud-worker/deploy/devcloud-setup.sh" \
    --yes install worker
log "Worker installation completed."
