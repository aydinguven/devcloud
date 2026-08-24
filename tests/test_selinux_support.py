from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SELINUX_SCRIPT = PROJECT_ROOT / "deploy" / "configure_selinux.sh"


def test_selinux_helper_uses_persistent_container_labels():
    script = SELINUX_SCRIPT.read_text(encoding="utf-8")

    assert "semanage fcontext" in script
    assert "container_file_t" in script
    assert "restorecon" in script
    assert "policycoreutils-python-utils" in script
    assert "setenforce 0" not in script
    assert "label=disable" not in script


def test_selinux_helper_preserves_running_private_labels():
    script = SELINUX_SCRIPT.read_text(encoding="utf-8")

    assert "RUNNING_CONTAINERS" in script
    assert 'restorecon -Fv "${WORKSPACES_DIR}"' in script
    assert 'restorecon -RFv "${WORKSPACES_DIR}"' in script
    assert "Podman's :Z" in script


def test_installers_call_the_selinux_helper():
    for relative_path in ("deploy/deploy.sh", "deploy/deploy_offline.sh"):
        installer = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert 'deploy/configure_selinux.sh' in installer


def test_selinux_enablement_guide_requires_permissive_relabel_first():
    guide = (PROJECT_ROOT / "SELINUX.md").read_text(encoding="utf-8")

    assert "SELINUX=permissive" in guide
    assert "fixfiles -F onboot" in guide
    assert "sudo setenforce 1" in guide
    assert "Do not switch directly from Disabled to Enforcing" in guide
