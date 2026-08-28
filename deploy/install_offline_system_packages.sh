#!/usr/bin/env bash
# Install the Rocky/RHEL bootstrap RPM closure embedded in an air-gap bundle.
set -Eeuo pipefail

log() {
    printf '[devcloud-system-rpms] %s\n' "$*"
}

fail() {
    printf '[devcloud-system-rpms] ERROR: %s\n' "$*" >&2
    exit 1
}

dnf_disable_repositories_option() {
    local help_text
    help_text="$(dnf --help 2>&1)" || fail "Could not inspect the installed DNF command."
    if [[ "${help_text}" == *"--disable-repo"* ]]; then
        printf '%s\n' "--disable-repo=*"
    elif [[ "${help_text}" == *"--disablerepo"* ]]; then
        printf '%s\n' "--disablerepo=*"
    else
        fail "The installed DNF command has no supported repository-disable option."
    fi
}

[[ "$(id -u)" -eq 0 ]] || fail "Run this installer as root."

PROJECT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RPM_ROOT="${PROJECT_DIR}/offline/system-rpms"

if [[ ! -d "${RPM_ROOT}" ]]; then
    log "This legacy bundle contains no system RPM set; using installed prerequisites."
    exit 0
fi

[[ -r /etc/os-release ]] || fail "/etc/os-release is required."
# shellcheck disable=SC1091
source /etc/os-release
DISTRIBUTION_ID="${ID,,}"
MAJOR_VERSION="${VERSION_ID%%.*}"
ARCHITECTURE="$(uname -m)"

case "${DISTRIBUTION_ID}" in
    rocky|rhel) ;;
    *) fail "Only Rocky Linux 10 and RHEL 10 RPM profiles are supported; detected ${ID:-unknown}." ;;
esac
[[ "${MAJOR_VERSION}" == "10" ]] || fail \
    "This bundle supports major version 10; detected ${VERSION_ID:-unknown}."
[[ "${ARCHITECTURE}" == "x86_64" ]] || fail \
    "This bundle supports x86_64; detected ${ARCHITECTURE}."

PROFILE="${DISTRIBUTION_ID}-${MAJOR_VERSION}-x86_64"
RPM_DIR="${RPM_ROOT}/${PROFILE}"
[[ -d "${RPM_DIR}" ]] || fail \
    "No RPM profile for ${PROFILE}. Use a bundle built on the same distribution."
command -v dnf >/dev/null 2>&1 || fail "DNF is required to install the bundled RPM transaction."
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required to verify bundled RPMs."
[[ -f "${RPM_ROOT}/SHA256SUMS" ]] || fail "The system RPM checksum index is missing."

log "Verifying bundled system RPM checksums..."
(cd "${RPM_ROOT}" && sha256sum -c SHA256SUMS)

mapfile -d '' RPM_FILES < <(find "${RPM_DIR}" -maxdepth 1 -type f -name '*.rpm' -print0 | sort -z)
[[ "${#RPM_FILES[@]}" -gt 0 ]] || fail "The ${PROFILE} RPM profile is empty."

log "Installing ${#RPM_FILES[@]} verified RPMs from ${PROFILE} without network repositories..."
DNF_DISABLE_REPOSITORIES="$(dnf_disable_repositories_option)"
dnf "${DNF_DISABLE_REPOSITORIES}" install -y "${RPM_FILES[@]}"

command -v python3 >/dev/null 2>&1 || fail "Bundled RPM installation did not provide python3."
command -v podman >/dev/null 2>&1 || fail "Bundled RPM installation did not provide Podman."
if ! command -v crun >/dev/null 2>&1 && ! command -v runc >/dev/null 2>&1; then
    fail "Bundled RPM installation did not provide an OCI runtime."
fi
command -v subscription-manager >/dev/null 2>&1 || fail \
    "Bundled RPM installation did not provide subscription-manager."

log "Offline operating-system prerequisites are ready."
