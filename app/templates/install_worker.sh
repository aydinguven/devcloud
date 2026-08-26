#!/usr/bin/env bash
# Download, verify, and install the current DevCloud CPU-worker bundle.
set -Eeuo pipefail

log() {
    printf '[devcloud-worker-bootstrap] %s\n' "$*"
}

fail() {
    printf '[devcloud-worker-bootstrap] ERROR: %s\n' "$*" >&2
    exit 1
}

[[ "$(id -u)" -eq 0 ]] || fail \
    "Run through sudo: curl -fsSL <master>/download/install-worker.sh | sudo bash"

for required_command in curl sha256sum tar mktemp; do
    command -v "${required_command}" >/dev/null 2>&1 || fail \
        "Required base command is missing: ${required_command}"
done

DEFAULT_MASTER_URL=__MASTER_URL__
BUNDLE_URL=__BUNDLE_URL__
CHECKSUM_URL=__CHECKSUM_URL__
BUNDLE_FILENAME=__BUNDLE_FILENAME__
CHECKSUM_FILENAME=__CHECKSUM_FILENAME__
INSTALL_DIR="${DEVCLOUD_WORKER_INSTALL_DIR:-/opt/devcloud-worker}"
WORKER_ENV_FILE="/etc/devcloud/worker.env"

[[ "${INSTALL_DIR}" == /* && "${INSTALL_DIR}" != "/" ]] || fail \
    "DEVCLOUD_WORKER_INSTALL_DIR must be a safe absolute path."
[[ ! -e "${INSTALL_DIR}" ]] || fail \
    "${INSTALL_DIR} already exists. This bootstrap command is for a new installation."

TEMP_DIR="$(mktemp -d -t devcloud-worker-bootstrap.XXXXXX)"
cleanup() {
    rm -rf -- "${TEMP_DIR}"
}
trap cleanup EXIT

log "Downloading ${BUNDLE_FILENAME} from the Master..."
curl --fail --location --silent --show-error \
    --output "${TEMP_DIR}/${BUNDLE_FILENAME}" "${BUNDLE_URL}"
curl --fail --location --silent --show-error \
    --output "${TEMP_DIR}/${CHECKSUM_FILENAME}" "${CHECKSUM_URL}"

log "Verifying the published bundle checksum..."
(cd "${TEMP_DIR}" && sha256sum -c "${CHECKSUM_FILENAME}")

install -d -m 0755 "$(dirname "${INSTALL_DIR}")"
tar -xf "${TEMP_DIR}/${BUNDLE_FILENAME}" -C "${TEMP_DIR}"
[[ -d "${TEMP_DIR}/devcloud-worker" ]] || fail \
    "The archive does not contain the expected devcloud-worker directory."
mv "${TEMP_DIR}/devcloud-worker" "${INSTALL_DIR}"

if [[ ! -f "${WORKER_ENV_FILE}" ]]; then
    [[ -r /dev/tty && -w /dev/tty ]] || fail \
        "Create ${WORKER_ENV_FILE} first when running without an interactive terminal."
    MASTER_URL="${DEVCLOUD_MASTER_URL:-${DEFAULT_MASTER_URL}}"
    printf 'Master URL [%s]: ' "${MASTER_URL}" >/dev/tty
    read -r entered_master_url </dev/tty
    MASTER_URL="${entered_master_url:-${MASTER_URL}}"
    printf 'Worker node ID: ' >/dev/tty
    read -r NODE_ID </dev/tty
    printf 'Worker node token: ' >/dev/tty
    read -r -s NODE_TOKEN </dev/tty
    printf '\n' >/dev/tty

    [[ "${MASTER_URL}" =~ ^https?://[^[:space:]]+$ ]] || fail \
        "Master URL must start with http:// or https:// and contain no spaces."
    [[ -n "${NODE_ID}" && "${NODE_ID}" != *[[:space:]]* ]] || fail \
        "Worker node ID cannot be empty or contain spaces."
    [[ -n "${NODE_TOKEN}" && "${NODE_TOKEN}" != *[[:space:]]* ]] || fail \
        "Worker node token cannot be empty or contain spaces."

    install -d -m 0755 /etc/devcloud
    umask 077
    {
        printf 'DEVCLOUD_MASTER_URL=%s\n' "${MASTER_URL}"
        printf 'DEVCLOUD_NODE_ID=%s\n' "${NODE_ID}"
        printf 'DEVCLOUD_NODE_TOKEN=%s\n' "${NODE_TOKEN}"
    } > "${WORKER_ENV_FILE}"
    chmod 0600 "${WORKER_ENV_FILE}"
else
    log "Using existing ${WORKER_ENV_FILE}."
fi

log "Starting the verified offline worker installer..."
bash "${INSTALL_DIR}/deploy/deploy_worker_offline.sh"
log "Worker installation completed."
