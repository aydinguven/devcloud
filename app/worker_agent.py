"""Outbound-only DevCloud worker agent.

Run with DEVCLOUD_MASTER_URL, DEVCLOUD_NODE_ID and DEVCLOUD_NODE_TOKEN set.
The master URL must use https:// in production; the agent converts it to WSS.
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import socket
import ssl
from pathlib import Path

import httpx
import websockets

from app import __version__
from app.config import settings
from app.orchestrator.podman_service import podman_service

logger = logging.getLogger("devcloud.worker")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} ayarlanmalıdır.")
    return value


def _connection_url() -> str:
    base = _required_env("DEVCLOUD_MASTER_URL").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    if not base.startswith(("ws://", "wss://")):
        raise RuntimeError("DEVCLOUD_MASTER_URL http:// veya https:// ile başlamalıdır.")
    return f"{base}/api/agent/connect/{_required_env('DEVCLOUD_NODE_ID')}"


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
        self.stream_targets: dict[str, object] = {}
        self.registry_path = Path(settings.STORAGE_ROOT) / ".devcloud-agent-registry.json"
        self.registry = self._load_registry()

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
            active_cnt = len(self.registry)

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
                        "capabilities": {"runtime": "podman"},
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
            }
            args = {key: value for key, value in payload.items() if key in allowed}
            container_id, storage_path = await podman_service.create_workspace_container(**args)
            self.registry[args["container_name"]] = {
                "workspace_id": args["workspace_id"],
                "storage_path": storage_path,
                "host_port": args["host_port"],
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
            master_url = _required_env("DEVCLOUD_MASTER_URL")
            asyncio.create_task(self._execute_upgrade(master_url))
            return {"status": "upgrade_started", "message": "Worker güncellemesi başlatıldı."}
        raise ValueError(f"Desteklenmeyen sistem komutu: {action}")

    async def _execute_upgrade(self, master_url: str) -> None:
        await asyncio.sleep(1)
        try:
            env = os.environ.copy()
            env["DEVCLOUD_MASTER_URL"] = master_url
            env["DEVCLOUD_NODE_ID"] = _required_env("DEVCLOUD_NODE_ID")
            env["DEVCLOUD_NODE_TOKEN"] = _required_env("DEVCLOUD_NODE_TOKEN")
            proc = await asyncio.create_subprocess_shell(
                f"curl -fsSL '{master_url.rstrip('/')}/download/install-worker.sh' | sudo DEVCLOUD_NODE_ID='{env['DEVCLOUD_NODE_ID']}' DEVCLOUD_NODE_TOKEN='{env['DEVCLOUD_NODE_TOKEN']}' bash",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except Exception as exc:
            logger.exception("Upgrade execution error: %s", exc)

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
        try:
            if action.startswith("system."):
                result = await self.handle_system_command(action, payload)
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
                logger.warning("Master bağlantısı kesildi: %s; %s sn sonra tekrar denenecek", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(WorkerAgent().run_forever())


if __name__ == "__main__":
    main()
