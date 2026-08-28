import io
import hashlib
import json
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from app.installer.backup import (
    _postgres_cli_connection,
    create_backup,
    restore_backup,
)
from app.installer.engine import InstallerEngine
from app.installer.cli import (
    _verify_offline_release,
    _worker_config_from_environment,
)
from app.installer.models import (
    ControllerRuntime,
    DatabaseMode,
    DeploymentRole,
    InstallConfig,
    RegistryMode,
)
from app.installer.platform import CommandRunner, InstallerError
from app.installer.release import prepare_release
from app.installer.state import InstallationState
from app.installer.ui import InstallerUI
from deploy.build_release import build


def config(role: DeploymentRole) -> InstallConfig:
    return InstallConfig(
        role=role,
        admin_password="SecretPassword!",
        worker_name="worker-01",
        worker_id="worker-id",
        enrollment_token_file="/root/enrollment-token",
    )


def test_lifecycle_installer_imports_before_application_wheels_are_installed():
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import app.installer.cli"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_install_plans_share_one_role_aware_engine(tmp_path):
    runner = CommandRunner(dry_run=True)
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)

    controller = engine.build_install_plan(config(DeploymentRole.CONTROLLER))
    all_in_one = engine.build_install_plan(config(DeploymentRole.ALL_IN_ONE))
    worker = engine.build_install_plan(config(DeploymentRole.WORKER))

    assert "migrations" in [step.key for step in controller.steps]
    assert "images" not in [step.key for step in controller.steps]
    assert {"migrations", "images"} <= {step.key for step in all_in_one.steps}
    assert "migrations" not in [step.key for step in worker.steps]
    assert "images" in [step.key for step in worker.steps]


def test_all_in_one_dry_run_installs_controller_and_worker_services(tmp_path):
    runner = CommandRunner(dry_run=True)
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)
    candidate = config(DeploymentRole.ALL_IN_ONE)
    candidate.controller_runtime = ControllerRuntime.NATIVE
    engine.build_install_plan(candidate).execute()

    commands = [" ".join(command) for command in runner.commands]
    assert any("devcloud-controller.service" in command for command in commands)
    assert any("devcloud-worker.service" in command for command in commands)
    assert any("app.migrations upgrade" in command for command in commands)
    assert not (tmp_path / "etc" / "devcloud").exists()


def test_all_in_one_writes_one_ordinary_enrolled_worker(tmp_path):
    engine = InstallerEngine(filesystem_root=tmp_path, runner=CommandRunner())
    candidate = config(DeploymentRole.ALL_IN_ONE)
    candidate.worker_name = "local-worker"

    engine._write_configuration(candidate)

    controller = engine._read_env(tmp_path / "etc/devcloud/controller.env")
    worker = engine._read_env(tmp_path / "etc/devcloud/worker.env")
    assert worker["DEVCLOUD_CONTROLLER_URL"] == "http://127.0.0.1:8000"
    assert worker["DEVCLOUD_NODE_ID"] == controller["DEVCLOUD_BOOTSTRAP_WORKER_ID"]
    assert hashlib.sha256(worker["DEVCLOUD_NODE_TOKEN"].encode()).hexdigest() == (
        controller["DEVCLOUD_BOOTSTRAP_WORKER_TOKEN_HASH"]
    )


