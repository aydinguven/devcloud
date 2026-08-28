from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class DeploymentRole(StrEnum):
    CONTROLLER = "controller"
    ALL_IN_ONE = "all-in-one"
    WORKER = "worker"


class DatabaseMode(StrEnum):
    SQLITE = "sqlite"
    BUNDLED_POSTGRESQL = "bundled-postgresql"
    EXTERNAL_POSTGRESQL = "external-postgresql"


class RegistryMode(StrEnum):
    EXTERNAL = "external"
    PRELOADED = "preloaded"


class ControllerRuntime(StrEnum):
    CONTAINER = "container"
    NATIVE = "native"


@dataclass(slots=True)
class InstallConfig:
    role: DeploymentRole
    public_url: str = "http://127.0.0.1"
    controller_url: str = "http://127.0.0.1:8000"
    database_mode: DatabaseMode = DatabaseMode.SQLITE
    database_url: str = ""
    registry_mode: RegistryMode = RegistryMode.PRELOADED
    registry_url: str = ""
    controller_runtime: ControllerRuntime = ControllerRuntime.CONTAINER
    service_user: str = "devcloud"
    worker_name: str = ""
    worker_id: str = ""
    enrollment_token_file: str = ""
    workspace_root: str = "/var/lib/devcloud/workspaces"
    install_root: str = "/opt/devcloud"
    state_root: str = "/var/lib/devcloud/installer"
    releases_root: str = "/var/lib/devcloud/releases"
    downloads_root: str = "/srv/devcloud-downloads"
    labels: dict[str, str] = field(default_factory=dict)
    preload_images: bool = False
    enable_tls: bool = False
    tls_hostname: str = ""
    certificate_file: str = ""
    private_key_file: str = ""
    admin_username: str = "admin"
    admin_email: str = "admin@example.com"
    admin_password: str = ""

    @property
    def installs_controller(self) -> bool:
        return self.role in {DeploymentRole.CONTROLLER, DeploymentRole.ALL_IN_ONE}

    @property
    def installs_worker(self) -> bool:
        return self.role in {DeploymentRole.WORKER, DeploymentRole.ALL_IN_ONE}

    @property
    def containerized_controller(self) -> bool:
        return (
            self.installs_controller
            and self.controller_runtime == ControllerRuntime.CONTAINER
        )

    def effective_database_url(self) -> str:
        if self.database_mode == DatabaseMode.SQLITE:
            return "sqlite+aiosqlite:////var/lib/devcloud/database/devcloud.db"
        if self.database_mode == DatabaseMode.BUNDLED_POSTGRESQL:
            if self.controller_runtime == ControllerRuntime.CONTAINER:
                if not self.database_url:
                    raise ValueError(
                        "Containerized PostgreSQL requires its generated database URL"
                    )
                return self.database_url
            return self.database_url or (
                "postgresql+asyncpg://devcloud@/devcloud?host=/var/run/postgresql"
            )
        if not self.database_url:
            raise ValueError("External PostgreSQL requires database_url")
        if self.database_url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + self.database_url[len("postgresql://") :]
        return self.database_url

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["role"] = self.role.value
        data["database_mode"] = self.database_mode.value
        data["registry_mode"] = self.registry_mode.value
        data["controller_runtime"] = self.controller_runtime.value
        data.pop("enrollment_token_file", None)
        data.pop("database_url", None)
        data.pop("private_key_file", None)
        data.pop("admin_password", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InstallConfig":
        values = dict(data)
        values["role"] = DeploymentRole(values["role"])
        values["database_mode"] = DatabaseMode(
            values.get("database_mode", DatabaseMode.SQLITE)
        )
        values["registry_mode"] = RegistryMode(
            values.get("registry_mode", RegistryMode.PRELOADED)
        )
        # State written before container support always represents a native
        # controller and must not silently change runtime during repair/update.
        values["controller_runtime"] = ControllerRuntime(
            values.get("controller_runtime", ControllerRuntime.NATIVE)
        )
        return cls(**values)

    @classmethod
    def from_json_file(cls, path: Path) -> "InstallConfig":
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("The answer file must contain a JSON object")
        return cls.from_dict(loaded)
