#!/usr/bin/env bash
# Bootstrap the interactive DevCloud lifecycle installer on Rocky/RHEL 10.
set -Eeuo pipefail

log() {
    printf '[devcloud-setup] %s\n' "$*"
}

fail() {
    printf '[devcloud-setup] ERROR: %s\n' "$*" >&2
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

[[ "$(id -u)" -eq 0 ]] || fail "Run devcloud-setup as root."
[[ -r /etc/os-release ]] || fail "/etc/os-release is required."
# shellcheck disable=SC1091
source /etc/os-release

DISTRIBUTION_ID="${ID,,}"
MAJOR_VERSION="${VERSION_ID%%.*}"
ARCHITECTURE="$(uname -m)"
case "${DISTRIBUTION_ID}" in
    rocky|rhel) ;;
    *) fail "Only Rocky Linux 10 and RHEL 10 are supported; detected ${ID:-unknown}." ;;
esac
[[ "${MAJOR_VERSION}" == "10" ]] || fail \
    "Only Rocky/RHEL major version 10 is supported; detected ${VERSION_ID:-unknown}."
[[ "${ARCHITECTURE}" == "x86_64" ]] || fail \
    "Only x86_64 is supported; detected ${ARCHITECTURE}."
command -v dnf >/dev/null 2>&1 || fail "DNF is required."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROFILE="${DISTRIBUTION_ID}-${MAJOR_VERSION}-x86_64"
BOOTSTRAP_RPMS="${PROJECT_DIR}/offline/bootstrap-rpms/${PROFILE}"
SYSTEM_RPMS="${PROJECT_DIR}/offline/system-rpms/${PROFILE}"
OFFLINE_MANIFEST="${PROJECT_DIR}/offline/MANIFEST.json"
OFFLINE_BUNDLE=0

if [[ -f "${OFFLINE_MANIFEST}" ]]; then
    [[ -d "${SYSTEM_RPMS}" ]] || fail \
        "The offline bundle has no system RPM repository for ${PROFILE}."
    log "Offline bundle detected; verifying and installing from its bundled DNF repository..."
    bash "${PROJECT_DIR}/deploy/install_offline_system_packages.sh" "${PROJECT_DIR}"
    OFFLINE_BUNDLE=1
    export DEVCLOUD_OFFLINE_INSTALL=1
fi

install_subscription_manager() {
    command -v subscription-manager >/dev/null 2>&1 && return 0
    if [[ "${OFFLINE_BUNDLE}" -eq 1 ]]; then
        fail "The verified offline RPM transaction did not install subscription-manager."
    fi
    log "Installing subscription-manager..."
    if dnf install -y subscription-manager; then
        return 0
    fi
    if [[ ! -d "${BOOTSTRAP_RPMS}" && -d "${SYSTEM_RPMS}" ]]; then
        BOOTSTRAP_RPMS="${SYSTEM_RPMS}"
    fi
    [[ -d "${BOOTSTRAP_RPMS}" ]] || fail \
        "subscription-manager is missing, configured repositories could not install it, and no ${PROFILE} bootstrap RPM closure is bundled."
    mapfile -d '' rpms < <(find "${BOOTSTRAP_RPMS}" -maxdepth 1 -type f -name '*.rpm' -print0 | sort -z)
    [[ "${#rpms[@]}" -gt 0 ]] || fail "The subscription-manager bootstrap RPM directory is empty."
    DNF_DISABLE_REPOSITORIES="$(dnf_disable_repositories_option)"
    dnf "${DNF_DISABLE_REPOSITORIES}" install -y "${rpms[@]}"
}

install_subscription_manager

if [[ "${OFFLINE_BUNDLE}" -eq 1 ]] && ! subscription-manager identity >/dev/null 2>&1; then
    cat <<'EOF'

[devcloud-setup] subscription-manager was installed successfully.
[devcloud-setup] Register this VM with Satellite/Foreman now, for example with your
[devcloud-setup] organization and activation key, then rerun:

    bash deploy/devcloud-setup.sh

[devcloud-setup] All remaining operating-system packages will be installed from
[devcloud-setup] the repositories enabled by that registration.
EOF
    exit 0
fi
if [[ "${OFFLINE_BUNDLE}" -eq 1 ]]; then
    log "Satellite/Foreman registration detected; configured repositories will provide remaining system packages."
fi

if ! command -v python3 >/dev/null 2>&1; then
    log "Installing Python 3 from configured repositories..."
    dnf install -y python3 || fail \
        "Configured repositories could not install Python 3. Register this VM with Satellite/Foreman, confirm its repositories are enabled, and rerun setup."
fi

if [[ "${OFFLINE_BUNDLE}" -eq 1 ]]; then
    log "Verifying the complete offline artifact manifest..."
    python3 "${PROJECT_DIR}/deploy/package_offline.py" \
        --verify "${PROJECT_DIR}" \
        --check-runtime
fi

export PYTHONPATH="${PROJECT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
# Subprocesses that switch to the service user must not inherit an inaccessible
# working directory such as /root/devcloud-worker.
cd /
exec python3 -m app.installer "$@"