def test_worker_bootstrap_answers_can_cross_pre_python_shell_boundary(
    monkeypatch, tmp_path
):
    token_file = tmp_path / "token"
    token_file.write_text("worker-secret\n", encoding="utf-8")
    monkeypatch.setenv(
        "DEVCLOUD_INSTALL_CONTROLLER_URL", "https://controller.example"
    )
    monkeypatch.setenv("DEVCLOUD_INSTALL_WORKER_ID", "worker-42")
    monkeypatch.setenv("DEVCLOUD_INSTALL_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("DEVCLOUD_INSTALL_WORKER_NAME", "compute-42")

    result = _worker_config_from_environment(DeploymentRole.WORKER)

    assert result is not None
    assert result.controller_url == "https://controller.example"
    assert result.worker_id == "worker-42"
    assert result.worker_name == "compute-42"


def test_same_semver_sources_receive_distinct_immutable_release_ids(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, payload in ((first, "one"), (second, "two")):
        (root / "app").mkdir(parents=True)
        (root / "app/__init__.py").write_text(
            '__version__ = "3.0.0"\n', encoding="utf-8"
        )
        (root / "requirements.txt").write_text(payload, encoding="utf-8")

    first_engine = InstallerEngine(
        project_root=first,
        filesystem_root=tmp_path / "host-one",
        runner=CommandRunner(dry_run=True),
    )
    second_engine = InstallerEngine(
        project_root=second,
        filesystem_root=tmp_path / "host-two",
        runner=CommandRunner(dry_run=True),
    )

    assert first_engine.release_id.startswith("3.0.0-")
    assert first_engine.release_id != second_engine.release_id


def test_installation_state_never_persists_secrets(tmp_path):
    candidate = config(DeploymentRole.WORKER)
    candidate.database_url = "postgresql+asyncpg://user:secret@db/devcloud"
    state = InstallationState.create(
        candidate.role.value, "9.9.9", candidate.public_dict()
    )
    path = tmp_path / "install-state.json"
    state.save(path)
    raw = path.read_text(encoding="utf-8")

    assert "SecretPassword" not in raw
    assert "user:secret" not in raw
    assert "enrollment-token" not in raw
    assert InstallationState.load(path).role == "worker"


def test_release_zip_rejects_path_traversal(tmp_path):
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape", "bad")
    with pytest.raises(InstallerError, match="Unsafe release path"):
        with prepare_release(
            archive,
            runner=CommandRunner(dry_run=True),
            require_signature=False,
        ):
            pass


def test_unsigned_git_zip_can_be_prepared_after_explicit_opt_in(tmp_path):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("repo/app/__init__.py", '__version__ = "3.0.0"\n')
        output.writestr("repo/requirements.txt", "")
    with prepare_release(
        archive,
        runner=CommandRunner(dry_run=True),
        require_signature=False,
    ) as release:
        assert release.version == "3.0.0"
        assert release.signature_verified is False


def test_manifest_rejects_unlisted_release_payload(tmp_path):
    app_init = b'__version__ = "3.0.0"\n'
    archive = tmp_path / "release.zip"
    manifest = {
        "format": 1,
        "version": "3.0.0",
        "artifacts": [
            {
                "path": "app/__init__.py",
                "size": len(app_init),
                "sha256": hashlib.sha256(app_init).hexdigest(),
            }
        ],
    }
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("devcloud/app/__init__.py", app_init)
        output.writestr("devcloud/requirements.txt", "injected-package\n")
        output.writestr("devcloud/release.json", json.dumps(manifest))
    with pytest.raises(InstallerError, match="unlisted artifact"):
        with prepare_release(
            archive,
            runner=CommandRunner(dry_run=True),
            require_signature=False,
        ):
            pass


def test_offline_update_rejects_invalid_embedded_manifest(tmp_path):
    manifest = tmp_path / "offline/MANIFEST.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"bundle_format": 2}\n', encoding="utf-8")

    with pytest.raises(InstallerError, match="Offline release verification failed"):
        _verify_offline_release(tmp_path)


def test_bundled_postgresql_plan_is_role_scoped(tmp_path):
    runner = CommandRunner(dry_run=True)
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)
    candidate = config(DeploymentRole.CONTROLLER)
    candidate.database_mode = DatabaseMode.BUNDLED_POSTGRESQL
    candidate.controller_runtime = ControllerRuntime.NATIVE

    engine.build_install_plan(candidate).execute()

    commands = [" ".join(command) for command in runner.commands]
    assert any("postgresql-server" in command for command in commands)
    assert any("postgresql-setup --initdb" in command for command in commands)
    assert any(
        "runuser -u devcloud --" in command
        and "app.migrations upgrade" in command
        for command in commands
    )
    assert not any(" podman" in f" {command}" for command in commands)


def test_container_controller_uses_quadlet_without_native_postgresql(tmp_path):
    runner = CommandRunner(dry_run=True)
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)
    candidate = config(DeploymentRole.CONTROLLER)
    candidate.database_mode = DatabaseMode.BUNDLED_POSTGRESQL

    engine.build_install_plan(candidate).execute()

    commands = [" ".join(command) for command in runner.commands]
    assert any("podman" in command for command in commands)
    assert any("devcloud-controller.container" in command for command in commands)
    assert any("devcloud-postgresql.container" in command for command in commands)
    assert any(
        "podman pull quay.io/sclorg/postgresql-16-c10s:latest" in command
        for command in commands
    )
    assert any(
        "podman tag quay.io/sclorg/postgresql-16-c10s:latest "
        "localhost/devcloud-postgresql:16" in command
        for command in commands
    )
    assert any(
        "podman inspect --format {{.State.Health.Status}} devcloud-controller"
        in command
        for command in commands
    )
    assert not any("postgresql-setup --initdb" in command for command in commands)
    assert not any("postgresql-server" in command for command in commands)
    assert not any("app.migrations upgrade" in command for command in commands)


