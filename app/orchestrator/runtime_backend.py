from typing import Awaitable, Callable, Protocol

from app.agents.manager import agent_manager

ProgressCallback = Callable[[str, str], Awaitable[None]]


class RuntimeBackend(Protocol):
    async def create_workspace_container(self, **kwargs) -> tuple[str, str]: ...
    async def container_exists(self, container_name: str) -> bool: ...
    async def start_container(self, container_name: str) -> bool: ...
    async def stop_container(self, container_name: str) -> bool: ...
    async def delete_container(self, container_name: str, storage_path: str = "") -> bool: ...
    async def get_container_status(self, container_name: str) -> str: ...
    async def get_logs(self, container_name: str, tail: int = 100) -> str: ...
    async def port_ready(self, container_name: str, host_port: int) -> bool: ...
    async def get_container_stats(self, container_name: str) -> dict: ...
    async def get_storage_size(self, container_name: str, storage_path: str) -> int: ...


class AgentRuntimeBackend:
    def __init__(self, node_id: str):
        self.node_id = node_id

    async def _request(self, action: str, payload: dict, timeout: float = 60) -> dict:
        return await agent_manager.get(self.node_id).request(action, payload, timeout=timeout)

    async def create_workspace_container(self, **kwargs) -> tuple[str, str]:
        progress_callback = kwargs.pop("progress_callback", None)
        if progress_callback:
            await progress_callback("Workspace worker'a gönderiliyor...", "info")
        result = await self._request("container.create", kwargs, timeout=180)
        if progress_callback:
            await progress_callback("Worker container'ı başlattı.", "success")
        return result["container_id"], result["storage_path"]

    async def container_exists(self, container_name: str) -> bool:
        result = await self._request("container.exists", {"container_name": container_name})
        return bool(result.get("exists"))

    async def start_container(self, container_name: str) -> bool:
        result = await self._request("container.start", {"container_name": container_name})
        return bool(result.get("success"))

    async def stop_container(self, container_name: str) -> bool:
        result = await self._request("container.stop", {"container_name": container_name})
        return bool(result.get("success"))

    async def delete_container(self, container_name: str, storage_path: str = "") -> bool:
        result = await self._request(
            "container.delete",
            {"container_name": container_name, "storage_path": storage_path},
        )
        return bool(result.get("success"))

    async def get_container_status(self, container_name: str) -> str:
        result = await self._request("container.status", {"container_name": container_name})
        return result.get("status", "unknown")

    async def get_logs(self, container_name: str, tail: int = 100) -> str:
        result = await self._request(
            "container.logs", {"container_name": container_name, "tail": tail}
        )
        return result.get("logs", "")

    async def port_ready(self, container_name: str, host_port: int) -> bool:
        result = await self._request(
            "container.port_ready",
            {"container_name": container_name, "host_port": host_port},
        )
        return bool(result.get("ready"))

    async def get_container_stats(self, container_name: str) -> dict:
        return await self._request("container.stats", {"container_name": container_name})

    async def get_storage_size(self, container_name: str, storage_path: str) -> int:
        result = await self._request("container.storage_size", {"container_name": container_name})
        return int(result.get("bytes", 0))


def runtime_for_node(node_id: str | None) -> RuntimeBackend:
    if not node_id:
        raise RuntimeError(
            "Workspace worker ataması eksik; controller üzerinde yerel runtime yok."
        )
    return AgentRuntimeBackend(node_id)
