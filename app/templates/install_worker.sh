#!/usr/bin/env bash
# Enroll and install one DevCloud worker using a short-lived controller ticket.
set -Eeuo pipefail
umask 077

log() {
    printf '[devcloud-worker-bootstrap] %s\n' "$*"
}

fail() {
    printf '[devcloud-worker-bootstrap] ERROR: %s\n' "$*" >&2
    exit 1
}

[[ "$(id -u)" -eq 0 ]] || fail \
    "Run through sudo: curl -fsSL <ticket-url> | sudo bash"

for required_command in basename curl hostname mktemp sha256sum stat tar; do
    command -v "${required_command}" >/dev/null 2>&1 || fail \
        "Required base command is missing: ${required_command}"
done

if ! command -v python3 >/dev/null 2>&1; then
    command -v dnf >/dev/null 2>&1 || fail "Python 3 and DNF are unavailable."
    log "Installing Python 3 from configured repositories..."
    dnf install -y python3 || fail "Configured repositories could not install Python 3."
fi

[[ ! -f /var/lib/devcloud/installer/install-state.json ]] || fail \
    "A managed DevCloud installation already exists; purge it or run update."

CONTROLLER_URL=__CONTROLLER_URL__
ENROLLMENT_URL=__ENROLLMENT_URL__
WORKER_NAME="${DEVCLOUD_WORKER_NAME:-}"
if [[ -z "${WORKER_NAME}" ]]; then
    [[ -r /dev/tty && -w /dev/tty ]] || fail \
        "Set DEVCLOUD_WORKER_NAME when running without a terminal."
    DEFAULT_WORKER_NAME="$(hostname -s)"
    printf 'Worker name [%s]: ' "${DEFAULT_WORKER_NAME}" >/dev/tty
    read -r entered_worker_name </dev/tty
    WORKER_NAME="${entered_worker_name:-${DEFAULT_WORKER_NAME}}"
fi
[[ "${WORKER_NAME}" =~ ^[a-zA-Z0-9_.-]{2,100}$ ]] || fail \
    "Worker name must be 2-100 letters, digits, dots, underscores, or hyphens."

TEMP_DIR="$(mktemp -d -t devcloud-worker-bootstrap.XXXXXX)"
cleanup() {
    rm -rf -- "${TEMP_DIR}"
}
trap cleanup EXIT

log "Enrolling ${WORKER_NAME} with the controller..."
ENROLLMENT_PAYLOAD="$(WORKER_NAME="${WORKER_NAME}" python3 -c \
    'import json, os; print(json.dumps({"name": os.environ["WORKER_NAME"]}))')"
ENROLLMENT_RESPONSE="${TEMP_DIR}/enrollment.json"
curl --fail-with-body --silent --show-error \
    --request POST \
    --header 'Content-Type: application/json' \
    --data "${ENROLLMENT_PAYLOAD}" \
    --output "${ENROLLMENT_RESPONSE}" \
    "${ENROLLMENT_URL}"

mapfile -t CREDENTIALS < <(python3 -c \
    'import json, sys; value=json.load(open(sys.argv[1], encoding="utf-8")); print(value["node_id"]); print(value["enrollment_token"]); print(value["controller_url"])' \
    "${ENROLLMENT_RESPONSE}")
[[ "${#CREDENTIALS[@]}" -eq 3 ]] || fail "Controller returned invalid enrollment data."
NODE_ID="${CREDENTIALS[0]}"
NODE_TOKEN="${CREDENTIALS[1]}"
CONTROLLER_URL="${CREDENTIALS[2]%/}"
[[ -n "${NODE_ID}" && -n "${NODE_TOKEN}" ]] || fail \
    "Controller returned empty worker credentials."

CURL_AUTH_CONFIG="${TEMP_DIR}/curl-auth.conf"
printf 'header = "Authorization: Bearer %s"\n' "${NODE_TOKEN}" > "${CURL_AUTH_CONFIG}"
chmod 0600 "${CURL_AUTH_CONFIG}"
RELEASE_METADATA="${TEMP_DIR}/release.json"
log "Resolving the controller's current platform release..."
curl --fail-with-body --silent --show-error \
    --config "${CURL_AUTH_CONFIG}" \
    --output "${RELEASE_METADATA}" \
    "${CONTROLLER_URL}/api/agent/releases/latest?node_id=${NODE_ID}"