def test_container_quadlets_render_image_and_database_dependencies(tmp_path):
    runner = CommandRunner()
    runner.run = lambda command, **_kwargs: subprocess.CompletedProcess(
        command, 0, "", ""
    )
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)
    candidate = config(DeploymentRole.CONTROLLER)
    candidate.database_mode = DatabaseMode.BUNDLED_POSTGRESQL

    engine._install_services(candidate)

    unit = (
        tmp_path / "etc/containers/systemd/devcloud-controller.container"
    ).read_text(encoding="utf-8")
    assert "{{" not in unit
    assert f"Image=localhost/devcloud-controller:{engine.release_version}" in unit
    assert "Requires=devcloud-postgresql.service" in unit
    database_unit = (
        tmp_path / "etc/containers/systemd/devcloud-postgresql.container"
    ).read_text(encoding="utf-8")
    assert "Image=localhost/devcloud-postgresql:16" in database_unit


def test_old_install_state_defaults_to_native_controller_runtime():
    payload = config(DeploymentRole.CONTROLLER).public_dict()
    payload.pop("controller_runtime")
    restored = InstallConfig.from_dict(payload)
    assert restored.controller_runtime == ControllerRuntime.NATIVE


def test_offline_bundle_installs_required_packages_from_configured_repositories(
    tmp_path,
):
    source = tmp_path / "source"
    source.joinpath("app").mkdir(parents=True)
    source.joinpath("app/__init__.py").write_text(
        '__version__ = "3.0.0"\n',
        encoding="utf-8",
    )
    source.joinpath("offline/MANIFEST.json").parent.mkdir(parents=True)
    source.joinpath("offline/MANIFEST.json").write_text("{}\n", encoding="utf-8")
    rpm_dir = source / "offline/system-rpms/rocky-10-x86_64"
    rpm_dir.mkdir(parents=True)
    rpm = rpm_dir / "complete-closure.rpm"
    rpm.write_bytes(b"rpm")
    rpm_dir.joinpath("repodata").mkdir()
    rpm_dir.joinpath("repodata/repomd.xml").write_text(
        "<repomd/>\n",
        encoding="utf-8",
    )

    host = tmp_path / "host"
    host.joinpath("etc").mkdir(parents=True)
    host.joinpath("etc/os-release").write_text(
        'ID="rocky"\nVERSION_ID="10.2"\n',
        encoding="utf-8",
    )
    runner = CommandRunner(dry_run=True)
    engine = InstallerEngine(
        project_root=source,
        filesystem_root=host,
        runner=runner,
    )

    engine._install_packages(config(DeploymentRole.ALL_IN_ONE))

    assert len(runner.commands) == 1
    command = runner.commands[0]
    assert command[:3] == ["dnf", "install", "-y"]
    assert "podman" in command
    assert "subscription-manager" in command
    assert not any("repofrompath" in argument for argument in command)


def test_external_postgresql_url_is_normalized_for_async_controller(tmp_path):
    candidate = config(DeploymentRole.CONTROLLER)
    candidate.database_mode = DatabaseMode.EXTERNAL_POSTGRESQL
    candidate.database_url = "postgresql://devcloud:secret@db.example/devcloud"
    assert candidate.effective_database_url() == (
        "postgresql+asyncpg://devcloud:secret@db.example/devcloud"
    )

    runner = CommandRunner(dry_run=True)
    InstallerEngine(filesystem_root=tmp_path, runner=runner).build_install_plan(
        candidate
    ).execute()
    commands = [" ".join(command) for command in runner.commands]
    assert any("postgresql" in command for command in commands)


def test_installer_rejects_dangerous_managed_paths_and_root_service(tmp_path):
    engine = InstallerEngine(
        filesystem_root=tmp_path, runner=CommandRunner(dry_run=True)
    )
    dangerous = config(DeploymentRole.WORKER)
    dangerous.workspace_root = "/etc/devcloud-workspaces"
    with pytest.raises(InstallerError, match="supported managed roots"):
        engine.build_install_plan(dangerous)

    privileged = config(DeploymentRole.WORKER)
    privileged.service_user = "root"
    with pytest.raises(InstallerError, match="non-root"):
        engine.build_install_plan(privileged)


