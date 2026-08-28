from __future__ import annotations

import argparse
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from app.installer.engine import InstallerEngine
from app.installer.models import DeploymentRole, InstallConfig
from app.installer.platform import CommandRunner, InstallerError
from app.installer.release import prepare_release
from app.installer.ui import InstallerUI
from deploy.package_offline import PackageError, verify_staged_bundle


DEFAULT_STATE_ROOT = "/var/lib/devcloud/installer"


def _verify_offline_release(root: Path) -> None:
    """Verify every embedded artifact before an offline update can load it."""
    manifest = root / "offline" / "MANIFEST.json"
    if not manifest.is_file():
        return
    try:
        verify_staged_bundle(root, expected_role="server")
    except PackageError as exc:
        raise InstallerError(f"Offline release verification failed: {exc}") from exc


def _worker_config_from_environment(
    role: DeploymentRole | None,
) -> InstallConfig | None:
    """Build a non-interactive worker config for the shell bootstrap.

    The shell bootstrap deliberately cannot depend on Python being present:
    devcloud-setup installs Python after subscription-manager is available.
    Environment variables carry the enrollment answers across that boundary.
    """
    if role != DeploymentRole.WORKER:
        return None
    names = {
        "controller_url": "DEVCLOUD_INSTALL_CONTROLLER_URL",
        "worker_id": "DEVCLOUD_INSTALL_WORKER_ID",
        "enrollment_token_file": "DEVCLOUD_INSTALL_TOKEN_FILE",
    }
    values = {key: os.environ.get(name, "").strip() for key, name in names.items()}
    if not any(values.values()):
        return None
    missing = [name for key, name in names.items() if not values[key]]
    if missing:
        raise InstallerError(
            "Incomplete non-interactive worker enrollment: "
            + ", ".join(missing)
        )
    return InstallConfig(
        role=role,
        controller_url=values["controller_url"],
        worker_id=values["worker_id"],
        enrollment_token_file=values["enrollment_token_file"],
        worker_name=(
            os.environ.get("DEVCLOUD_INSTALL_WORKER_NAME", "").strip()
            or socket.gethostname()
        ),
        workspace_root=(
            os.environ.get("DEVCLOUD_INSTALL_WORKSPACE_ROOT", "").strip()
            or "/var/lib/devcloud/workspaces"
        ),
        preload_images=os.environ.get(
            "DEVCLOUD_INSTALL_PRELOAD_IMAGES", "true"
        ).strip().lower()
        not in {"0", "false", "no"},
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devcloud-setup",
        description="Interactive installer and lifecycle manager for DevCloud",
    )
    parser.add_argument("--dry-run", action="store_true", help="show and record commands without changing the host")
    parser.add_argument("--yes", action="store_true", help="accept the displayed plan")
    parser.add_argument("--filesystem-root", default="/", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")

    install = commands.add_parser("install", help="install a supported deployment role")
    install.add_argument(
        "role",
        nargs="?",
        choices=[role.value for role in DeploymentRole],
    )
    install.add_argument("--answers", type=Path, help="JSON answer file")

    update = commands.add_parser("update", help="apply a connected or uploaded release")
    update.add_argument("--bundle", type=Path, required=True)
    update.add_argument(
        "--allow-unsigned",
        action="store_true",
        help="allow an unsigned source ZIP after explicit operator review",
    )
    update.add_argument(
        "--keyring",
        type=Path,
        default=Path("/etc/devcloud/release-keyring.gpg"),
    )

    commands.add_parser("repair", help="repair configuration, permissions, SELinux, and services")
    commands.add_parser("status", help="show installation and service state")
    backup = commands.add_parser("backup", help="back up configuration and data")
    backup.add_argument("--output", type=Path)
    backup.add_argument(
        "--include-workspaces",
        action="store_true",
        help="include persistent workspace files (may be large)",
    )
    restore = commands.add_parser("restore", help="restore a verified backup")
    restore.add_argument("--bundle", type=Path)
    uninstall = commands.add_parser("uninstall", help="remove installed services")
    uninstall.add_argument(
        "--purge",
        action="store_true",
        help="also permanently remove configuration and data",
    )
    return parser


def _config_from_state(engine: InstallerEngine) -> InstallConfig:
    state = engine.current_state(DEFAULT_STATE_ROOT)
    if state is None:
        raise InstallerError("No managed DevCloud installation was found")
    return InstallConfig.from_dict({**state.configuration, "role": state.role})


def _execute_plan(plan, ui: InstallerUI, *, assume_yes: bool) -> None:
    ui.show_plan(plan.title, plan.descriptions)
    if not assume_yes and not ui.confirm("Proceed with this plan?", False):
        ui.write("No changes were made.")
        return
    plan.execute()
    ui.write(f"\n{plan.title} completed successfully.")


def _interactive_command(ui: InstallerUI, installed: bool) -> str:
    options = [
        ("install", "Install Controller, All-in-one, or Worker"),
        ("update", "Update existing installation from a release bundle"),
        ("repair", "Repair or reconfigure existing installation"),
        ("status", "Show installation status"),
        ("backup", "Back up configuration and data"),
        ("restore", "Restore a backup"),
        ("uninstall", "Uninstall"),
        ("quit", "Quit"),
    ]
    if not installed:
        options = [options[0], options[-1]]
    return ui.choose("DevCloud Setup", options)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    ui = InstallerUI()
    runner = CommandRunner(dry_run=args.dry_run)
    engine = InstallerEngine(
        filesystem_root=Path(args.filesystem_root),
        runner=runner,
    )

    try:
        command = args.command
        if command is None:
            command = _interactive_command(
                ui, engine.current_state(DEFAULT_STATE_ROOT) is not None
            )
            if command == "quit":
                return 0

        if command == "install":
            if getattr(args, "answers", None):
                config = InstallConfig.from_json_file(args.answers)
            else:
                role = DeploymentRole(args.role) if getattr(args, "role", None) else None
                config = (
                    _worker_config_from_environment(role)
                    or ui.collect_install_config(role)
                )
            _execute_plan(
                engine.build_install_plan(config),
                ui,
                assume_yes=args.yes,
            )
            return 0

        if command == "status":
            ui.write(json.dumps(engine.status(DEFAULT_STATE_ROOT), indent=2))
            return 0

        config = _config_from_state(engine)

        if command == "repair":
            _execute_plan(
                engine.build_repair_plan(config),
                ui,
                assume_yes=args.yes,
            )
            return 0

        if command == "backup":
            default_output = Path.cwd() / (
                "devcloud-backup-"
                + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                + ".tar.gz"
            )
            output = getattr(args, "output", None)
            if output is None and args.command is None:
                output = Path(ui.ask("Backup output", str(default_output)))
            output = output or default_output
            include_workspaces = bool(
                getattr(args, "include_workspaces", False)
            )
            if args.command is None:
                include_workspaces = ui.confirm(
                    "Include persistent workspace data?", False
                )
            _execute_plan(
                engine.build_backup_plan(
                    config,
                    output=output,
                    include_workspaces=include_workspaces,
                ),
                ui,
                assume_yes=args.yes,
            )
            return 0

        if command == "restore":
            bundle = getattr(args, "bundle", None)
            if bundle is None:
                bundle = Path(ui.ask("Backup archive"))
            if not args.yes:
                expected = config.worker_name or config.role.value
                typed = ui.ask(
                    f"Restore replaces managed state. Type {expected!r} to continue"
                )
                if typed != expected:
                    ui.write("Confirmation did not match; no changes were made.")
                    return 1
            _execute_plan(
                engine.build_restore_plan(config, archive=bundle.resolve()),
                ui,
                assume_yes=True,
            )
            return 0

        if command == "update":
            bundle = getattr(args, "bundle", None)
            if bundle is None:
                bundle = Path(ui.ask("Release ZIP or tar archive"))
            allow_unsigned = bool(getattr(args, "allow_unsigned", False))
            if args.command is None:
                allow_unsigned = ui.confirm(
                    "This is an unsigned source ZIP?", False
                )
            if allow_unsigned and not args.yes:
                ui.write(
                    "WARNING: unsigned source archives have no publisher authenticity guarantee."
                )
                if not ui.confirm("Continue with this unsigned source?", False):
                    return 0
            keyring = engine.host_path(
                getattr(args, "keyring", Path("/etc/devcloud/release-keyring.gpg"))
            )
            with prepare_release(
                bundle.resolve(),
                runner=runner,
                keyring=keyring,
                require_signature=not allow_unsigned,
            ) as prepared:
                _verify_offline_release(prepared.root)
                update_engine = InstallerEngine(
                    project_root=prepared.root,
                    filesystem_root=Path(args.filesystem_root),
                    runner=runner,
                )
                _execute_plan(
                    update_engine.build_update_plan(config),
                    ui,
                    assume_yes=args.yes,
                )
            return 0

        if command == "uninstall":
            purge = bool(getattr(args, "purge", False))
            if args.command is None:
                purge = ui.confirm(
                    "Permanently remove configuration and managed data?", False
                )
            if purge and not args.yes:
                expected = config.worker_name or config.role.value
                typed = ui.ask(
                    f"Type {expected!r} to permanently delete managed data"
                )
                if typed != expected:
                    ui.write("Confirmation did not match; no changes were made.")
                    return 1
            _execute_plan(
                engine.build_uninstall_plan(config, purge_data=purge),
                ui,
                assume_yes=args.yes or purge,
            )
            return 0

        parser.error(f"Unsupported command: {command}")
    except (InstallerError, ValueError, OSError) as exc:
        ui.write(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
