"""Managed installation backup and restore primitives."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from app.installer.models import DatabaseMode, InstallConfig
from app.installer.platform import CommandRunner, InstallerError
from app.installer.release import _extract_tar


BACKUP_FORMAT = 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    for path in source.rglob("*"):
        if path.is_symlink():
            raise InstallerError(f"Refusing to back up symbolic link: {path}")
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        raise InstallerError("The configured database URL is not SQLite")
    value = database_url[len(prefix) :]
    return Path(value if value.startswith("/") else value)


def _postgres_cli_connection(database_url: str) -> tuple[str, dict[str, str]]:
    # Keep the lifecycle installer importable before application wheels exist.
    # SQLAlchemy is available in every installed release when backup/restore runs.
    from sqlalchemy.engine import URL, make_url

    try:
        parsed = make_url(database_url)
    except Exception as exc:
        raise InstallerError("The configured PostgreSQL URL is invalid") from exc
    if not parsed.drivername.startswith("postgresql"):
        raise InstallerError("The configured database URL is not PostgreSQL")
    password = parsed.password
    parsed = URL.create(
        drivername="postgresql",
        username=parsed.username,
        host=parsed.host,
        port=parsed.port,
        database=parsed.database,
        query=parsed.query,
    )
    environment = {"PGPASSWORD": password} if password else {}
    return parsed.render_as_string(hide_password=False), environment


def create_backup(
    *,
    config: InstallConfig,
    version: str,
    output: Path,
    host_path,
    runner: CommandRunner,
    include_workspaces: bool,
) -> Path:
    output = output.resolve()
    if runner.dry_run:
        runner.run(
            [
                "devcloud-internal-backup",
                str(output),
                "--include-workspaces" if include_workspaces else "--configuration-only",
            ]
        )
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="devcloud-backup-") as temporary:
        root = Path(temporary) / "devcloud-backup"
        payload = root / "payload"
        payload.mkdir(parents=True)
        _copy_tree(host_path("/etc/devcloud"), payload / "etc-devcloud")
        state = host_path(config.state_root) / "install-state.json"
        if state.is_file():
            (payload / "installer").mkdir()
            shutil.copy2(state, payload / "installer" / state.name)

        controller_env = host_path("/etc/devcloud/controller.env")
        env_values: dict[str, str] = {}
        if controller_env.is_file():
            for raw in controller_env.read_text(encoding="utf-8").splitlines():
                if raw and not raw.startswith("#") and "=" in raw:
                    key, value = raw.split("=", 1)
                    env_values[key] = value.strip().strip('"')
        database_url = env_values.get("DATABASE_URL", config.effective_database_url())
        database_kind = "none"
        if config.installs_controller and database_url.startswith(
            "sqlite+aiosqlite:///"
        ):
            source_db = host_path(_sqlite_path(database_url))
            if source_db.is_file():
                target_db = payload / "database" / "devcloud.db"
                target_db.parent.mkdir()
                source = sqlite3.connect(source_db)
                target = sqlite3.connect(target_db)
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
                database_kind = "sqlite"
        elif config.installs_controller:
            target_db = payload / "database" / "devcloud.pgdump"
            target_db.parent.mkdir()
            cli_url, pg_environment = _postgres_cli_connection(database_url)
            runner.run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--file",
                    str(target_db),
                    cli_url,
                ],
                env=pg_environment,
            )
            database_kind = "postgresql"

        if include_workspaces and config.installs_worker:
            _copy_tree(
                host_path(config.workspace_root),
                payload / "workspaces",
            )

        members = []
        for path in sorted(item for item in payload.rglob("*") if item.is_file()):
            members.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            )
        manifest = {
            "format": BACKUP_FORMAT,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "role": config.role.value,
            "version": version,
            "database": database_kind,
            "includes_workspaces": include_workspaces,
            "members": members,
        }
        (root / "backup.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        temporary_output = output.with_name(f".{output.name}.tmp")
        temporary_output.unlink(missing_ok=True)
        with tarfile.open(temporary_output, "w:gz") as archive:
            archive.add(root, arcname="devcloud-backup")
        os.chmod(temporary_output, 0o600)
        os.replace(temporary_output, output)
    return output


def _validated_backup(extracted: Path) -> tuple[Path, dict]:
    root = extracted / "devcloud-backup"
    manifest_path = root / "backup.json"
    if not manifest_path.is_file():
        raise InstallerError("Backup manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallerError("Backup manifest is invalid") from exc
    if manifest.get("format") not in {1, BACKUP_FORMAT}:
        raise InstallerError("Backup format is not supported")
    records = manifest.get("members")
    if not isinstance(records, list):
        raise InstallerError("Backup manifest has no member index")
    listed: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise InstallerError("Backup manifest contains an invalid member")
        relative = Path(str(record.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise InstallerError("Backup manifest contains an unsafe path")
        relative_name = relative.as_posix()
        if relative_name in listed:
            raise InstallerError(f"Backup manifest repeats a member: {relative}")
        listed.add(relative_name)
        path = root / relative
        if (
            not path.is_file()
            or path.stat().st_size != record.get("size")
            or _sha256(path) != record.get("sha256")
        ):
            raise InstallerError(f"Backup member verification failed: {relative}")
        mode = record.get("mode", 0o600)
        if not isinstance(mode, int) or mode < 0 or mode > 0o777:
            raise InstallerError(f"Backup member has an unsafe mode: {relative}")
        os.chmod(path, mode)
    actual = {
        path.relative_to(root).as_posix()
        for path in (root / "payload").rglob("*")
        if path.is_file()
    }
    unlisted = sorted(actual - listed)
    if unlisted:
        raise InstallerError(
            "Backup contains unlisted member(s): " + ", ".join(unlisted[:3])
        )
    return root, manifest


def restore_backup(
    *,
    config: InstallConfig,
    archive: Path,
    host_path,
    runner: CommandRunner,
) -> None:
    if runner.dry_run:
        runner.run(["devcloud-internal-restore", str(archive.resolve())])
        return
    if not archive.is_file():
        raise InstallerError(f"Backup archive does not exist: {archive}")
    with tempfile.TemporaryDirectory(prefix="devcloud-restore-") as temporary:
        extracted = Path(temporary)
        _extract_tar(archive, extracted)
        root, manifest = _validated_backup(extracted)
        if manifest.get("role") != config.role.value:
            raise InstallerError(
                f"Backup role {manifest.get('role')} does not match installed "
                f"role {config.role.value}"
            )
        payload = root / "payload"
        restored_etc = payload / "etc-devcloud"
        if restored_etc.is_dir():
            target = host_path("/etc/devcloud")
            target.mkdir(parents=True, exist_ok=True)
            _copy_tree(restored_etc, target)
        restored_state = payload / "installer" / "install-state.json"
        if restored_state.is_file():
            state_target = host_path(config.state_root) / "install-state.json"
            state_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(restored_state, state_target)

        database_kind = manifest.get("database")
        if database_kind == "sqlite":
            source_db = payload / "database" / "devcloud.db"
            target_db = host_path(
                _sqlite_path(config.effective_database_url())
            )
            target_db.parent.mkdir(parents=True, exist_ok=True)
            temporary_db = target_db.with_suffix(".restore")
            shutil.copy2(source_db, temporary_db)
            os.replace(temporary_db, target_db)
        elif database_kind == "postgresql":
            controller_env = host_path("/etc/devcloud/controller.env")
            database_url = ""
            for raw in controller_env.read_text(encoding="utf-8").splitlines():
                if raw.startswith("DATABASE_URL="):
                    database_url = raw.split("=", 1)[1].strip().strip('"')
            if not database_url:
                raise InstallerError("Restored PostgreSQL URL is missing")
            cli_url, pg_environment = _postgres_cli_connection(database_url)
            runner.run(
                [
                    "pg_restore",
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--dbname",
                    cli_url,
                    str(payload / "database" / "devcloud.pgdump"),
                ],
                env=pg_environment,
            )

        restored_workspaces = payload / "workspaces"
        if restored_workspaces.is_dir() and config.installs_worker:
            target = host_path(config.workspace_root)
            previous = target.with_name(
                f"{target.name}.pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            )
            if target.exists():
                os.replace(target, previous)
            target.mkdir(parents=True)
            _copy_tree(restored_workspaces, target)
