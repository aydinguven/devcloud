from __future__ import annotations

import asyncio
import base64
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket


class AgentUnavailable(RuntimeError):
    pass


class AgentCommandError(RuntimeError):
    pass


@dataclass
class AgentStream:
    id: str
    queue: asyncio.Queue["StreamChunk" | Exception | None] = field(default_factory=asyncio.Queue)


@dataclass(frozen=True)
class StreamChunk:
    data: bytes
    is_text: bool = False


class AgentConnection:
    def __init__(self, node_id: str, websocket: WebSocket):
        self.node_id = node_id
        self.websocket = websocket
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._streams: dict[str, AgentStream] = {}

    async def send_json(self, message: dict) -> None:
        async with self._send_lock:
            await self.websocket.send_json(message)

    async def request(self, action: str, payload: dict, timeout: float = 60) -> dict:
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.send_json(
                {
                    "type": "command",
                    "request_id": request_id,
                    "action": action,
                    "payload": payload,
                }
            )
            result = await asyncio.wait_for(future, timeout=timeout)
            if not result.get("ok", False):
                raise AgentCommandError(result.get("error") or "Worker komutu başarısız oldu.")
            return result.get("payload") or {}
        finally:
            self._pending.pop(request_id, None)

    async def open_stream(self, action: str, payload: dict, timeout: float = 30) -> tuple[dict, AgentStream]:
        stream = AgentStream(id=str(uuid.uuid4()))
        self._streams[stream.id] = stream
        try:
            metadata = await self.request(
                action,
                {**payload, "stream_id": stream.id},
                timeout=timeout,
            )
            return metadata, stream
        except Exception:
            self._streams.pop(stream.id, None)
            raise

    async def send_stream_data(self, stream_id: str, data: bytes, text: bool = False) -> None:
        await self.send_json(
            {
                "type": "stream_data",
                "stream_id": stream_id,
                "encoding": "text" if text else "base64",
                "data": data.decode("utf-8") if text else base64.b64encode(data).decode("ascii"),
            }
        )

    async def close_stream(self, stream_id: str) -> None:
        await self.send_json({"type": "stream_end", "stream_id": stream_id})
        self._streams.pop(stream_id, None)

    async def handle_message(self, message: dict) -> None:
        message_type = message.get("type")
        if message_type == "result":
            future = self._pending.get(message.get("request_id", ""))
            if future and not future.done():
                future.set_result(message)
            return

        stream = self._streams.get(message.get("stream_id", ""))
        if not stream:
            return
        if message_type == "stream_data":
            try:
                data = message.get("data", "")
                if message.get("encoding") == "text":
                    decoded = data.encode("utf-8")
                else:
                    decoded = base64.b64decode(data, validate=True)
                await stream.queue.put(
                    StreamChunk(decoded, is_text=message.get("encoding") == "text")
                )
            except Exception as exc:
                await stream.queue.put(exc)
        elif message_type == "stream_error":
            await stream.queue.put(AgentCommandError(message.get("error") or "Worker stream hatası."))
            await stream.queue.put(None)
            self._streams.pop(stream.id, None)
        elif message_type == "stream_end":
            await stream.queue.put(None)
            self._streams.pop(stream.id, None)

    async def disconnect(self) -> None:
        error = AgentUnavailable(f"Worker bağlantısı kesildi: {self.node_id}")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        for stream in self._streams.values():
            await stream.queue.put(error)
            await stream.queue.put(None)
        self._pending.clear()
        self._streams.clear()


class AgentManager:
    def __init__(self):
        self._connections: dict[str, AgentConnection] = {}

    async def register(self, node_id: str, websocket: WebSocket) -> AgentConnection:
        old = self._connections.pop(node_id, None)
        if old:
            await old.disconnect()
            try:
                await old.websocket.close(code=1012, reason="Yeni agent bağlantısı kuruldu")
            except Exception:
                pass
        connection = AgentConnection(node_id, websocket)
        self._connections[node_id] = connection
        return connection

    async def unregister(self, node_id: str, connection: AgentConnection) -> None:
        if self._connections.get(node_id) is connection:
            self._connections.pop(node_id, None)
        await connection.disconnect()

    def get(self, node_id: str) -> AgentConnection:
        connection = self._connections.get(node_id)
        if not connection:
            raise AgentUnavailable(f"Worker çevrimdışı veya tunnel bağlı değil: {node_id}")
        return connection

    def is_connected(self, node_id: str) -> bool:
        return node_id in self._connections

    async def disconnect(self, node_id: str, reason: str = "Worker bağlantısı sonlandırıldı") -> None:
        connection = self._connections.pop(node_id, None)
        if not connection:
            return
        await connection.disconnect()
        try:
            await connection.websocket.close(code=1008, reason=reason)
        except Exception:
            pass


agent_manager = AgentManager()
