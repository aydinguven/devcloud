from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from app.installer.models import DatabaseMode, DeploymentRole, InstallConfig, RegistryMode
from app.installer.backup import create_backup, restore_backup
from app.installer.platform import (
    CommandRunner,
    InstallerError,
    detect_platform,
    validate_target,
)
from app.installer.state import InstallationState


@dataclass(slots=True)
class PlanStep:
    key: str
    description: str
    apply: Callable[[], None]


class InstallPlan:
    def __init__(
        self,
        title: str,
        steps: list[PlanStep],
        *,
        on_failure: Callable[[], None] | None = None,
    ):
        self.title = title
        self.steps = steps
        self.on_failure = on_failure

    @property
    def descriptions(self) -> list[str]:
        return [step.description for step in self.steps]

    def execute(self) -> None:
        try:
            for step in self.steps:
                step.apply()
        except Exception:
            if self.on_failure is not None:
                self.on_failure()
            raise


class InstallerEngine:
    """Build and apply repeatable installation plans.

    The engine deliberately separates plan creation from execution so the
    interactive UI, answer-file automation, and tests all exercise the same
    actions.
    """

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        filesystem_root: Path = Path("/"),
        runner: CommandRunner | None = None,
    ):
        self.project_root = (
            project_root or Path(__file__).resolve().parents[2]
        ).resolve()
        self.filesystem_root = filesystem_root.resolve()
        self.runner = runner or CommandRunner()
        self.release_version = self._source_version()
        self.release_id = self._source_release_id()
        self.previous_release: Path | None = None
        self.migration_applied = False

    def _source_version(self) -> str:
        version_file = self.project_root / "app" / "__init__.py"
        if not version_file.is_file():
            raise InstallerError(f"Release version file is missing: {version_file}")
        match = re.search(
            r'__version__\s*=\s*["\']([^"\']+)["\']',
            version_file.read_text(encoding="utf-8"),
        )
        if not match:
            raise InstallerError("Release does not declare app.__version__")
        return match.group(1)

    def _source_release_id(self) -> str:
        manifest = self.project_root / "release.json"
        if manifest.is_file():
            try:
                import json

                source_commit = str(
                    json.loads(manifest.read_text(encoding="utf-8")).get(
                        "source_commit", ""
                    )
                )
            except (OSError, ValueError):
                source_commit = ""
            if re.fullmatch(r"[0-9a-fA-F]{7,64}", source_commit):
                return f"{self.release_version}-{source_commit[:12].lower()}"

        digest = hashlib.sha256()
        ignored = {
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "data",
            "dist",
            "offline",
        }
        files: list[Path] = []
        for directory, directory_names, file_names in os.walk(
            self.project_root, followlinks=False
        ):
            directory_names[:] = sorted(
                name for name in directory_names if name not in ignored
            )
            root = Path(directory)
            for name in sorted(file_names):
                path = root / name
                if path.is_file() and not path.is_symlink():
                    files.append(path)
        files.sort()
        for path in files:
            relative = path.relative_to(self.project_root).as_posix().encode()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        return f"{self.release_version}-{digest.hexdigest()[:12]}"

    def host_path(self, value: str | Path) -> Path:
        raw = str(value)
        path = Path(raw)
        if self.filesystem_root == Path("/"):
            return path
        if not PurePosixPath(raw.replace("\\", "/")).is_absolute():
            raise InstallerError(f"Expected an absolute installation path: {path}")
        return self.filesystem_root / raw.lstrip("/\\")

    def state_path(self, config: InstallConfig) -> Path:
        return self.host_path(config.state_root) / "install-state.json"

    def current_state(self, state_root: str = "/var/lib/devcloud/installer") -> InstallationState | None:
        return InstallationState.load(self.host_path(state_root) / "install-state.json")

    def preflight(self) -> None:
        if self.runner.dry_run:
            return
        if os.name != "posix":
            raise InstallerError("DevCloud installation is supported only on Linux hosts.")
        if os.geteuid() != 0:
            raise InstallerError("Run devcloud-setup as root.")
        host = detect_platform(self.host_path("/etc/os-release"))
        validate_target(host)
        if not self.runner.exists("dnf"):
            raise InstallerError("DNF is required on Rocky Linux 10 and RHEL 10.")
        if not self.runner.exists("systemctl"):
            raise InstallerError("systemd is required.")

    def build_install_plan(self, config: InstallConfig) -> InstallPlan:
        self._validate_config(config)
        steps = [
            PlanStep("preflight", "Validate Rocky/RHEL 10, root access, DNF, and systemd", self.preflight),
            PlanStep(
                "packages",
                "Install required operating-system packages from configured DNF repositories",
                lambda: self._install_packages(config),
            ),
            PlanStep(
                "service-user",
                f"Create or validate the dedicated {config.service_user} service account",
                lambda: self._ensure_service_user(config),
            ),
            PlanStep(
                "directories",
                "Create application, configuration, state, release, and workspace directories",
                lambda: self._ensure_directories(config),
            ),
            PlanStep(
                "database",
                "Initialize the selected database service and database role",
                lambda: self._configure_database(config),
            ),
            PlanStep(
                "release",
                f"Install DevCloud {self.release_version} as an immutable side-by-side release",
                lambda: self._install_release(config),
            ),
            PlanStep(
                "python",
                "Create the release virtual environment and install verified Python dependencies",
                lambda: self._install_python_dependencies(config),
            ),
            PlanStep(
                "configuration",
                "Write role configuration and protected credentials",
                lambda: self._write_configuration(config),
            ),
            PlanStep(
                "services",
                "Install and enable the selected systemd services",
                lambda: self._install_services(config),
            ),
        ]
        if config.installs_worker:
            steps.extend(
                [
                    PlanStep(
                        "selinux",
                        "Configure persistent workspace storage and SELinux labels",
                        lambda: self._configure_worker_storage(config),
                    ),
                    PlanStep(
                        "images",
                        "Load or build the selected workspace images",
                        lambda: self._prepare_images(config),
                    ),
                ]
            )
        if config.installs_controller:
            steps.extend(
                [
                    PlanStep(
                        "migrations",
                        "Apply explicit database migrations before starting the controller",
                        lambda: self._run_migrations(config),
                    ),
                    PlanStep(
                        "ingress",
                        "Install the controller ingress service and initial HTTP configuration",
                        lambda: self._install_ingress(config),
                    ),
                ]
            )
        steps.extend(
            [
                PlanStep(
                    "start",
                    "Start services and wait for systemd activation",
                    lambda: self._start_services(config),
                ),
                PlanStep(
                    "state",
                    "Record the completed installation without persisting secrets",
                    lambda: self._save_state(config),
                ),
            ]
        )
        return InstallPlan(f"Install DevCloud {config.role.value}", steps)

    def build_repair_plan(self, config: InstallConfig) -> InstallPlan:
        self._validate_config(config)
        return InstallPlan(
            f"Repair DevCloud {config.role.value}",
            [
                PlanStep("preflight", "Re-run host preflight checks", self.preflight),
                PlanStep(
                    "directories",
                    "Repair directory ownership and permissions",
                    lambda: self._ensure_directories(config),
                ),
                PlanStep(
                    "configuration",
                    "Re-render configuration while preserving existing secrets",
                    lambda: self._write_configuration(config),
                ),
                PlanStep(
                    "services",
                    "Reinstall systemd service definitions",
                    lambda: self._install_services(config),
                ),
                PlanStep(
                    "selinux",
                    "Reapply workspace SELinux labels when a worker role is installed",
                    lambda: self._configure_worker_storage(config)
                    if config.installs_worker
                    else None,
                ),
                PlanStep(
                    "restart",
                    "Restart installed services and verify systemd activation",
                    lambda: self._restart_services(config),
                ),
                PlanStep(
                    "state",
                    "Refresh installation state",
                    lambda: self._save_state(config),
                ),
            ],
        )

    def build_update_plan(self, config: InstallConfig) -> InstallPlan:
        self._validate_config(config)
        return InstallPlan(
            f"Update DevCloud to {self.release_version}",
            [
                PlanStep("preflight", "Validate the target host and release source", self.preflight),
                PlanStep(
                    "release",
                    f"Stage immutable release {self.release_version} without removing the current release",
                    lambda: self._install_release(config),
                ),
                PlanStep(
                    "python",
                    "Install bundled Python dependencies or reuse an identical existing environment",
                    lambda: self._install_python_dependencies(config),
                ),
                PlanStep(
                    "configuration",
                    "Re-render configuration while preserving installed secrets",
                    lambda: self._write_configuration(config),
                ),
                PlanStep(
                    "services",
                    "Install the release systemd unit definitions",
                    lambda: self._install_services(config),
                ),
                PlanStep(
                    "migrations",
                    "Apply explicit database migrations for the new release",
                    lambda: self._run_migrations(config),
                ),
                PlanStep(
                    "restart",
                    "Restart installed services and verify systemd activation",
                    lambda: self._restart_services(config),
                ),
                PlanStep(
                    "state",
                    "Record the successfully applied release",
                    lambda: self._save_state(config),
                ),
            ],
            on_failure=lambda: self._rollback_release(config),
        )

    def build_uninstall_plan(
        self,
        config: InstallConfig,
        *,
        purge_data: bool = False,
    ) -> InstallPlan:
        steps = [
            PlanStep(
                "stop",
                "Stop and disable DevCloud services",
                lambda: self._stop_services(config),
            ),
            PlanStep(
                "units",
                "Remove DevCloud systemd unit files",
                lambda: self._remove_service_units(config),
            ),
            PlanStep(
                "software",
                "Remove the active application link while preserving configuration and data",
                lambda: self._remove_active_release(config),
            ),
        ]
        if purge_data:
            steps.append(
                PlanStep(
                    "purge",
                    "Permanently remove DevCloud configuration, releases, state, and workspace data",
                    lambda: self._purge_data(config),
                )
            )
        return InstallPlan("Uninstall DevCloud", steps)

    def build_backup_plan(
        self,
        config: InstallConfig,
        *,
        output: Path,
        include_workspaces: bool = False,
    ) -> InstallPlan:
        return InstallPlan(
            "Back up DevCloud",
            [
                PlanStep(
                    "backup",
                    (
                        "Create a verified encrypted-permissions backup of "
                        "configuration, database, and workspace data"
                        if include_workspaces
                        else "Create a verified backup of configuration and database"
                    ),
                    lambda: create_backup(
                        config=config,
                        version=self.release_version,
                        output=output,
                        host_path=self.host_path,
                        runner=self.runner,
                        include_workspaces=include_workspaces,
                    ),
                )
            ],
        )

    def build_restore_plan(
        self,
        config: InstallConfig,
        *,
        archive: Path,
    ) -> InstallPlan:
        return InstallPlan(
            "Restore DevCloud",
            [
                PlanStep(
                    "stop",
                    "Stop DevCloud services before replacing managed state",
                    lambda: self._stop_services(config),
                ),
                PlanStep(
                    "restore",
                    "Verify and restore configuration, database, and included workspace data",
                    lambda: restore_backup(
                        config=config,
                        archive=archive,
                        host_path=self.host_path,
                        runner=self.runner,
                    ),
                ),
                PlanStep(
                    "selinux",
                    "Restore worker storage ownership and SELinux root labels",
                    lambda: self._configure_worker_storage(config)
                    if config.installs_worker
                    else None,
                ),
                PlanStep(
                    "migrations",
                    "Bring the restored database schema to the installed release",
                    lambda: self._run_migrations(config),
                ),
                PlanStep(
                    "start",
                    "Start restored services",
                    lambda: self._restart_services(config),
                ),
            ],
        )

    def status(self, state_root: str = "/var/lib/devcloud/installer") -> dict:
        state = self.current_state(state_root)
        if state is None:
            return {"installed": False, "services": {}}
        services: dict[str, str] = {}
        role = DeploymentRole(state.role)
        names = []
        if role in {DeploymentRole.CONTROLLER, DeploymentRole.ALL_IN_ONE}:
            names.append("devcloud-controller.service")
        if role in {DeploymentRole.WORKER, DeploymentRole.ALL_IN_ONE}:
            names.append("devcloud-worker.service")
        for name in names:
            if self.runner.dry_run:
                services[name] = "unknown"
                continue
            result = self.runner.run(
                ["systemctl", "is-active", name],
                capture_output=True,
                check=False,
            )
            services[name] = result.stdout.strip() or (
                "inactive" if result.returncode else "unknown"
            )
        return {
            "installed": True,
            "role": state.role,
            "version": state.version,
            "installed_at": state.installed_at,
            "updated_at": state.updated_at,
            "configuration": state.configuration,
            "services": services,
        }

    def _validate_config(self, config: InstallConfig) -> None:
        for field_name in (
            "workspace_root",
            "install_root",
            "state_root",
            "releases_root",
            "downloads_root",
        ):
            value = PurePosixPath(
                str(getattr(config, field_name)).replace("\\", "/")
            )
            if not value.is_absolute() or value == PurePosixPath("/"):
                raise InstallerError(f"{field_name} must be a safe absolute path")
            if not any("devcloud" in part.lower() for part in value.parts):
                raise InstallerError(
                    f"{field_name} must identify a DevCloud-specific directory"
                )
        path_policies = {
            "install_root": (PurePosixPath("/opt"), PurePosixPath("/var/lib")),
            "state_root": (PurePosixPath("/var/lib"),),
            "releases_root": (PurePosixPath("/var/lib"),),
            "downloads_root": (PurePosixPath("/srv"), PurePosixPath("/var/lib")),
            "workspace_root": (
                PurePosixPath("/var/lib"),
                PurePosixPath("/srv"),
                PurePosixPath("/mnt"),
            ),
        }
        for field_name, allowed_roots in path_policies.items():
            value = PurePosixPath(
                str(getattr(config, field_name)).replace("\\", "/")
            )
            if not any(value == root or root in value.parents for root in allowed_roots):
                raise InstallerError(
                    f"{field_name} is outside the supported managed roots"
                )
        if (
            config.service_user == "root"
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", config.service_user)
        ):
            raise InstallerError(
                "service_user must be a non-root Linux service account name"
            )
        if config.registry_mode == RegistryMode.EXTERNAL:
            if (
                not config.registry_url
                or "://" in config.registry_url
                or any(character.isspace() for character in config.registry_url)
                or not re.fullmatch(
                    r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._-]+)*",
                    config.registry_url,
                )
            ):
                raise InstallerError(
                    "External registry must be an OCI image prefix such as "
                    "registry.example.com/devcloud"
                )
        if config.database_mode == DatabaseMode.EXTERNAL_POSTGRESQL:
            if not config.database_url.startswith(
                ("postgresql+asyncpg://", "postgresql://")
            ):
                existing = self._read_env(
                    self.host_path("/etc/devcloud/controller.env")
                )
                if not existing.get("DATABASE_URL", "").startswith(
                    ("postgresql+asyncpg://", "postgresql://")
                ):
                    raise InstallerError(
                        "External PostgreSQL requires a postgresql+asyncpg:// URL"
                    )
        if config.role == DeploymentRole.WORKER:
            if not config.worker_id:
                existing_worker = self._read_env(
                    self.host_path("/etc/devcloud/worker.env")
                )
                if not existing_worker.get("DEVCLOUD_NODE_ID"):
                    raise InstallerError("Worker installation requires a worker ID")
            if (
                not config.enrollment_token_file
                and not self.host_path("/etc/devcloud/worker.env").is_file()
                and not self.runner.dry_run
            ):
                raise InstallerError(
                    "Worker installation requires an enrollment token file"
                )
        if config.installs_controller and not config.admin_password:
            existing = self.host_path("/etc/devcloud/controller.env")
            if not existing.is_file():
                raise InstallerError(
                    "A new controller installation requires an administrator password"
                )

    @staticmethod
    def _dnf_disable_repositories_option(help_text: str) -> str:
        if "--disable-repo" in help_text:
            return "--disable-repo=*"
        if "--disablerepo" in help_text:
            return "--disablerepo=*"
        raise InstallerError(
            "The installed DNF command has no supported repository-disable option."
        )

    @staticmethod
    def _dnf_enable_repository_option(help_text: str, repository_id: str) -> str:
        if "--enable-repo" in help_text:
            return f"--enable-repo={repository_id}"
        if "--enablerepo" in help_text:
            return f"--enablerepo={repository_id}"
        raise InstallerError(
            "The installed DNF command has no supported repository-enable option."
        )

    def _install_offline_packages(
        self,
        repository_root: Path,
        packages: set[str],
    ) -> None:
        repository_id = "devcloud-offline"
        if self.runner.dry_run:
            disable_option = "--disablerepo=*"
            enable_option = f"--enablerepo={repository_id}"
        else:
            help_result = self.runner.run(
                ["dnf", "--help"],
                capture_output=True,
                check=False,
            )
            disable_option = self._dnf_disable_repositories_option(
                f"{help_result.stdout}\n{help_result.stderr}"
            )
            enable_option = self._dnf_enable_repository_option(
                f"{help_result.stdout}\n{help_result.stderr}",
                repository_id,
            )
        self.runner.run(
            [
                "dnf",
                disable_option,
                f"--repofrompath={repository_id},{repository_root.resolve().as_uri()}",
                enable_option,
                "install",
                "-y",
                *sorted(packages),
            ]
        )

    def _install_packages(self, config: InstallConfig) -> None:
        packages = {
            "gnupg2",
            "python3",
            "python3-pip",
            "policycoreutils-python-utils",
            "subscription-manager",
        }
        if config.installs_controller:
            packages.update({"nginx", "curl", "createrepo_c"})
        if config.installs_worker:
            packages.update({"podman", "crun", "tar", "gzip"})
        if config.database_mode == DatabaseMode.BUNDLED_POSTGRESQL:
            packages.add("postgresql-server")
        elif (
            config.installs_controller
            and config.database_mode == DatabaseMode.EXTERNAL_POSTGRESQL
        ):
            packages.add("postgresql")
        offline_manifest = self.project_root / "offline" / "MANIFEST.json"
        if offline_manifest.is_file():
            profile = detect_platform(self.host_path("/etc/os-release")).profile
            rpm_root = self.project_root / "offline" / "system-rpms" / profile
            rpms = sorted(rpm_root.glob("*.rpm")) if rpm_root.is_dir() else []
            if not rpms or not (rpm_root / "repodata" / "repomd.xml").is_file():
                raise InstallerError(
                    "This release is marked as an offline bundle but has no "
                    f"complete RPM repository for {profile}"
                )
            self._install_offline_packages(rpm_root, packages)
            return

        result = self.runner.run(
            ["dnf", "install", "-y", *sorted(packages)],
            check=False,
        )
        if result.returncode == 0 or self.runner.dry_run:
            return
        profile = detect_platform(self.host_path("/etc/os-release")).profile
        rpm_root = self.project_root / "offline" / "system-rpms" / profile
        rpms = sorted(rpm_root.glob("*.rpm")) if rpm_root.is_dir() else []
        if not rpms or not (rpm_root / "repodata" / "repomd.xml").is_file():
            raise InstallerError(
                "DNF repositories could not install required packages and the "
                f"release has no complete offline RPM repository for {profile}"
            )
        self._install_offline_packages(rpm_root, packages)

    def _configure_database(self, config: InstallConfig) -> None:
        if (
            not config.installs_controller
            or config.database_mode != DatabaseMode.BUNDLED_POSTGRESQL
        ):
            return
        pg_version = self.host_path("/var/lib/pgsql/data/PG_VERSION")
        if self.runner.dry_run or not pg_version.is_file():
            self.runner.run(["postgresql-setup", "--initdb"])
        self.runner.run(["systemctl", "enable", "--now", "postgresql.service"])
        role = self.runner.run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-tAc",
                "SELECT 1 FROM pg_roles WHERE rolname='devcloud'",
            ],
            capture_output=True,
            check=False,
        )
        if self.runner.dry_run or role.returncode != 0 or role.stdout.strip() != "1":
            self.runner.run(
                [
                    "runuser",
                    "-u",
                    "postgres",
                    "--",
                    "createuser",
                    "--login",
                    "devcloud",
                ]
            )
        database = self.runner.run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-tAc",
                "SELECT 1 FROM pg_database WHERE datname='devcloud'",
            ],
            capture_output=True,
            check=False,
        )
        if (
            self.runner.dry_run
            or database.returncode != 0
            or database.stdout.strip() != "1"
        ):
            self.runner.run(
                [
                    "runuser",
                    "-u",
                    "postgres",
                    "--",
                    "createdb",
                    "--owner",
                    "devcloud",
                    "devcloud",
                ]
            )

    def _ensure_service_user(self, config: InstallConfig) -> None:
        if self.runner.dry_run:
            self.runner.run(
                [
                    "useradd",
                    "--system",
                    "--home-dir",
                    "/var/lib/devcloud",
                    "--shell",
                    "/sbin/nologin",
                    config.service_user,
                ]
            )
            if config.installs_worker:
                self.runner.run(
                    [
                        "usermod",
                        "--add-subuids",
                        "100000-165535",
                        "--add-subgids",
                        "100000-165535",
                        config.service_user,
                    ]
                )
            return
        result = self.runner.run(
            ["id", "-u", config.service_user],
            capture_output=True,
            check=False,
        ) if self.runner.exists("id") else None
        if not result or result.returncode != 0:
            self.runner.run(
                [
                    "useradd",
                    "--system",
                    "--home-dir",
                    "/var/lib/devcloud",
                    "--shell",
                    "/sbin/nologin",
                    config.service_user,
                ]
            )
        if config.installs_worker:
            self._ensure_worker_subids(config.service_user)

    def _ensure_worker_subids(self, service_user: str) -> None:
        """Give a system account the mappings required by rootless Podman."""
        subuid = self.host_path("/etc/subuid")
        subgid = self.host_path("/etc/subgid")

        def entry(path: Path) -> tuple[int, int] | None:
            if not path.is_file():
                return None
            for raw in path.read_text(encoding="utf-8").splitlines():
                fields = raw.split(":")
                if len(fields) != 3 or fields[0] != service_user:
                    continue
                try:
                    return int(fields[1]), int(fields[2])
                except ValueError:
                    continue
            return None

        def free_range(path: Path) -> tuple[int, int]:
            candidate = 100000
            length = 65536
            ranges: list[tuple[int, int]] = []
            if path.is_file():
                for raw in path.read_text(encoding="utf-8").splitlines():
                    fields = raw.split(":")
                    if len(fields) != 3:
                        continue
                    try:
                        start, count = int(fields[1]), int(fields[2])
                    except ValueError:
                        continue
                    ranges.append((start, start + count - 1))
            for start, end in sorted(ranges):
                if candidate + length - 1 < start:
                    break
                if candidate <= end:
                    candidate = end + 1
            return candidate, candidate + length - 1

        uid_entry = entry(subuid)
        gid_entry = entry(subgid)
        command = ["usermod"]
        if uid_entry is None:
            start, end = free_range(subuid)
            command.extend(["--add-subuids", f"{start}-{end}"])
        if gid_entry is None:
            start, end = free_range(subgid)
            command.extend(["--add-subgids", f"{start}-{end}"])
        if len(command) > 1:
            command.append(service_user)
            self.runner.run(command)

    def _ensure_directories(self, config: InstallConfig) -> None:
        owned = [
            "/var/lib/devcloud",
            config.install_root,
            config.state_root,
            config.releases_root,
            "/var/lib/devcloud/database",
            "/var/lib/devcloud/download-builds",
            "/var/lib/devcloud/update-queue",
            "/var/lib/devcloud/update-queue/uploads",
        ]
        if config.installs_worker:
            owned.append(config.workspace_root)
        if config.installs_controller:
            owned.append(config.downloads_root)
        if self.runner.dry_run:
            for value in owned:
                self.runner.run(
                    [
                        "install",
                        "-d",
                        "-o",
                        config.service_user,
                        "-g",
                        config.service_user,
                        "-m",
                        "0750",
                        value,
                    ]
                )
            self.runner.run(["install", "-d", "-m", "0750", "/etc/devcloud"])
            return
        for value in owned:
            path = self.host_path(value)
            path.mkdir(parents=True, exist_ok=True)
            if not self.runner.dry_run:
                self.runner.run(
                    ["chown", f"{config.service_user}:{config.service_user}", str(path)]
                )
        etc_dir = self.host_path("/etc/devcloud")
        etc_dir.mkdir(parents=True, exist_ok=True)
        if not self.runner.dry_run:
            os.chmod(etc_dir, 0o750)

    def _install_release(self, config: InstallConfig) -> None:
        target = self.host_path(config.releases_root) / self.release_id
        if self.runner.dry_run:
            self.runner.run(
                [
                    "devcloud-internal-install-release",
                    str(self.project_root),
                    str(target),
                ]
            )
            return
        marker_name = ".devcloud-release-id"
        if target.exists():
            marker = target / marker_name
            if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != self.release_id:
                raise InstallerError(
                    f"Release target exists without a valid identity marker: {target}"
                )
        else:
            temporary_release = target.with_name(
                f".{target.name}.staging-{uuid.uuid4().hex}"
            )
            try:
                shutil.copytree(
                    self.project_root,
                    temporary_release,
                    symlinks=False,
                    ignore=shutil.ignore_patterns(
                        ".git",
                        ".venv",
                        ".pytest_cache",
                        ".pytest_tmp",
                        "__pycache__",
                        "*.pyc",
                        "dist",
                    ),
                )
                (temporary_release / marker_name).write_text(
                    self.release_id + "\n", encoding="utf-8"
                )
                os.replace(temporary_release, target)
            finally:
                if temporary_release.exists():
                    shutil.rmtree(temporary_release)
        install_root = self.host_path(config.install_root)
        install_root.mkdir(parents=True, exist_ok=True)
        current = install_root / "current"
        if current.is_symlink():
            try:
                self.previous_release = current.resolve(strict=True)
            except FileNotFoundError:
                self.previous_release = None
        temporary = install_root / f".current-{uuid.uuid4().hex}"
        relative_target = os.path.relpath(target, start=install_root)
        temporary.symlink_to(relative_target, target_is_directory=True)
        os.replace(temporary, current)

    def _rollback_release(self, config: InstallConfig) -> None:
        if self.runner.dry_run:
            self.runner.run(["devcloud-internal-rollback-release"])
            return
        if self.migration_applied or self.previous_release is None:
            return
        install_root = self.host_path(config.install_root)
        current = install_root / "current"
        temporary = install_root / f".rollback-{uuid.uuid4().hex}"
        relative_target = os.path.relpath(self.previous_release, start=install_root)
        temporary.symlink_to(relative_target, target_is_directory=True)
        os.replace(temporary, current)

    def _install_python_dependencies(self, config: InstallConfig) -> None:
        release = self.host_path(config.releases_root) / self.release_id
        venv = release / ".venv"
        if not (venv / "bin" / "python").exists():
            previous = self.previous_release
            current_requirements = release / "requirements.txt"
            previous_requirements = previous / "requirements.txt" if previous else None
            wheels = release / "offline" / "wheels"
            can_reuse = (
                previous is not None
                and (previous / ".venv" / "bin" / "python").exists()
                and previous_requirements is not None
                and previous_requirements.is_file()
                and current_requirements.is_file()
                and hashlib.sha256(previous_requirements.read_bytes()).digest()
                == hashlib.sha256(current_requirements.read_bytes()).digest()
                and not (wheels.is_dir() and any(wheels.glob("*.whl")))
            )
            if can_reuse and not self.runner.dry_run:
                venv.symlink_to(previous / ".venv", target_is_directory=True)
                return
            self.runner.run(["python3", "-m", "venv", str(venv)])
        pip = str(venv / "bin" / "python")
        wheels = release / "offline" / "wheels"
        command = [pip, "-m", "pip", "install", "--disable-pip-version-check"]
        if wheels.is_dir() and any(wheels.glob("*.whl")):
            command.extend(["--no-index", "--find-links", str(wheels)])
        command.extend(["-r", str(release / "requirements.txt")])
        self.runner.run(command)

    @staticmethod
    def _quote_env(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _read_env(self, path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        if not path.is_file():
            return values
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw or raw.lstrip().startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            raw_value = value.strip()
            if raw_value.startswith('"') and raw_value.endswith('"'):
                try:
                    values[key] = str(json.loads(raw_value))
                    continue
                except json.JSONDecodeError:
                    pass
            values[key] = raw_value.strip('"')
        return values

    def _write_env(self, path: Path, values: dict[str, str]) -> None:
        payload = "".join(
            f"{key}={self._quote_env(str(value))}\n"
            for key, value in sorted(values.items())
            if value != ""
        )
        self._atomic_write(path, payload.encode("utf-8"), 0o600)

    def _write_configuration(self, config: InstallConfig) -> None:
        if self.runner.dry_run:
            self.runner.run(
                [
                    "devcloud-internal-write-config",
                    config.role.value,
                    "/etc/devcloud",
                ]
            )
            return
        etc_dir = self.host_path("/etc/devcloud")
        etc_dir.mkdir(parents=True, exist_ok=True)
        controller_env_path = etc_dir / "controller.env"
        worker_env_path = etc_dir / "worker.env"

        local_worker_id = ""
        local_worker_token = ""
        if config.role == DeploymentRole.ALL_IN_ONE:
            old_worker = self._read_env(worker_env_path)
            local_worker_id = old_worker.get("DEVCLOUD_NODE_ID", "") or str(uuid.uuid4())
            local_worker_token = old_worker.get("DEVCLOUD_NODE_TOKEN", "") or secrets.token_urlsafe(32)

        if config.installs_controller:
            existing = self._read_env(controller_env_path)
            secret_key = existing.get("SECRET_KEY", "") or secrets.token_urlsafe(48)
            admin_password = (
                existing.get("ADMIN_PASSWORD", "")
                or config.admin_password
            )
            controller_values = {
                "ENV": "production",
                "DEBUG": "False",
                "SECRET_KEY": secret_key,
                "COOKIE_SECURE": "True" if config.public_url.startswith("https://") else "False",
                "DATABASE_URL": (
                    (
                        "postgresql+asyncpg://"
                        + existing.get("DATABASE_URL", "")[len("postgresql://") :]
                        if existing.get("DATABASE_URL", "").startswith(
                            "postgresql://"
                        )
                        else existing.get("DATABASE_URL", "")
                    )
                    if config.database_mode == DatabaseMode.EXTERNAL_POSTGRESQL
                    and not config.database_url
                    else config.effective_database_url()
                ),
                "AUTO_MIGRATE": "False",
                "STORAGE_ROOT": config.workspace_root,
                "DOWNLOADS_ROOT": config.downloads_root,
                "DOWNLOAD_BUILD_ROOT": "/var/lib/devcloud/download-builds",
                "DOWNLOAD_PUBLIC_BASE_URL": config.public_url,
                "ADMIN_USERNAME": config.admin_username,
                "ADMIN_EMAIL": config.admin_email,
                "ADMIN_PASSWORD": admin_password,
                "DEVCLOUD_DEPLOYMENT_ROLE": config.role.value,
                "DEVCLOUD_REGISTRY_MODE": config.registry_mode.value,
                "DEVCLOUD_REGISTRY_URL": config.registry_url,
            }
            if local_worker_id:
                controller_values.update(
                    {
                        "DEVCLOUD_BOOTSTRAP_WORKER_ID": local_worker_id,
                        "DEVCLOUD_BOOTSTRAP_WORKER_NAME": config.worker_name,
                        "DEVCLOUD_BOOTSTRAP_WORKER_TOKEN_HASH": hashlib.sha256(
                            local_worker_token.encode("utf-8")
                        ).hexdigest(),
                    }
                )
            self._write_env(controller_env_path, controller_values)

        if config.installs_worker:
            if config.role == DeploymentRole.WORKER:
                existing_worker = self._read_env(worker_env_path)
                token_path = (
                    Path(config.enrollment_token_file)
                    if config.enrollment_token_file
                    else None
                )
                if (
                    token_path is not None
                    and not token_path.is_file()
                    and not self.runner.dry_run
                ):
                    raise InstallerError(
                        f"Enrollment token file does not exist: {token_path}"
                    )
                token = (
                    token_path.read_text(encoding="utf-8").strip()
                    if token_path is not None and token_path.is_file()
                    else existing_worker.get("DEVCLOUD_NODE_TOKEN", "")
                    or "dry-run-token"
                )
                worker_id = (
                    config.worker_id
                    or existing_worker.get("DEVCLOUD_NODE_ID", "")
                )
                controller_url = (
                    config.controller_url
                    or existing_worker.get("DEVCLOUD_CONTROLLER_URL", "")
                    or existing_worker.get("DEVCLOUD_MASTER_URL", "")
                )
            else:
                token = local_worker_token
                worker_id = local_worker_id
                controller_url = "http://127.0.0.1:8000"
            if not token or any(character.isspace() for character in token):
                raise InstallerError("Enrollment token is empty or contains whitespace")
            self._write_env(
                worker_env_path,
                {
                    "DEVCLOUD_CONTROLLER_URL": controller_url,
                    "DEVCLOUD_MASTER_URL": controller_url,
                    "DEVCLOUD_NODE_ID": worker_id,
                    "DEVCLOUD_NODE_TOKEN": token,
                    "DEVCLOUD_WORKER_NAME": config.worker_name,
                    "STORAGE_ROOT": config.workspace_root,
                },
            )

    def _run_migrations(self, config: InstallConfig) -> None:
        if not config.installs_controller:
            return
        release = self.host_path(config.install_root) / "current"
        python = release / ".venv" / "bin" / "python"
        controller_env = self._read_env(
            self.host_path("/etc/devcloud/controller.env")
        )
        self.runner.run(
            [str(python), "-m", "app.migrations", "upgrade"],
            cwd=release,
            env=controller_env,
        )
        self.migration_applied = True

    def _install_services(self, config: InstallConfig) -> None:
        release = self.host_path(config.install_root) / "current"
        systemd_dir = self.host_path("/etc/systemd/system")
        if not self.runner.dry_run:
            systemd_dir.mkdir(parents=True, exist_ok=True)
        units: list[tuple[str, str]] = [
            ("devcloud-update.service", "devcloud-update.service"),
            ("devcloud-update.path", "devcloud-update.path"),
        ]
        if config.installs_controller:
            units.append(("devcloud.service", "devcloud-controller.service"))
        if config.installs_worker:
            units.append(("devcloud-worker.service", "devcloud-worker.service"))
        for source_name, target_name in units:
            template = (self.project_root / "deploy" / source_name).read_text(
                encoding="utf-8"
            )
            rendered = template.replace("{{USER}}", config.service_user).replace(
                "{{PROJECT_DIR}}", str(release)
            )
            if self.runner.dry_run:
                self.runner.run(
                    [
                        "install",
                        "-m",
                        "0644",
                        str(self.project_root / "deploy" / source_name),
                        str(systemd_dir / target_name),
                    ]
                )
            else:
                self._atomic_write(
                    systemd_dir / target_name,
                    rendered.encode("utf-8"),
                    0o644,
                )
        self.runner.run(["systemctl", "daemon-reload"])

    def _configure_worker_storage(self, config: InstallConfig) -> None:
        if not config.installs_worker:
            return
        script = self.host_path(config.install_root) / "current" / "deploy" / "configure_selinux.sh"
        self.runner.run(
            ["bash", str(script)],
            env={
                "DEVCLOUD_SERVICE_USER": config.service_user,
                "DEVCLOUD_STORAGE_ROOT": str(self.host_path(config.workspace_root)),
            },
        )

    def _prepare_images(self, config: InstallConfig) -> None:
        if not config.installs_worker or not config.preload_images:
            return
        uid_result = self.runner.run(
            ["id", "-u", config.service_user],
            capture_output=True,
        )
        service_uid = uid_result.stdout.strip()
        if not service_uid:
            if not self.runner.dry_run:
                raise InstallerError(
                    f"Could not resolve the uid for service user {config.service_user}."
                )
            service_uid = "SERVICE_UID"
        runtime_dir = f"/run/user/{service_uid}"
        self.runner.run(
            [
                "install",
                "-d",
                "-o",
                config.service_user,
                "-g",
                config.service_user,
                "-m",
                "0700",
                runtime_dir,
            ]
        )
        user_command = [
            "runuser",
            "-u",
            config.service_user,
            "--",
            "env",
            "HOME=/var/lib/devcloud",
            f"XDG_RUNTIME_DIR={runtime_dir}",
        ]
        release = self.host_path(config.install_root) / "current"
        image_dir = release / "offline" / "images"
        archives = sorted(image_dir.glob("*.tar")) if image_dir.is_dir() else []
        if archives:
            for archive in archives:
                self.runner.run(
                    [*user_command, "podman", "load", "-i", str(archive)]
                )
            return
        build_script = release / "containers" / "build_images.sh"
        self.runner.run([*user_command, "bash", str(build_script)])

    def _install_ingress(self, config: InstallConfig) -> None:
        if not config.installs_controller:
            return
        script = self.host_path(config.install_root) / "current" / "deploy" / "install_ingress.sh"
        self.runner.run(["bash", str(script), config.service_user])

    def _start_services(self, config: InstallConfig) -> None:
        self.runner.run(
            ["systemctl", "enable", "--now", "devcloud-update.path"]
        )
        if config.installs_controller:
            self.runner.run(
                ["systemctl", "enable", "--now", "devcloud-controller.service"]
            )
        if config.installs_worker:
            self.runner.run(
                ["systemctl", "enable", "--now", "devcloud-worker.service"]
            )
        self._verify_services(config)

    def _restart_services(self, config: InstallConfig) -> None:
        self.runner.run(
            ["systemctl", "enable", "--now", "devcloud-update.path"]
        )
        if config.installs_controller:
            self.runner.run(["systemctl", "enable", "devcloud-controller.service"])
            self.runner.run(["systemctl", "restart", "devcloud-controller.service"])
        if config.installs_worker:
            self.runner.run(["systemctl", "enable", "devcloud-worker.service"])
            self.runner.run(["systemctl", "restart", "devcloud-worker.service"])
        self._verify_services(config)

    def _verify_services(self, config: InstallConfig) -> None:
        names = ["devcloud-update.path"]
        if config.installs_controller:
            names.append("devcloud-controller.service")
        if config.installs_worker:
            names.append("devcloud-worker.service")
        for name in names:
            self.runner.run(["systemctl", "is-active", "--quiet", name])
        if config.installs_worker:
            release = self.host_path(config.install_root) / "current"
            worker_env = self._read_env(
                self.host_path("/etc/devcloud/worker.env")
            )
            self.runner.run(
                [
                    str(release / ".venv" / "bin" / "python"),
                    "-m",
                    "app.installer.verify_worker",
                ],
                cwd=release,
                env=worker_env,
            )

    def _stop_services(self, config: InstallConfig) -> None:
        names = ["devcloud-update.path", "devcloud-update.service"]
        if config.installs_worker:
            names.append("devcloud-worker.service")
        if config.installs_controller:
            names.append("devcloud-controller.service")
        for name in names:
            self.runner.run(["systemctl", "disable", "--now", name])

    def _remove_service_units(self, config: InstallConfig) -> None:
        systemd_dir = self.host_path("/etc/systemd/system")
        names = ["devcloud-update.service", "devcloud-update.path"]
        if config.installs_controller:
            names.append("devcloud-controller.service")
        if config.installs_worker:
            names.append("devcloud-worker.service")
        for name in names:
            if not self.runner.dry_run:
                (systemd_dir / name).unlink(missing_ok=True)
        self.runner.run(["systemctl", "daemon-reload"])

    def _remove_active_release(self, config: InstallConfig) -> None:
        if self.runner.dry_run:
            self.runner.run(
                ["devcloud-internal-remove-active-release", config.install_root]
            )
            return
        current = self.host_path(config.install_root) / "current"
        if current.is_symlink() or current.is_file():
            current.unlink(missing_ok=True)

    def _purge_data(self, config: InstallConfig) -> None:
        targets = {
            self.host_path(config.install_root),
            self.host_path(config.state_root),
            self.host_path(config.releases_root),
            self.host_path("/etc/devcloud"),
        }
        if config.installs_worker:
            targets.add(self.host_path(config.workspace_root))
        if config.installs_controller:
            targets.update(
                {
                    self.host_path(config.downloads_root),
                    self.host_path("/var/lib/devcloud/database"),
                    self.host_path("/var/lib/devcloud/download-builds"),
                }
            )
        root = self.filesystem_root.resolve()
        for target in targets:
            resolved = target.resolve()
            if resolved == root or root not in resolved.parents:
                raise InstallerError(f"Refusing unsafe purge target: {resolved}")
            if self.runner.dry_run:
                self.runner.run(["devcloud-internal-purge", str(resolved)])
                continue
            if resolved.exists():
                shutil.rmtree(resolved)

    def _save_state(self, config: InstallConfig) -> None:
        if self.runner.dry_run:
            self.runner.run(
                [
                    "devcloud-internal-save-state",
                    config.role.value,
                    self.release_version,
                ]
            )
            return
        path = self.state_path(config)
        existing = InstallationState.load(path)
        if existing:
            state = InstallationState(
                schema=existing.schema,
                role=config.role.value,
                version=self.release_version,
                installed_at=existing.installed_at,
                updated_at=datetime.now(timezone.utc).isoformat(),
                configuration=config.public_dict(),
            )
        else:
            state = InstallationState.create(
                config.role.value, self.release_version, config.public_dict()
            )
        state.save(path)

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(mode))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