mapfile -t RELEASE < <(python3 -c \
    'import json, sys; value=json.load(open(sys.argv[1], encoding="utf-8")); print(value["filename"]); print(value["url"]); print(value["sha256"]); print(value["size"])' \
    "${RELEASE_METADATA}")
[[ "${#RELEASE[@]}" -eq 4 ]] || fail "Controller returned invalid release metadata."
BUNDLE_FILENAME="${RELEASE[0]}"
BUNDLE_URL="${RELEASE[1]}"
EXPECTED_SHA256="${RELEASE[2]}"
EXPECTED_SIZE="${RELEASE[3]}"
[[ "${BUNDLE_FILENAME}" == "$(basename -- "${BUNDLE_FILENAME}")" ]] || fail \
    "Controller returned an unsafe release filename."
[[ "${BUNDLE_URL}" =~ ^https?://[^[:space:]]+$ ]] || fail \
    "Controller returned an invalid release URL."
[[ "${EXPECTED_SHA256}" =~ ^[0-9a-f]{64}$ ]] || fail \
    "Controller returned an invalid release checksum."
[[ "${EXPECTED_SIZE}" =~ ^[0-9]+$ ]] || fail \
    "Controller returned an invalid release size."

BUNDLE_PATH="${TEMP_DIR}/${BUNDLE_FILENAME}"
log "Downloading ${BUNDLE_FILENAME}..."
curl --fail-with-body --silent --show-error \
    --config "${CURL_AUTH_CONFIG}" \
    --output "${BUNDLE_PATH}" \
    "${BUNDLE_URL}"
[[ "$(stat -c '%s' "${BUNDLE_PATH}")" == "${EXPECTED_SIZE}" ]] || fail \
    "Downloaded release size does not match controller metadata."
printf '%s  %s\n' "${EXPECTED_SHA256}" "${BUNDLE_FILENAME}" \
    > "${TEMP_DIR}/${BUNDLE_FILENAME}.sha256"
(cd "${TEMP_DIR}" && sha256sum -c "${BUNDLE_FILENAME}.sha256")

RELEASE_DIRECTORY="$(python3 -c \
    'import sys, tarfile; archive=tarfile.open(sys.argv[1]); roots={name.split("/", 1)[0] for name in archive.getnames() if name}; print(next(iter(roots)) if len(roots) == 1 else "")' \
    "${BUNDLE_PATH}")"
[[ "${RELEASE_DIRECTORY}" =~ ^devcloud-[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail \
    "Platform release has an unexpected top-level directory."
tar -xf "${BUNDLE_PATH}" -C "${TEMP_DIR}"
RELEASE_ROOT="${TEMP_DIR}/${RELEASE_DIRECTORY}"
[[ -f "${RELEASE_ROOT}/deploy/devcloud-setup.sh" ]] || fail \
    "Platform release does not contain the managed installer."

TOKEN_FILE="${TEMP_DIR}/enrollment-token"
printf '%s\n' "${NODE_TOKEN}" > "${TOKEN_FILE}"
chmod 0600 "${TOKEN_FILE}"
export DEVCLOUD_INSTALL_CONTROLLER_URL="${CONTROLLER_URL}"
export DEVCLOUD_INSTALL_WORKER_ID="${NODE_ID}"
export DEVCLOUD_INSTALL_TOKEN_FILE="${TOKEN_FILE}"
export DEVCLOUD_INSTALL_WORKER_NAME="${WORKER_NAME}"
export DEVCLOUD_INSTALL_WORKSPACE_ROOT="${STORAGE_ROOT:-/var/lib/devcloud/workspaces}"
export DEVCLOUD_INSTALL_PRELOAD_IMAGES=false

log "Installing the verified worker release..."
bash "${RELEASE_ROOT}/deploy/devcloud-setup.sh" --yes install worker
log "Worker ${WORKER_NAME} enrolled and installed successfully."
