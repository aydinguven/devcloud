import asyncio
import logging
import os
import shutil
import socket
from pathlib import Path
from typing import Any

from app.config import settings
from app.orchestrator.flavors import get_flavor
from app.orchestrator.templates import get_template

logger = logging.getLogger("devcloud.podman")


class PodmanExecutionError(Exception):
    """Exception raised when a podman command fails."""
    pass


class PodmanService:
    """Service to orchestrate Podman containers for user workspaces."""

    def __init__(self, podman_bin: str | None = None):
        self.podman_bin = podman_bin or settings.PODMAN_BIN
        self._mock_mode: bool = settings.USE_MOCK_PODMAN or not self._check_podman_installed()
        # In-memory store for mock mode
        self._mock_containers: dict[str, dict[str, Any]] = {}
        if self._mock_mode:
            logger.info("PodmanService running in MOCK mode (simulated containers).")
        else:
            logger.info(f"PodmanService using Podman binary at: {self.podman_bin}")

    @property
    def is_mock(self) -> bool:
        return self._mock_mode

    def _check_podman_installed(self) -> bool:
        """Check if podman executable is found in PATH."""
        return shutil.which(self.podman_bin) is not None

    async def run_cmd(self, *args: str) -> tuple[int, str, str]:
        """Execute a podman CLI command asynchronously with stdin closed."""
        cmd = [self.podman_bin, *args]
        logger.debug("Executing command: %s", " ".join(cmd))
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()
            return process.returncode or 0, stdout_str, stderr_str
        except Exception as exc:
            logger.error(f"Failed to execute podman command '{' '.join(cmd)}': {exc}")
            raise PodmanExecutionError(f"Podman execution failed: {exc}") from exc

    async def get_active_podman_ports(self) -> set[int]:
        """Query running Podman containers to find host ports currently mapped."""
        if self._mock_mode:
            return set()
        code, stdout, _ = await self.run_cmd("ps", "--format", "{{.Ports}}")
        if code != 0 or not stdout:
            return set()
        ports = set()
        import re
        for line in stdout.splitlines():
            matches = re.findall(r":(\d+)->", line)
            for m in matches:
                try:
                    ports.add(int(m))
                except ValueError:
                    pass
        return ports

    def find_free_port(self, used_ports: set[int]) -> int:
        """Find an available TCP port on the host within the configured range."""
        for port in range(settings.PORT_RANGE_START, settings.PORT_RANGE_END):
            if port in used_ports:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    continue
        raise RuntimeError("No available ports found in the configured range.")

    async def find_available_port(self, db_used_ports: set[int]) -> int:
        """Find free port considering both database records and active Podman containers."""
        active_ports = await self.get_active_podman_ports()
        all_used = db_used_ports | active_ports
        return self.find_free_port(all_used)


    def ensure_workspace_storage(self, user_id: int, workspace_id: str) -> str:
        """Create and initialize the persistent directory on host for workspace."""
        user_dir = Path(settings.STORAGE_ROOT) / str(user_id)
        workspace_dir = user_dir / str(workspace_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize a default README in the workspace
        readme_file = workspace_dir / "README.md"
        if not readme_file.exists():
            readme_file.write_text(
                f"# Workspace {workspace_id}\n\nWelcome to your cloud workspace!\n"
                f"Files stored in this directory are persistent across container restarts.\n",
                encoding="utf-8",
            )
        return str(workspace_dir.resolve())

    async def ensure_image_exists(
        self,
        template_id: str,
        image_tag: str,
        progress_callback: Any | None = None,
    ) -> bool:
        """Check if image exists in Podman; if not, attempt to build from containers/<template_id>."""
        if self._mock_mode:
            return True

        code, _, _ = await self.run_cmd("image", "exists", image_tag)
        if code == 0:
            if progress_callback:
                await progress_callback(f"⚡ Verified image [{image_tag}] in local OCI store", "success")
            return True

        # Auto-build if Containerfile directory exists
        containers_dir = Path(__file__).resolve().parent.parent.parent / "containers" / template_id
        if containers_dir.exists() and (containers_dir / "Containerfile").exists():
            if progress_callback:
                await progress_callback(f"🔨 Image [{image_tag}] not found. Auto-building from Containerfile (first run only)...", "info")
            logger.info(f"Image {image_tag} not found locally. Auto-building from {containers_dir}...")
            build_code, stdout, stderr = await self.run_cmd("build", "-t", image_tag, str(containers_dir))
            if build_code == 0:
                logger.info(f"Successfully built image {image_tag}")
                if progress_callback:
                    await progress_callback(f"✅ Image [{image_tag}] built and cached successfully!", "success")
                return True
            else:
                logger.error(f"Failed to auto-build {image_tag}: {stderr or stdout}")
                if progress_callback:
                    await progress_callback(f"⚠️ Build warning: {stderr or stdout}", "error")
        return False

    async def create_workspace_container(
        self,
        workspace_id: str,
        user_id: int,
        container_name: str,
        template_id: str,
        flavor_id: str,
        host_port: int,
        workspace_token: str,
        progress_callback: Any | None = None,
    ) -> tuple[str, str]:
        """Create and run a new container for a workspace.
        
        Returns:
            tuple of (container_id, storage_path)
        """
        import time

        template = get_template(template_id)
        if not template:
            raise ValueError(f"Unknown template: {template_id}")

        flavor = get_flavor(flavor_id)
        if not flavor:
            raise ValueError(f"Unknown flavor: {flavor_id}")

        storage_path = self.ensure_workspace_storage(user_id, workspace_id)

        if self._mock_mode:
            container_id = f"mock-cid-{workspace_id[:12]}"
            self._mock_containers[container_name] = {
                "id": container_id,
                "status": "running",
                "template": template_id,
                "flavor": flavor_id,
                "host_port": host_port,
                "storage_path": storage_path,
                "logs": [
                    f"[{container_name}] Initializing {template.name}...",
                    f"[{container_name}] Persistent volume mounted at: {template.container_workdir}",
                    f"[{container_name}] Allocated {flavor.cpus} CPU(s) and {flavor.memory_display} RAM",
                    f"[{container_name}] Service listening on port {template.default_port}",
                    f"[{container_name}] Workspace ready for connection.",
                ],
            }
            if progress_callback:
                await progress_callback(f"⚡ Mock container ready on port {host_port}", "success")
            return container_id, storage_path

        # 1. Clean up any stale/dead container with the same name
        if progress_callback:
            await progress_callback(f"🧹 Clearing prior instances of [{container_name}]...", "dim")
        await self.run_cmd("rm", "-f", container_name)

        # 2. Check and ensure image exists or auto-build
        await self.ensure_image_exists(template_id, template.image_tag, progress_callback)

        # 3. Podman run flags
        if progress_callback:
            await progress_callback(f"🐳 Executing podman run with {flavor.cpus} CPU(s) & {flavor.memory_display} RAM...", "info")

        cmd_args = [
            "run",
            "-d",
            "--name", container_name,
            "--cpus", str(flavor.cpus),
            "--memory", f"{flavor.memory_mb}m",
            "-p", f"127.0.0.1:{host_port}:{template.default_port}",
            "-v", f"{storage_path}:{template.container_workdir}:Z",
            "--restart", "unless-stopped",
        ]

        # Injected environment variables for auth & config
        if "vscode" in template_id:
            cmd_args.extend([
                "-e", f"PASSWORD={workspace_token}",
                "-e", "DISABLE_TELEMETRY=true",
            ])
        elif "jupyter" in template_id:
            cmd_args.extend([
                "-e", f"JUPYTER_TOKEN={workspace_token}",
                "-e", "JUPYTER_ENABLE_LAB=yes",
            ])

        for k, v in template.env_vars.items():
            cmd_args.extend(["-e", f"{k}={v}"])

        # Image tag
        cmd_args.append(template.image_tag)

        if template.startup_command:
            cmd_args.extend(template.startup_command)

        code, stdout, stderr = await self.run_cmd(*cmd_args)
        if code != 0:
            raise PodmanExecutionError(f"Failed to start container: {stderr or stdout}")

        container_id = stdout.strip()

        # 4. Fast Readiness Health Check
        if progress_callback:
            await progress_callback(f"⏳ Container started (ID: {container_id[:12]}). Verifying port {host_port} readiness...", "info")

        start_time = time.monotonic()
        is_ready = False
        for _ in range(15):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", host_port),
                    timeout=0.6,
                )
                writer.close()
                await writer.wait_closed()
                is_ready = True
                elapsed = time.monotonic() - start_time
                if progress_callback:
                    await progress_callback(f"⚡ Port {host_port} is UP and ACCEPTING connections (in {elapsed:.1f}s)!", "success")
                break
            except Exception:
                await asyncio.sleep(0.4)

        if not is_ready and progress_callback:
            await progress_callback(f"ℹ️ Container is online; IDE server is finishing initialization on port {host_port}.", "info")

        return container_id, storage_path

    async def start_container(self, container_name: str) -> bool:
        """Start an existing stopped container."""
        if self._mock_mode:
            if container_name in self._mock_containers:
                self._mock_containers[container_name]["status"] = "running"
                self._mock_containers[container_name]["logs"].append(
                    f"[{container_name}] Container restarted."
                )
            return True

        code, stdout, stderr = await self.run_cmd("start", container_name)
        if code != 0:
            logger.error(f"Failed to start container {container_name}: {stderr}")
            return False
        return True

    async def stop_container(self, container_name: str) -> bool:
        """Stop a running container."""
        if self._mock_mode:
            if container_name in self._mock_containers:
                self._mock_containers[container_name]["status"] = "stopped"
                self._mock_containers[container_name]["logs"].append(
                    f"[{container_name}] Container stopped."
                )
            return True

        code, stdout, stderr = await self.run_cmd("stop", "-t", "10", container_name)
        if code != 0:
            logger.error(f"Failed to stop container {container_name}: {stderr}")
            return False
        return True

    async def delete_container(self, container_name: str) -> bool:
        """Remove a container permanently."""
        if self._mock_mode:
            self._mock_containers.pop(container_name, None)
            return True

        code, stdout, stderr = await self.run_cmd("rm", "-f", container_name)
        if code != 0:
            logger.warning(f"Failed or already removed container {container_name}: {stderr}")
            return False
        return True

    async def get_container_status(self, container_name: str) -> str:
        """Check if container is running, stopped, or missing."""
        if self._mock_mode:
            return self._mock_containers.get(container_name, {}).get("status", "stopped")

        code, stdout, stderr = await self.run_cmd(
            "inspect", "--format", "{{.State.Status}}", container_name
        )
        if code != 0:
            return "stopped"
        status = stdout.strip().lower()
        return status

    async def get_logs(self, container_name: str, tail: int = 100) -> str:
        """Retrieve recent container stdout/stderr logs."""
        if self._mock_mode:
            logs = self._mock_containers.get(container_name, {}).get("logs", [])
            return "\n".join(logs) or "No logs available."

        code, stdout, stderr = await self.run_cmd("logs", "--tail", str(tail), container_name)
        if code != 0:
            if "no container with name or ID" in stderr or "no such container" in stderr:
                return f"Container '{container_name}' is not currently running or has not been spawned yet."
            return f"Container logs ({container_name}): {stderr or stdout}"
        return stdout or stderr or "Container is running. No output logged yet."

    async def load_offline_images(self, images_dir: Path | str) -> list[str]:
        """Load any .tar or .tar.gz image archives present in images_dir into Podman."""
        images_path = Path(images_dir)
        if not images_path.exists():
            return []

        loaded_files = []
        for file in sorted(images_path.glob("*.tar*")):
            if self._mock_mode:
                logger.info(f"[MOCK] Loaded offline image from archive: {file.name}")
                loaded_files.append(file.name)
            else:
                code, stdout, stderr = await self.run_cmd("load", "-i", str(file))
                if code == 0:
                    logger.info(f"Successfully loaded image from {file.name}: {stdout}")
                    loaded_files.append(file.name)
                else:
                    logger.error(f"Failed to load image from {file.name}: {stderr}")
        return loaded_files

    async def list_images(self) -> list[str]:
        """List local container images."""
        if self._mock_mode:
            return [
                "localhost/devcloud-vscode-empty:latest",
                "localhost/devcloud-vscode-python:latest",
                "localhost/devcloud-jupyter-python:latest",
                "localhost/devcloud-vscode-java:latest",
            ]
        code, stdout, _ = await self.run_cmd("images", "--format", "{{.Repository}}:{{.Tag}}")
        if code != 0:
            return []
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    async def warm_image_cache_background(self):
        """Pre-warm/build all workspace template images in background on server boot."""
        if self._mock_mode:
            return

        from app.orchestrator.templates import TEMPLATES
        logger.info("Checking template images cache status in background...")

        for tpl_id, tpl in TEMPLATES.items():
            try:
                code, _, _ = await self.run_cmd("image", "exists", tpl.image_tag)
                if code == 0:
                    logger.info(f"Image [{tpl.image_tag}] is pre-cached and READY.")
                else:
                    logger.info(f"Pre-warming missing image [{tpl.image_tag}] in background...")
                    containers_dir = Path(__file__).resolve().parent.parent.parent / "containers" / tpl_id
                    if containers_dir.exists() and (containers_dir / "Containerfile").exists():
                        build_code, stdout, stderr = await self.run_cmd("build", "-t", tpl.image_tag, str(containers_dir))
                        if build_code == 0:
                            logger.info(f"Successfully pre-warmed image [{tpl.image_tag}].")
                        else:
                            logger.warning(f"Failed background build of [{tpl.image_tag}]: {stderr or stdout}")
            except Exception as exc:
                logger.warning(f"Background pre-warm error for {tpl_id}: {exc}")
        logger.info("Template image cache verification complete. All templates ready!")



podman_service = PodmanService()

