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


def test_service_uses_runtime_directory_for_configured_user():
    service = (PROJECT_ROOT / "deploy" / "devcloud.service").read_text(encoding="utf-8")

    assert "Environment=XDG_RUNTIME_DIR=/run/user/%U" in service
    assert "ExecStartPre=+/usr/bin/install -d -o %U -g %G -m 0700 /run/user/%U" in service
    assert "Environment=XDG_RUNTIME_DIR=/run/user/0" not in service


def test_installers_use_bounded_restart_helper():
    for relative_path in ("deploy/deploy.sh", "deploy/deploy_offline.sh"):
        installer = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert 'bash "${PROJECT_DIR}/deploy/restart.sh"' in installer
        assert "systemctl restart devcloud" not in installer


def test_installers_keep_distribution_specific_python_packages():
    installer = (PROJECT_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    apt_install = next(line for line in installer.splitlines() if "apt-get install" in line)
    dnf_install = next(line for line in installer.splitlines() if "dnf install" in line)

    assert "python3-venv" in apt_install
    assert "python3-venv" not in dnf_install


def test_offline_installer_requires_documented_sudo_and_does_not_alias_runtimes():
    installer = (PROJECT_ROOT / "deploy" / "deploy_offline.sh").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (PROJECT_ROOT / "AIRGAP.md").read_text(encoding="utf-8")

    assert "sudo bash deploy/deploy_offline.sh" in readme
    assert "sudo bash deploy/deploy_offline.sh" in runbook
    assert "ln -sf /usr/bin/runc /usr/bin/crun" not in installer


def test_platform_updater_is_atomic_and_uses_bounded_restart_helper():
    script = (PROJECT_ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    assert "flock -n 9" in script
    assert "sudo -n true" in script
    assert 'TARGET_BRANCH="${DEVCLOUD_UPDATE_BRANCH:-main}"' in script
    assert 'git fetch origin "${TARGET_BRANCH}"' in script
    assert 'git switch "${TARGET_BRANCH}"' in script
    assert 'git merge --ff-only "origin/${TARGET_BRANCH}"' in script
    assert "git status --porcelain --untracked-files=no" in script
    assert "systemctl show --property User --value devcloud" in script
    assert ".venv/bin/python -m pip install" in script
    assert "platform-update-restart.log" in script
    assert '"${PROJECT_DIR}/deploy/restart.sh"' in script
    assert "systemctl restart devcloud" not in script


def test_platform_update_api_streams_script_and_checks_exit_code():
    routes = (PROJECT_ROOT / "app/routes/admin_routes.py").read_text(encoding="utf-8")

    assert 'project_dir / "deploy" / "update.sh"' in routes
    assert "stderr=asyncio.subprocess.STDOUT" in routes
    assert "if return_code != 0" in routes
    assert "systemctl restart devcloud" not in routes