def test_installer_rejects_registry_urls_with_a_transport_scheme(tmp_path):
    engine = InstallerEngine(
        filesystem_root=tmp_path, runner=CommandRunner(dry_run=True)
    )
    candidate = config(DeploymentRole.CONTROLLER)
    candidate.registry_mode = RegistryMode.EXTERNAL
    candidate.registry_url = "https://registry.example.com/devcloud"

    with pytest.raises(InstallerError, match="OCI image prefix"):
        engine.build_install_plan(candidate)


def test_worker_plan_provisions_rootless_subordinate_ids(tmp_path):
    runner = CommandRunner(dry_run=True)
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)

    engine.build_install_plan(config(DeploymentRole.WORKER)).execute()

    commands = [" ".join(command) for command in runner.commands]
    assert any(
        "usermod --add-subuids 100000-165535 --add-subgids 100000-165535 devcloud"
        in command
        for command in commands
    )
    assert any("app.installer.verify_worker" in command for command in commands)


def test_worker_images_are_loaded_into_service_users_rootless_store(tmp_path):
    image_dir = tmp_path / "opt/devcloud/current/offline/images"
    image_dir.mkdir(parents=True)
    image_archive = image_dir / "workspace.tar"
    image_archive.touch()
    runner = CommandRunner(dry_run=True)
    calls = []
    original_run = runner.run

    def recording_run(command, **kwargs):
        calls.append((command, kwargs.get("cwd")))
        return original_run(command, **kwargs)

    runner.run = recording_run
    engine = InstallerEngine(filesystem_root=tmp_path, runner=runner)
    candidate = config(DeploymentRole.WORKER)
    candidate.preload_images = True

    engine._prepare_images(candidate)

    assert ["id", "-u", "devcloud"] in runner.commands
    assert [
        "runuser",
        "-u",
        "devcloud",
        "--",
        "env",
        "HOME=/var/lib/devcloud",
        "XDG_RUNTIME_DIR=/run/user/SERVICE_UID",
        "podman",
        "load",
        "-i",
        str(image_archive),
    ] in runner.commands
    assert any(
        command[-3:] == ["load", "-i", str(image_archive)]
        and cwd == tmp_path / "opt/devcloud/current"
        for command, cwd in calls
    )


def test_ui_collects_worker_connection_details():
    answers = iter(
        [
            "https://controller.example",
            "worker-id",
            "/root/token",
            "compute-01",
            "/srv/workspaces",
        ]
    )
    ui = InstallerUI(input_fn=lambda _prompt: next(answers), output=io.StringIO())
    result = ui.collect_install_config(DeploymentRole.WORKER)

    assert result.controller_url == "https://controller.example"
    assert result.worker_id == "worker-id"
    assert result.worker_name == "compute-01"
    assert result.preload_images is False


def test_sqlite_backup_is_verified_and_restorable(tmp_path):
    root = tmp_path / "host"
    database = root / "var/lib/devcloud/database/devcloud.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('before')")
        connection.commit()
    finally:
        connection.close()
    etc = root / "etc/devcloud"
    etc.mkdir(parents=True)
    etc.joinpath("controller.env").write_text(
        'DATABASE_URL="sqlite+aiosqlite:////var/lib/devcloud/database/devcloud.db"\n',
        encoding="utf-8",
    )
    state = root / "var/lib/devcloud/installer"
    state.mkdir(parents=True)
    state.joinpath("install-state.json").write_text("{}\n", encoding="utf-8")
    install = config(DeploymentRole.CONTROLLER)
    install.database_mode = DatabaseMode.SQLITE
    output = tmp_path / "backup.tar.gz"
    host_path = lambda value: root / str(Path(value)).lstrip("/\\")

    create_backup(
        config=install,
        version="1.0.0",
        output=output,
        host_path=host_path,
        runner=CommandRunner(),
        include_workspaces=False,
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute("DELETE FROM sample")
        connection.commit()
    finally:
        connection.close()
    restore_backup(
        config=install,
        archive=output,
        host_path=host_path,
        runner=CommandRunner(),
    )
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT value FROM sample").fetchone() == (
            "before",
        )
    finally:
        connection.close()


