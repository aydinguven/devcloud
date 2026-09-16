from __future__ import annotations

import asyncio
import base64
import uuid
import contextlib
import anyio

from dataclasses import dataclass, field

from fastapi import WebSocket

STREAM_WINDOW = 4
# VS Code webviews can post provider catalogs and restored UI state as a single
# WebSocket message. Keep the tunnel bounded, but do not truncate legitimate IDE
# frames at the old 1 MiB transfer-oriented limit.
MAX_STREAM_FRAME_BYTES = 8 * 1024 * 1024
MAX_STREAMS = 32


class AgentUnavailable(RuntimeError):
    pass


class AgentCommandError(RuntimeError):
    pass


@dataclass
class AgentStream:
    id: str
    queue: asyncio.Queue["StreamChunk" | Exception | None] = field(default_factory=lambda: asyncio.Queue(maxsize=STREAM_WINDOW + 2))


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

    @property
    def confidential_for_secrets(self) -> bool:
        """Permit plaintext command secrets only over WSS or loopback test/agents."""
        if self.websocket is None:
            return True
        scope = getattr(self.websocket, "scope", {}) or {}
        if str(scope.get("scheme") or "").lower() == "wss":
            return True
        client = scope.get("client")
        host = str(client[0] if isinstance(client, (tuple, list)) and client else "")
        return host in {"127.0.0.1", "::1", "localhost"}

    async def send_json(self, message: dict) -> None:
        async with self._send_lock:
            await self.websocket.send_json(message)

    async def request(self, action: str, payload: dict, timeout: float = 60) -> dict:
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                await self.send_json({
                    "type": "command", "request_id": request_id,
                    "action": action, "payload": payload,
                })
                result = await future
            if not result.get("ok", False):
                raise AgentCommandError(result.get("error") or "Worker komutu başarısız oldu.")
            return result.get("payload") or {}
        finally:
            self._pending.pop(request_id, None)

    async def open_stream(self, action: str, payload: dict, timeout: float = 30) -> tuple[dict, AgentStream]:
        if len(self._streams) >= MAX_STREAMS:
            raise AgentCommandError("Worker stream limit reached")
        stream = AgentStream(id=str(uuid.uuid4()))
        self._streams[stream.id] = stream
        try:
            metadata = await self.request(
                action,
                {**payload, "stream_id": stream.id, "flow_control": True},
                timeout=timeout,
            )
            return metadata, stream
        except BaseException:
            await self.close_stream(stream.id)
            raise

    async def send_stream_data(self, stream_id: str, data: bytes, text: bool = False) -> None:
        if len(data) > MAX_STREAM_FRAME_BYTES:
            raise AgentCommandError("WebSocket frame exceeds stream limit")
        # The command response acknowledges the upstream write. A stalled IDE
        # therefore blocks this stream's producer, never the shared receiver.
        await self.request("proxy.websocket.send", {
            "stream_id": stream_id, "encoding": "text" if text else "base64",
            "data": data.decode("utf-8") if text else base64.b64encode(data).decode("ascii"),
        })

    async def receive_stream(self, stream: AgentStream):
        item = await stream.queue.get()
        if isinstance(item, StreamChunk) and stream.id in self._streams:
            await self.send_json({"type": "stream_ack", "stream_id": stream.id})
        return item

    @staticmethod
    def _finish_stream(stream, error=None):
        if error:
            while not stream.queue.empty():
                stream.queue.get_nowait()
            stream.queue.put_nowait(error)
        stream.queue.put_nowait(None)

    async def close_stream(self, stream_id: str) -> None:
        stream = self._streams.pop(stream_id, None)
        if stream:
            self._finish_stream(stream, AgentCommandError("Stream closed"))
        with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
            await asyncio.wait_for(self.send_json({"type": "stream_cancel", "stream_id": stream_id}), 5)

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
                if len(data) > 4 * ((MAX_STREAM_FRAME_BYTES + 2) // 3):
                    raise ValueError("Oversized stream frame")
                decoded = data.encode("utf-8") if message.get("encoding") == "text" else base64.b64decode(data, validate=True)
                if len(decoded) > MAX_STREAM_FRAME_BYTES or stream.queue.qsize() >= STREAM_WINDOW:
                    raise ValueError("Worker exceeded stream flow-control window")
                stream.queue.put_nowait(StreamChunk(decoded, is_text=message.get("encoding") == "text"))
            except Exception as exc:
                self._streams.pop(stream.id, None)
                self._finish_stream(stream, AgentCommandError(str(exc)))
                await self.send_json({"type": "stream_cancel", "stream_id": stream.id})
        elif message_type == "stream_error":
            self._streams.pop(stream.id, None)
            self._finish_stream(stream, AgentCommandError(message.get("error") or "Worker stream error"))
        elif message_type == "stream_end":
            self._streams.pop(stream.id, None)
            self._finish_stream(stream)

    async def disconnect(self) -> None:
        error = AgentUnavailable(f"Worker bağlantısı kesildi: {self.node_id}")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        for stream in self._streams.values():
            self._finish_stream(stream, error)
        self._pending.clear()
        self._streams.clear()


class AgentManager:
    def __init__(self):
        self._connections: dict[str, AgentConnection] = {}
        self._event_listeners: set[asyncio.Queue] = set()

    def subscribe_events(self) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=100)
        self._event_listeners.add(queue)
        return queue

    def unsubscribe_events(self, queue: asyncio.Queue) -> None:
        self._event_listeners.discard(queue)

    async def broadcast_event(self, event_type: str, data: dict) -> None:
        message = {"type": event_type, "data": data}
        dead_queues = set()
        for queue in self._event_listeners:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                dead_queues.add(queue)
            except Exception:
                dead_queues.add(queue)
        for dead in dead_queues:
            self._event_listeners.discard(dead)

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
        await self.broadcast_event("node.connected", {"node_id": node_id, "status": "online"})
        return connection

    async def unregister(self, node_id: str, connection: AgentConnection) -> bool:
        if self._connections.get(node_id) is not connection:
            await connection.disconnect()
            return False
        self._connections.pop(node_id, None)
        await connection.disconnect()
        await self.broadcast_event("node.disconnected", {"node_id": node_id, "status": "offline"})
        return True

    def get(self, node_id: str) -> AgentConnection:
        connection = self._connections.get(node_id)
        if not connection:
            raise AgentUnavailable(f"Worker çevrimdışı veya tunnel bağlı değil: {node_id}")
        return connection

    def is_connected(self, node_id: str) -> bool:
        return node_id in self._connections

    def connected_node_ids(self) -> tuple[str, ...]:
        return tuple(self._connections)

    async def disconnect(self, node_id: str, reason: str = "Worker bağlantısı sonlandırıldı") -> None:
        connection = self._connections.pop(node_id, None)
        if not connection:
            return
        await connection.disconnect()
        try:
            await connection.websocket.close(code=1008, reason=reason)
        except Exception:
            pass
        await self.broadcast_event("node.disconnected", {"node_id": node_id, "status": "offline"})


agent_manager = AgentManager()
