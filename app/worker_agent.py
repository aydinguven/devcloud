"""Outbound-only DevCloud worker agent.

Run with DEVCLOUD_CONTROLLER_URL, DEVCLOUD_NODE_ID and DEVCLOUD_NODE_TOKEN set.
The legacy DEVCLOUD_MASTER_URL name remains an upgrade compatibility fallback.
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import socket
import ssl
import tempfile
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets

from app import __version__
from app.config import settings
from app.orchestrator.podman_service import podman_service
from app.release_catalog import semantic_version
from app.worker_gpu import discover_nvidia_capabilities

logger = logging.getLogger("devcloud.worker")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} ayarlanmalıdır.")
    return value


def _connection_url() -> str:
    base = (
        os.environ.get("DEVCLOUD_CONTROLLER_URL", "").strip()
        or os.environ.get("DEVCLOUD_MASTER_URL", "").strip()
    ).rstrip("/")
    if not base:
        raise RuntimeError("DEVCLOUD_CONTROLLER_URL ayarlanmalıdır.")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    if not base.startswith(("ws://", "wss://")):
        raise RuntimeError("DEVCLOUD_CONTROLLER_URL http:// veya https:// ile başlamalıdır.")
    return f"{base}/api/agent/connect/{_required_env('DEVCLOUD_NODE_ID')}"


def _controller_http_url() -> str:
    base = (
        os.environ.get("DEVCLOUD_CONTROLLER_URL", "").strip()
        or os.environ.get("DEVCLOUD_MASTER_URL", "").strip()
    ).rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise RuntimeError("DEVCLOUD_CONTROLLER_URL http:// veya https:// ile başlamalıdır.")
    return base


def _tls_context(connection_url: str) -> ssl.SSLContext | None:
    if not connection_url.startswith("wss://"):
        return None
    ca_file = os.environ.get("DEVCLOUD_AGENT_CA_FILE", "").strip() or None
    context = ssl.create_default_context(cafile=ca_file)
    cert_file = os.environ.get("DEVCLOUD_AGENT_CERT_FILE", "").strip()
    key_file = os.environ.get("DEVCLOUD_AGENT_KEY_FILE", "").strip()
    if bool(cert_file) != bool(key_file):
        raise RuntimeError("Agent client sertifikası için cert ve key birlikte ayarlanmalıdır.")
    if cert_file:
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return context


class WorkerAgent:
    def __init__(self):
        self.websocket = None
        self.send_lock = asyncio.Lock()
        self.registry_lock = asyncio.Lock()
        self.image_sync_lock = asyncio.Lock()
        self.upgrade_task: asyncio.Task | None = None
        self.stream_targets: dict[str, object] = {}
        self.registry_path = Path(settings.STORAGE_ROOT) / ".devcloud-agent-registry.json"
        self.registry = self._load_registry()
        self.image_state_path = Path(settings.STORAGE_ROOT) / ".devcloud-image-state.json"
        self.image_state = self._load_image_state()
        self.image_progress: dict[str, dict] = {}
        self.upgrade_status: dict[str, str] = {
            "state": "idle",
            "target_version": "",
            "message": "",
        }

    def _load_registry(self) -> dict:
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.registry_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.registry, indent=2), encoding="utf-8")
        temporary.replace(self.registry_path)

    def _load_image_state(self) -> dict[str, dict]:
        try:
            data = json.loads(self.image_state_path.read_text(encoding="utf-8"))
            return {
                str(key): value
                for key, value in data.items()
                if isinstance(key, str) and isinstance(value, dict)
            } if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save_image_state(self) -> None:
        self.image_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.image_state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.image_state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.image_state_path)

    def _set_upgrade_status(
        self,
        state: str,
        *,
        target_version: str = "",
        message: str = "",
    ) -> None:
        self.upgrade_status = {
            "state": state,
            "target_version": target_version,
            "message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _reported_upgrade_status(self) -> dict[str, str]:
        if self.upgrade_status.get("state") in {"preparing", "downloading", "failed"}:
            return dict(self.upgrade_status)
        queue_root = Path(settings.UPDATE_QUEUE_ROOT).resolve()
        for filename, fallback_state in (
            ("running.json", "running"),
            ("pending.json", "queued"),
            ("status.json", "idle"),
        ):
            try:
                value = json.loads(
                    (queue_root / filename).read_text(encoding="utf-8")
                )
            except (FileNotFoundError, ValueError, OSError):
                continue
            if not isinstance(value, dict):
                continue
            state = str(value.get("state") or fallback_state)
            target_version = str(value.get("target_version") or "")
            message = str(value.get("error") or value.get("message") or "").strip()
            if not message and state == "failed":
                output_lines = [
                    line.strip()
                    for line in str(value.get("output") or "").splitlines()
                    if line.strip()
                ]
                message = "\n".join(output_lines[-12:])[-2000:]
            if state == "failed" and target_version == __version__:
                state = "succeeded"
                message = (
                    f"Worker zaten hedef sürümde (v{__version__}). "
                    "Önceki aynı-sürüm güncelleme hatası kapatıldı."
                )
            return {
                "state": state,
                "target_version": target_version,
                "message": message,
                "return_code": str(value.get("return_code") or ""),
                "updated_at": str(
                    value.get("finished_at")
                    or value.get("started_at")
                    or value.get("queued_at")
                    or ""
                ),
            }
        return dict(self.upgrade_status)

    @staticmethod
    def _http_verify_context(base_url: str):
        if not base_url.startswith("https://"):
            return True
        ca_file = os.environ.get("DEVCLOUD_AGENT_CA_FILE", "").strip() or None
        context = ssl.create_default_context(cafile=ca_file)
        cert_file = os.environ.get("DEVCLOUD_AGENT_CERT_FILE", "").strip()
        key_file = os.environ.get("DEVCLOUD_AGENT_KEY_FILE", "").strip()
        if bool(cert_file) != bool(key_file):
            raise RuntimeError(
                "Agent client sertifikası için cert ve key birlikte ayarlanmalıdır."
            )
        if cert_file:
            context.load_cert_chain(certfile=cert_file, keyfile=key_file)
        return context

    async def sync_workspace_images(self) -> list[dict]:
        """Reconcile enabled controller images into the worker's Podman store."""
        async with self.image_sync_lock:
            return await self._sync_workspace_images()

    async def _sync_workspace_images(self) -> list[dict]:
        base_url = _controller_http_url()
        node_id = _required_env("DEVCLOUD_NODE_ID")
        headers = {"Authorization": f"Bearer {_required_env('DEVCLOUD_NODE_TOKEN')}"}
        timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            verify=self._http_verify_context(base_url),
            follow_redirects=False,
        ) as client:
            response = await client.get(
                f"{base_url}/api/agent/images/catalog",
                params={"node_id": node_id},
            )
            response.raise_for_status()
            catalog = response.json().get("images", [])
            if not isinstance(catalog, list):
                raise RuntimeError("Controller workspace image catalog is invalid")

            desired_ids: set[str] = set()
            cache_root = Path(settings.STORAGE_ROOT) / ".devcloud-image-cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            for item in catalog:
                if not isinstance(item, dict):
                    continue
                image_id = str(item.get("id") or "")
                image_ref = str(item.get("image_ref") or "")
                expected_sha256 = str(item.get("sha256") or "")
                expected_size = int(item.get("size") or 0)
                image_url = str(item.get("url") or "")
                if image_url.startswith("/"):
                    download_url = f"{base_url}{image_url}"
                elif image_url.startswith(f"{base_url}/api/agent/images/"):
                    download_url = image_url
                else:
                    download_url = ""
                try:
                    safe_image_id = str(uuid.UUID(image_id)) == image_id
                except ValueError:
                    safe_image_id = False
                if (
                    not safe_image_id
                    or not image_ref
                    or len(expected_sha256) != 64
                    or any(character not in "0123456789abcdef" for character in expected_sha256)
                    or expected_size <= 0
                    or not download_url
                ):
                    raise RuntimeError("Controller returned unsafe workspace image metadata")
                desired_ids.add(image_id)
                self.image_progress[image_id] = {
                    "id": image_id,
                    "image_ref": image_ref,
                    "sha256": expected_sha256,
                    "state": "queued",
                    "downloaded_bytes": 0,
                    "total_bytes": expected_size,
                    "error": "",
                }
                current = self.image_state.get(image_id, {})
                exists_code, _, _ = await podman_service.run_cmd(
                    "image", "exists", image_ref, timeout=30
                )
                if current.get("sha256") == expected_sha256 and exists_code == 0:
                    self.image_progress[image_id].update(
                        state="ready", downloaded_bytes=expected_size
                    )
                    continue

                temporary = cache_root / f"{image_id}.partial"
                digest = hashlib.sha256()
                downloaded = 0
                try:
                    self.image_progress[image_id]["state"] = "downloading"
                    async with client.stream("GET", download_url) as download:
                        download.raise_for_status()
                        with temporary.open("wb") as destination:
                            async for chunk in download.aiter_bytes(1024 * 1024):
                                downloaded += len(chunk)
                                self.image_progress[image_id][
                                    "downloaded_bytes"
                                ] = downloaded
                                if downloaded > expected_size:
                                    raise RuntimeError(
                                        f"Workspace image {image_ref} exceeded its catalog size"
                                    )
                                digest.update(chunk)
                                destination.write(chunk)
                    self.image_progress[image_id]["state"] = "verifying"
                    if downloaded != expected_size or digest.hexdigest() != expected_sha256:
                        raise RuntimeError(
                            f"Workspace image verification failed for {image_ref}"
                        )
                    self.image_progress[image_id]["state"] = "loading"
                    code, stdout, stderr = await podman_service.run_cmd(
                        "load", "-i", str(temporary), timeout=1800
                    )
                    if code != 0:
                        raise RuntimeError(
                            f"Podman could not load {image_ref}: {stderr or stdout}"
                        )
                    exists_code, _, _ = await podman_service.run_cmd(
                        "image", "exists", image_ref, timeout=30
                    )
                    if exists_code != 0:
                        raise RuntimeError(
                            f"Loaded archive did not provide expected image {image_ref}"
                        )
                    self.image_state[image_id] = {
                        "id": image_id,
                        "template_id": str(item.get("template_id") or ""),
                        "image_ref": image_ref,
                        "digest": str(item.get("digest") or ""),
                        "sha256": expected_sha256,
                        "size": expected_size,
                    }
                    self._save_image_state()
                    self.image_progress[image_id].update(
                        state="ready", downloaded_bytes=expected_size, error=""
                    )
                    logger.info("Workspace image synchronized: %s", image_ref)
                except Exception as exc:
                    self.image_progress[image_id].update(
                        state="failed", error=str(exc)[:500]
                    )
                    raise
                finally:
                    temporary.unlink(missing_ok=True)

            stale = set(self.image_state) - desired_ids
            if stale:
                for image_id in stale:
                    self.image_state.pop(image_id, None)
                self._save_image_state()
            for image_id in set(self.image_progress) - desired_ids:
                self.image_progress.pop(image_id, None)
            return [self.image_state[key] for key in sorted(self.image_state)]

    async def image_sync_loop(self) -> None:
        while True:
            try:
                await self.sync_workspace_images()
            except Exception as exc:
                logger.warning("Workspace image synchronization failed: %s", exc)
            await asyncio.sleep(30)

    async def send(self, message: dict) -> None:
        async with self.send_lock:
            await self.websocket.send(json.dumps(message, ensure_ascii=False))

    async def result(self, request_id: str, payload: dict | None = None, error: str | None = None):
        await self.send(
            {
                "type": "result",
                "request_id": request_id,
                "ok": error is None,
                "payload": payload or {},
                "error": error,
            }
        )

    async def heartbeat(self) -> None:
        while True:
            workspace_images = [
                self.image_state[key] for key in sorted(self.image_state)
            ]
            disk = shutil.disk_usage(settings.STORAGE_ROOT)
            memory_kb = 0
            mem_avail_kb = 0
            cpu_pct = 0.0
            try:
                import psutil
                cpu_pct = float(psutil.cpu_percent(interval=None))
                mem = psutil.virtual_memory()
                memory_kb = mem.total // 1024
                mem_avail_kb = mem.available // 1024
            except Exception:
                try:
                    for line in Path("/proc/meminfo").read_text().splitlines():
                        if line.startswith("MemTotal:"):
                            memory_kb = int(line.split()[1])
                        elif line.startswith("MemAvailable:"):
                            mem_avail_kb = int(line.split()[1])
                except OSError:
                    pass

            mem_total_mb = memory_kb // 1024
            mem_used_mb = max(0, mem_total_mb - (mem_avail_kb // 1024))
            disk_total_mb = disk.total // (1024 * 1024)
            disk_used_mb = max(0, (disk.total - disk.free) // (1024 * 1024))
            inventory = []
            for container_name, entry in self.registry.items():
                if not isinstance(entry, dict):
                    continue
                inventory.append(
                    {
                        "workspace_id": str(entry.get("workspace_id") or ""),
                        "container_name": container_name,
                        "host_port": int(entry.get("host_port") or 0),
                        "storage_path": str(entry.get("storage_path") or ""),
                        "status": await podman_service.get_container_status(
                            container_name
                        ),
                    }
                )
            active_cnt = len(inventory)
            accelerator_capabilities = await asyncio.to_thread(
                discover_nvidia_capabilities
            )

            await self.send(
                {
                    "type": "heartbeat",
                    "payload": {
                        "hostname": socket.gethostname(),
                        "cpu_total": float(os.cpu_count() or 0),
                        "memory_total_mb": mem_total_mb,
                        "disk_total_mb": disk_total_mb,
                        "cpu_percent": round(cpu_pct, 1),
                        "memory_used_mb": mem_used_mb,
                        "disk_used_mb": disk_used_mb,
                        "active_containers_count": active_cnt,
                        "capabilities": {
                            "runtime": "podman",
                            **accelerator_capabilities,
                            "upgrade": self._reported_upgrade_status(),
                            "workspace_images": workspace_images,
                            "workspace_image_sync": [
                                self.image_progress[key]
                                for key in sorted(self.image_progress)
                            ],
                        },
                        "inventory": inventory,
                        "agent_version": __version__,
                    },
                }
            )
            await asyncio.sleep(20)

    def _registered_storage(self, container_name: str) -> str:
        entry = self._registered_entry(container_name)
        return str(entry.get("storage_path") or "")

    def _registered_entry(self, container_name: str, workspace_id: str = "") -> dict:
        entry = self.registry.get(container_name)
        if not isinstance(entry, dict):
            raise PermissionError("Container bu worker'a kayıtlı değil.")
        if workspace_id and str(entry.get("workspace_id") or "") != workspace_id:
            raise PermissionError("Workspace ve container eşleşmesi doğrulanamadı.")
        return entry

    def _safe_workspace_path(self, container_name: str, relative_path: str = "") -> Path:
        storage_path = self._registered_storage(container_name)
        if not storage_path:
            raise FileNotFoundError("Workspace storage kaydı bulunamadı.")
        root = Path(storage_path).resolve()
        target = (root / str(relative_path).lstrip("/\\")).resolve()
        if target != root and root not in target.parents:
            raise PermissionError("Workspace dışındaki dosyalara erişilemez.")
        return target

    async def handle_container_command(self, action: str, payload: dict) -> dict:
        name = str(payload.get("container_name") or "")
        if action == "container.create":
            allowed = {
                "workspace_id", "user_id", "container_name", "template_id",
                "flavor_id", "host_port", "workspace_token",
                "accelerator_cdi_name",
            }
            args = {key: value for key, value in payload.items() if key in allowed}
            async with self.registry_lock:
                existing = self.registry.get(args["container_name"])
                if isinstance(existing, dict):
                    if str(existing.get("workspace_id") or "") != str(args["workspace_id"]):
                        raise PermissionError(
                            "Container adı başka bir workspace için kayıtlı."
                        )
                    if int(existing.get("host_port") or -1) != int(args["host_port"]):
                        raise PermissionError(
                            "Tekrarlanan create isteğinin host portu kayıtla eşleşmiyor."
                        )
                    if str(existing.get("accelerator_cdi_name") or "") != str(
                        args.get("accelerator_cdi_name") or ""
                    ):
                        raise PermissionError(
                            "Tekrarlanan create isteğinin GPU tahsisi kayıtla eşleşmiyor."
                        )
                    if await podman_service.container_exists(args["container_name"]):
                        return {
                            "container_id": str(existing.get("container_id") or ""),
                            "storage_path": str(existing.get("storage_path") or ""),
                            "reused": True,
                        }
                container_id, storage_path = await podman_service.create_workspace_container(**args)
                self.registry[args["container_name"]] = {
                    "workspace_id": args["workspace_id"],
                    "container_id": container_id,
                    "storage_path": storage_path,
                    "host_port": args["host_port"],
                    "accelerator_cdi_name": args.get("accelerator_cdi_name") or "",
                }
                self._save_registry()
                return {"container_id": container_id, "storage_path": storage_path}
        if not name:
            raise ValueError("container_name gereklidir.")
        registered = self._registered_entry(name)
        if action == "container.exists":
            return {"exists": await podman_service.container_exists(name)}
        if action == "container.start":
            return {"success": await podman_service.start_container(name)}
        if action == "container.stop":
            return {"success": await podman_service.stop_container(name)}
        if action == "container.status":
            return {"status": await podman_service.get_container_status(name)}
        if action == "container.logs":
            tail = max(1, min(int(payload.get("tail", 100)), 1000))
            return {"logs": await podman_service.get_logs(name, tail=tail)}
        if action == "container.port_ready":
            host_port = int(payload.get("host_port", -1))
            if int(registered.get("host_port", -2)) != host_port:
                raise PermissionError("Workspace port eşleşmesi doğrulanamadı.")
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", host_port), timeout=0.6
                )
            except (OSError, TimeoutError):
                return {"ready": False}
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return {"ready": True}
        if action == "container.stats":
            return await podman_service.get_container_stats(name)
        if action == "container.storage_size":
            storage_path = self._registered_storage(name)
            if not storage_path:
                return {"bytes": 0}
            from app.orchestrator.metrics_service import get_dir_size_bytes
            return {"bytes": get_dir_size_bytes(storage_path)}
        if action == "container.snapshot":
            workspace_id = str(payload.get("workspace_id") or "")
            self._registered_entry(name, workspace_id)
            image_tag = str(payload.get("image_tag") or "").strip()
            if not image_tag:
                raise ValueError("image_tag gereklidir.")
            success = await podman_service.commit_container(name, image_tag)
            if not success:
                raise RuntimeError("Container image snapshot oluşturulamadı.")
            return {"success": True, "image_tag": image_tag}
        if action == "container.delete":
            success = await podman_service.delete_container(name)
            storage_path = self._registered_storage(name)
            if storage_path:
                root = Path(settings.STORAGE_ROOT).resolve()
                target = Path(storage_path).resolve()
                if target != root and root in target.parents:
                    shutil.rmtree(target, ignore_errors=True)
            self.registry.pop(name, None)
            self._save_registry()
            return {"success": success}
        raise ValueError(f"Desteklenmeyen container komutu: {action}")

    async def handle_backup_open(self, request_id: str, payload: dict) -> None:
        """Create a ZIP on the worker and stream it over the existing tunnel."""
        from app.orchestrator.backup_service import create_workspace_zip_backup

        stream_id = str(payload["stream_id"])
        container_name = str(payload.get("container_name") or "")
        workspace_id = str(payload.get("workspace_id") or "")
        entry = self._registered_entry(container_name, workspace_id)
        storage_path = str(entry.get("storage_path") or "")
        if not storage_path or not Path(storage_path).is_dir():
            await self.result(request_id, error="Workspace storage bulunamadı.")
            return
        descriptor, archive_name = tempfile.mkstemp(
            prefix=f"devcloud-{workspace_id[:8]}-", suffix=".zip"
        )
        os.close(descriptor)
        archive = Path(archive_name)
        try:
            create_workspace_zip_backup(storage_path, archive)
            await self.result(
                request_id,
                {
                    "filename": f"{workspace_id}.zip",
                    "size": archive.stat().st_size,
                },
            )
            with archive.open("rb") as handle:
                while chunk := handle.read(256 * 1024):
                    await self.send(
                        {
                            "type": "stream_data",
                            "stream_id": stream_id,
                            "encoding": "base64",
                            "data": base64.b64encode(chunk).decode("ascii"),
                        }
                    )
            await self.send({"type": "stream_end", "stream_id": stream_id})
        except Exception as exc:
            await self.result(request_id, error=str(exc))
            await self.send(
                {"type": "stream_error", "stream_id": stream_id, "error": str(exc)}
            )
        finally:
            archive.unlink(missing_ok=True)

    async def handle_file_command(self, action: str, payload: dict) -> dict:
        name = str(payload.get("container_name") or "")
        target = self._safe_workspace_path(name, str(payload.get("path") or ""))
        root = self._safe_workspace_path(name)
        if action == "files.list":
            if not target.is_dir():
                raise FileNotFoundError("Dizin bulunamadı.")
            items = []
            for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                stat = entry.stat()
                items.append(
                    {
                        "name": entry.name,
                        "path": entry.relative_to(root).as_posix(),
                        "is_dir": entry.is_dir(),
                        "size_bytes": stat.st_size if entry.is_file() else 0,
                        "modified_timestamp": stat.st_mtime,
                    }
                )
            current = target.relative_to(root).as_posix()
            return {"current_path": "" if current == "." else current, "items": items}
        if action == "files.upload":
            target.mkdir(parents=True, exist_ok=True)
            uploaded = []
            for item in payload.get("files") or []:
                filename = Path(str(item.get("name") or "")).name
                if not filename:
                    continue
                (target / filename).write_bytes(base64.b64decode(item.get("content", ""), validate=True))
                uploaded.append(filename)
            return {"files": uploaded}
        if action == "files.download":
            if not target.is_file():
                raise FileNotFoundError("Dosya bulunamadı.")
            return {"name": target.name, "content": base64.b64encode(target.read_bytes()).decode("ascii")}
        if action == "files.mkdir":
            target.mkdir(parents=True, exist_ok=True)
            return {"name": target.name}
        if action == "files.delete":
            if target == root:
                raise PermissionError("Workspace kök dizini silinemez.")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            else:
                raise FileNotFoundError("Dosya veya dizin bulunamadı.")
            return {"name": target.name}
        raise ValueError(f"Desteklenmeyen dosya komutu: {action}")

    async def handle_image_command(self, action: str, payload: dict) -> dict:
        if action != "image.build":
            raise ValueError(f"Desteklenmeyen image komutu: {action}")
        image_tag = str(payload.get("image_tag") or "").strip()
        containerfile = str(payload.get("containerfile") or "")
        if not image_tag or not containerfile:
            raise ValueError("image_tag ve containerfile gereklidir.")
        success, logs = await podman_service.build_image_from_content(
            containerfile_content=containerfile,
            image_tag=image_tag,
        )
        if success and payload.get("push") and not podman_service.is_mock:
            code, stdout, stderr = await podman_service.run_cmd(
                "push", image_tag, timeout=600
            )
            if code != 0:
                return {
                    "success": False,
                    "logs": logs + "\n" + (stderr or stdout),
                }
        return {"success": success, "logs": logs, "image_tag": image_tag}

    async def handle_http_open(self, request_id: str, payload: dict) -> None:
        stream_id = str(payload["stream_id"])
        client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        response = None
        try:
            target_url = await self._target_url(payload, websocket=False)
            body = base64.b64decode(payload.get("body", ""), validate=True)
            request = client.build_request(
                method=payload["method"],
                url=target_url,
                headers=payload.get("headers") or {},
                content=body,
            )
            response = await client.send(request, stream=True)
            headers = list(response.headers.multi_items())
            await self.result(
                request_id,
                {"status_code": response.status_code, "headers": headers},
            )
            async for chunk in response.aiter_raw():
                await self.send(
                    {
                        "type": "stream_data",
                        "stream_id": stream_id,
                        "encoding": "base64",
                        "data": base64.b64encode(chunk).decode("ascii"),
                    }
                )
            await self.send({"type": "stream_end", "stream_id": stream_id})
        except Exception as exc:
            await self.result(request_id, error=str(exc))
            await self.send({"type": "stream_error", "stream_id": stream_id, "error": str(exc)})
        finally:
            if response is not None:
                await response.aclose()
            await client.aclose()

    async def _target_url(self, payload: dict, websocket: bool) -> str:
        scheme = "ws" if websocket else "http"
        if payload.get("custom_port") is not None:
            port = int(payload["custom_port"])
            if not 1 <= port <= 65535:
                raise ValueError("Geçersiz custom port.")
            container_name = str(payload["container_name"])
            self._registered_entry(
                container_name,
                str(payload.get("workspace_id") or ""),
            )
            host = await podman_service.get_container_ip(container_name)
            if not host:
                raise RuntimeError("Container IP adresi bulunamadı.")
        else:
            port = int(payload["host_port"])
            registered = self._registered_entry(
                str(payload["container_name"]),
                str(payload.get("workspace_id") or ""),
            )
            if int(registered.get("host_port", -1)) != port:
                raise PermissionError("Workspace port eşleşmesi doğrulanamadı.")
            host = "127.0.0.1"
        path = "/" + str(payload.get("path") or "").lstrip("/")
        query = str(payload.get("query") or "")
        return f"{scheme}://{host}:{port}{path}" + (f"?{query}" if query else "")

    async def handle_ws_open(self, request_id: str, payload: dict) -> None:
        stream_id = str(payload["stream_id"])
        try:
            target_url = await self._target_url(payload, websocket=True)
            target = await websockets.connect(
                target_url,
                additional_headers=payload.get("headers") or None,
            )
            self.stream_targets[stream_id] = target
            await self.result(request_id, {"connected": True})
            try:
                while True:
                    message = await target.recv()
                    is_text = isinstance(message, str)
                    raw = message.encode("utf-8") if is_text else message
                    await self.send(
                        {
                            "type": "stream_data",
                            "stream_id": stream_id,
                            "encoding": "text" if is_text else "base64",
                            "data": message if is_text else base64.b64encode(raw).decode("ascii"),
                        }
                    )
            finally:
                await self.send({"type": "stream_end", "stream_id": stream_id})
        except Exception as exc:
            await self.result(request_id, error=str(exc))
            await self.send({"type": "stream_error", "stream_id": stream_id, "error": str(exc)})
        finally:
            target = self.stream_targets.pop(stream_id, None)
            if target:
                await target.close()

    async def handle_stream_message(self, message: dict) -> None:
        stream_id = str(message.get("stream_id") or "")
        target = self.stream_targets.get(stream_id)
        if not target:
            return
        if message.get("type") == "stream_end":
            await target.close()
            return
        data = message.get("data", "")
        if message.get("encoding") == "text":
            await target.send(data)
        else:
            await target.send(base64.b64decode(data, validate=True))

    async def handle_system_command(self, action: str, payload: dict) -> dict:
        if action == "system.upgrade":
            controller_url = (
                os.environ.get("DEVCLOUD_CONTROLLER_URL", "").strip()
                or _required_env("DEVCLOUD_MASTER_URL")
            )
            if self.upgrade_task is not None and not self.upgrade_task.done():
                return {
                    "status": "upgrade_in_progress",
                    "message": "Worker güncellemesi zaten çalışıyor.",
                }
            self._set_upgrade_status(
                "preparing",
                message="Controller release bilgisi alınıyor.",
            )
            self.upgrade_task = asyncio.create_task(
                self._execute_upgrade(controller_url)
            )
            return {"status": "upgrade_started", "message": "Worker güncellemesi başlatıldı."}
        raise ValueError(f"Desteklenmeyen sistem komutu: {action}")

    async def _execute_upgrade(self, controller_url: str) -> None:
        await asyncio.sleep(1)
        temporary: Path | None = None
        destination: Path | None = None
        target_version = ""
        try:
            node_id = _required_env("DEVCLOUD_NODE_ID")
            token = _required_env("DEVCLOUD_NODE_TOKEN")
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=60.0) as client:
                metadata_response = await client.get(
                    f"{controller_url.rstrip('/')}/api/agent/releases/latest",
                    params={"node_id": node_id},
                    headers=headers,
                )
                metadata_response.raise_for_status()
                metadata = metadata_response.json()
                target_version = str(metadata.get("version") or "")
                current_semantic = semantic_version(__version__)
                target_semantic = semantic_version(target_version)
                if target_version == __version__:
                    self._set_upgrade_status(
                        "succeeded",
                        target_version=target_version,
                        message=f"Worker zaten güncel (v{__version__}).",
                    )
                    return
                if (
                    current_semantic is not None
                    and target_semantic is not None
                    and target_semantic < current_semantic
                ):
                    raise RuntimeError(
                        f"Yayımlanan v{target_version}, worker sürümü "
                        f"v{__version__}'dan eski; sürüm düşürme engellendi."
                    )
                self._set_upgrade_status(
                    "downloading",
                    target_version=target_version,
                    message="Platform bundle indiriliyor.",
                )
                queue_root = Path(settings.UPDATE_QUEUE_ROOT).resolve()
                uploads = queue_root / "uploads"
                uploads.mkdir(parents=True, exist_ok=True)
                if (queue_root / "pending.json").exists() or (
                    queue_root / "running.json"
                ).exists():
                    raise RuntimeError("Başka bir worker güncellemesi zaten bekliyor.")
                suffix = (
                    "".join(Path(str(metadata["filename"])).suffixes[-2:])
                    or ".release"
                )
                destination = uploads / f"{uuid.uuid4().hex}{suffix}"
                temporary = destination.with_suffix(".part")
                digest = hashlib.sha256()
                size = 0
                async with client.stream(
                    "GET", metadata["url"], headers=headers
                ) as response:
                    response.raise_for_status()
                    with temporary.open("xb") as handle:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > settings.UPDATE_MAX_UPLOAD_BYTES:
                                raise RuntimeError("Worker release boyut sınırını aşıyor.")
                            digest.update(chunk)
                            handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                if digest.hexdigest() != metadata["sha256"]:
                    raise RuntimeError("Worker release checksum doğrulaması başarısız.")
                os.chmod(temporary, 0o600)
                os.replace(temporary, destination)
                marker = {
                    "state": "queued",
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                    "filename": metadata["filename"],
                    "bundle": str(destination),
                    "size": size,
                    "sha256": digest.hexdigest(),
                    "target_version": target_version,
                    "allow_unsigned": False,
                }
                marker_tmp = queue_root / "pending.tmp"
                marker_tmp.write_text(
                    json.dumps(marker, indent=2) + "\n", encoding="utf-8"
                )
                os.chmod(marker_tmp, 0o600)
                os.replace(marker_tmp, queue_root / "pending.json")
                self._set_upgrade_status(
                    "queued",
                    target_version=target_version,
                    message="Bundle root updater kuyruğuna alındı.",
                )
        except Exception as exc:
            if destination is not None:
                destination.unlink(missing_ok=True)
            self._set_upgrade_status(
                "failed",
                target_version=target_version,
                message=str(exc),
            )
            logger.exception("Upgrade execution error: %s", exc)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def handle_command(self, message: dict) -> None:
        request_id = str(message.get("request_id") or "")
        action = str(message.get("action") or "")
        payload = message.get("payload") or {}
        if action == "proxy.http.open":
            await self.handle_http_open(request_id, payload)
            return
        if action == "proxy.websocket.open":
            await self.handle_ws_open(request_id, payload)
            return
        if action == "workspace.backup.open":
            await self.handle_backup_open(request_id, payload)
            return
        try:
            if action.startswith("system."):
                result = await self.handle_system_command(action, payload)
            elif action.startswith("image."):
                result = await self.handle_image_command(action, payload)
            elif action.startswith("files."):
                result = await self.handle_file_command(action, payload)
            else:
                result = await self.handle_container_command(action, payload)
            await self.result(request_id, result)
        except Exception as exc:
            logger.exception("Worker command failed: %s", action)
            await self.result(request_id, error=str(exc))

    async def run_once(self) -> None:
        headers = {"Authorization": f"Bearer {_required_env('DEVCLOUD_NODE_TOKEN')}"}
        connection_url = _connection_url()
        async with websockets.connect(
            connection_url,
            additional_headers=headers,
            ssl=_tls_context(connection_url),
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            self.websocket = websocket
            heartbeat_task = asyncio.create_task(self.heartbeat())
            image_sync_task = asyncio.create_task(self.image_sync_loop())
            tasks: set[asyncio.Task] = set()
            try:
                async for raw in websocket:
                    if not isinstance(raw, str):
                        continue
                    message = json.loads(raw)
                    if message.get("type") == "command":
                        task = asyncio.create_task(self.handle_command(message))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                    elif message.get("type") in {"stream_data", "stream_end"}:
                        await self.handle_stream_message(message)
            finally:
                heartbeat_task.cancel()
                image_sync_task.cancel()
                for task in tasks:
                    task.cancel()

    async def run_forever(self) -> None:
        delay = 1
        while True:
            try:
                await self.run_once()
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Controller bağlantısı kesildi: %s; %s sn sonra tekrar denenecek", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(WorkerAgent().run_forever())


if __name__ == "__main__":
    main()