def test_container_postgresql_backup_runs_dump_inside_database_container(tmp_path):
    root = tmp_path / "host"
    etc = root / "etc/devcloud"
    etc.mkdir(parents=True)
    etc.joinpath("controller.env").write_text(
        'DATABASE_URL="postgresql+asyncpg://devcloud:secret@devcloud-postgresql/devcloud"\n',
        encoding="utf-8",
    )
    candidate = config(DeploymentRole.CONTROLLER)
    candidate.database_mode = DatabaseMode.BUNDLED_POSTGRESQL
    runner = CommandRunner()
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:2] == ["podman", "cp"]:
            Path(command[-1]).write_bytes(b"postgres-dump")
        return subprocess.CompletedProcess(command, 0, "", "")

    runner.run = fake_run
    output = tmp_path / "postgres-backup.tar.gz"
    create_backup(
        config=candidate,
        version="3.1.0",
        output=output,
        host_path=lambda value: root / str(Path(value)).lstrip("/\\"),
        runner=runner,
        include_workspaces=False,
    )

    assert any(
        command[:5]
        == [
            "podman",
            "exec",
            "devcloud-postgresql",
            "pg_dump",
            "--format=custom",
        ]
        for command in commands
    )
    assert output.is_file()


def test_backup_rejects_tampered_member(tmp_path):
    root = tmp_path / "archive-root" / "devcloud-backup"
    payload = root / "payload"
    payload.mkdir(parents=True)
    member = payload / "value.txt"
    member.write_text("tampered", encoding="utf-8")
    root.joinpath("backup.json").write_text(
        json.dumps(
            {
                "format": 1,
                "role": "controller",
                "database": "none",
                "members": [
                    {
                        "path": "payload/value.txt",
                        "size": member.stat().st_size,
                        "sha256": "0" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    archive = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(root, arcname="devcloud-backup")
    with pytest.raises(InstallerError, match="verification failed"):
        restore_backup(
            config=config(DeploymentRole.CONTROLLER),
            archive=archive,
            host_path=lambda value: tmp_path / str(Path(value)).lstrip("/\\"),
            runner=CommandRunner(),
        )


def test_backup_rejects_unlisted_restore_payload(tmp_path):
    root = tmp_path / "archive-root" / "devcloud-backup"
    payload = root / "payload"
    payload.mkdir(parents=True)
    payload.joinpath("unlisted.env").write_text("SECRET=value\n", encoding="utf-8")
    root.joinpath("backup.json").write_text(
        json.dumps(
            {
                "format": 2,
                "role": "controller",
                "database": "none",
                "members": [],
            }
        ),
        encoding="utf-8",
    )
    archive = tmp_path / "unlisted.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(root, arcname="devcloud-backup")

    with pytest.raises(InstallerError, match="unlisted member"):
        restore_backup(
            config=config(DeploymentRole.CONTROLLER),
            archive=archive,
            host_path=lambda value: tmp_path / str(Path(value)).lstrip("/\\"),
            runner=CommandRunner(),
        )


def test_postgresql_cli_url_hides_password_from_process_arguments():
    url, environment = _postgres_cli_connection(
        "postgresql+asyncpg://devcloud:s3cret@db.example/devcloud"
    )

    assert url == "postgresql://devcloud@db.example/devcloud"
    assert "s3cret" not in url
    assert environment == {"PGPASSWORD": "s3cret"}


def test_update_path_recovers_a_running_marker_after_reboot():
    unit = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "devcloud-update.path"
    ).read_text(encoding="utf-8")

    assert "pending.json" in unit
    assert "running.json" in unit


def test_release_builder_produces_a_manifest_verified_archive(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app").mkdir()
    (source / "app/__init__.py").write_text(
        '__version__ = "3.0.0"\n', encoding="utf-8"
    )
    (source / "requirements.txt").write_text("", encoding="utf-8")
    for command in (
        ["git", "init"],
        ["git", "config", "user.email", "release@test.invalid"],
        ["git", "config", "user.name", "Release Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "release fixture"],
    ):
        subprocess.run(command, cwd=source, check=True, capture_output=True)

    archive = build(source, tmp_path / "dist")

    with prepare_release(
        archive,
        runner=CommandRunner(dry_run=True),
        require_signature=False,
    ) as prepared:
        assert prepared.version == "3.0.0"
        assert prepared.manifest is not None
        assert prepared.manifest["source_commit"]
