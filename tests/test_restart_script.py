from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESTART_SCRIPT = PROJECT_ROOT / "deploy" / "restart.sh"


def test_restart_script_reloads_workers_and_is_health_checked():
    script = RESTART_SCRIPT.read_text(encoding="utf-8")

    assert 'systemctl_cmd kill --kill-who=main --signal=SIGHUP' in script
    assert "systemctl_cmd start --no-block" in script
    assert "DEVCLOUD_RELOAD_TIMEOUT_SECONDS" in script
    assert "DEVCLOUD_START_TIMEOUT_SECONDS" in script
    assert "http://127.0.0.1:8000/login" in script
    assert 'journalctl -u "${SERVICE_NAME}"' in script
    assert "systemctl_cmd stop" not in script


def test_restart_script_preserves_workspace_processes():
    script = RESTART_SCRIPT.read_text(encoding="utf-8")

    assert 'worker_pids "${OLD_MAIN_PID}"' in script
    assert "multiprocessing.resource_tracker" in script
    assert "podman rm" not in script
    assert "podman stop" not in script
    assert "pkill" not in script
    assert "killall" not in script


def test_service_supports_standard_systemd_reload():
    service = (PROJECT_ROOT / "deploy" / "devcloud.service").read_text(encoding="utf-8")

    assert "ExecReload=/bin/kill -HUP $MAINPID" in service


def test_installers_use_bounded_restart_helper():
    for relative_path in ("deploy/deploy.sh", "deploy/deploy_offline.sh"):
        installer = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert 'bash "${PROJECT_DIR}/deploy/restart.sh"' in installer
        assert "systemctl restart devcloud" not in installer
