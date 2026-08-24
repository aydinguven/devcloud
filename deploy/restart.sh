#!/usr/bin/env bash
# Bounded DevCloud systemd restart that preserves Podman workspace containers.
set -Eeuo pipefail

SERVICE_NAME="${DEVCLOUD_SERVICE_NAME:-devcloud.service}"
STOP_TIMEOUT_SECONDS="${DEVCLOUD_STOP_TIMEOUT_SECONDS:-8}"
START_TIMEOUT_SECONDS="${DEVCLOUD_START_TIMEOUT_SECONDS:-25}"
HEALTH_URL="${DEVCLOUD_HEALTH_URL:-http://127.0.0.1:8000/login}"

log() {
    printf '[devcloud-restart] %s\n' "$*"
}

fail() {
    printf '[devcloud-restart] ERROR: %s\n' "$*" >&2
    exit 1
}

is_positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

is_positive_integer "${STOP_TIMEOUT_SECONDS}" ||
    fail "DEVCLOUD_STOP_TIMEOUT_SECONDS must be a positive integer."
is_positive_integer "${START_TIMEOUT_SECONDS}" ||
    fail "DEVCLOUD_START_TIMEOUT_SECONDS must be a positive integer."

if (( EUID == 0 )); then
    SUDO=()
else
    command -v sudo >/dev/null 2>&1 || fail "Run as root or install sudo."
    SUDO=(sudo)
fi

systemctl_cmd() {
    "${SUDO[@]}" systemctl "$@"
}

pid_is_alive() {
    local pid="$1"
    [[ "${pid}" =~ ^[1-9][0-9]*$ ]] &&
        "${SUDO[@]}" kill -0 "${pid}" 2>/dev/null
}

service_state() {
    systemctl_cmd show "${SERVICE_NAME}" --property=ActiveState --value 2>/dev/null ||
        printf 'unknown\n'
}

main_pid() {
    local pid
    pid="$(systemctl_cmd show "${SERVICE_NAME}" --property=MainPID --value 2>/dev/null ||
        printf '0')"
    if [[ "${pid}" =~ ^[1-9][0-9]*$ ]]; then
        printf '%s\n' "${pid}"
    else
        printf '0\n'
    fi
}

health_ready() {
    if command -v curl >/dev/null 2>&1; then
        curl --fail --silent --max-time 2 --output /dev/null "${HEALTH_URL}"
    else
        # systemd state remains a useful fallback on minimal offline hosts.
        return 0
    fi
}

OLD_MAIN_PID="$(main_pid)"
OLD_WORKER_PIDS=()

if pid_is_alive "${OLD_MAIN_PID}" && command -v pgrep >/dev/null 2>&1; then
    while IFS= read -r worker_pid; do
        if [[ "${worker_pid}" =~ ^[1-9][0-9]*$ ]]; then
            OLD_WORKER_PIDS+=("${worker_pid}")
        fi
    done < <(pgrep -P "${OLD_MAIN_PID}" || true)
fi

log "Stopping ${SERVICE_NAME} without blocking (main PID: ${OLD_MAIN_PID})..."
systemctl_cmd stop --no-block "${SERVICE_NAME}" || true

stop_deadline=$((SECONDS + STOP_TIMEOUT_SECONDS))
while (( SECONDS < stop_deadline )); do
    state="$(service_state)"
    if [[ "${state}" == "inactive" || "${state}" == "failed" ]]; then
        break
    fi
    sleep 0.25
done

state="$(service_state)"
if [[ "${state}" != "inactive" && "${state}" != "failed" ]]; then
    log "Graceful stop exceeded ${STOP_TIMEOUT_SECONDS}s; killing only the old service main process."
    systemctl_cmd kill --kill-who=main --signal=SIGKILL "${SERVICE_NAME}" || true
fi

lingering_workers=()
for worker_pid in "${OLD_WORKER_PIDS[@]}"; do
    if pid_is_alive "${worker_pid}"; then
        lingering_workers+=("${worker_pid}")
    fi
done

if (( ${#lingering_workers[@]} > 0 )); then
    log "Terminating ${#lingering_workers[@]} stale Uvicorn worker(s)..."
    "${SUDO[@]}" kill -TERM "${lingering_workers[@]}" 2>/dev/null || true
    worker_deadline=$((SECONDS + 2))
    while (( SECONDS < worker_deadline )); do
        any_alive=0
        for worker_pid in "${lingering_workers[@]}"; do
            if pid_is_alive "${worker_pid}"; then
                any_alive=1
                break
            fi
        done
        (( any_alive == 0 )) && break
        sleep 0.2
    done

    for worker_pid in "${lingering_workers[@]}"; do
        if pid_is_alive "${worker_pid}"; then
            "${SUDO[@]}" kill -KILL "${worker_pid}" 2>/dev/null || true
        fi
    done
fi

systemctl_cmd reset-failed "${SERVICE_NAME}" >/dev/null 2>&1 || true
log "Starting ${SERVICE_NAME} without blocking..."
systemctl_cmd start --no-block "${SERVICE_NAME}"

start_deadline=$((SECONDS + START_TIMEOUT_SECONDS))
while (( SECONDS < start_deadline )); do
    state="$(service_state)"
    new_main_pid="$(main_pid)"

    if [[ "${state}" == "failed" ]]; then
        break
    fi

    if [[ "${state}" == "active" && "${new_main_pid}" != "0" ]] && health_ready; then
        log "DevCloud is healthy (main PID: ${new_main_pid}, URL: ${HEALTH_URL})."
        exit 0
    fi
    sleep 0.5
done

fail_state="$(service_state)"
printf '[devcloud-restart] Service did not become healthy (state: %s).\n' "${fail_state}" >&2
systemctl_cmd status "${SERVICE_NAME}" --no-pager --full || true
"${SUDO[@]}" journalctl -u "${SERVICE_NAME}" -n 40 --no-pager || true
exit 1
