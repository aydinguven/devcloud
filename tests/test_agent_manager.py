import asyncio
import base64

import pytest

from app.agents.manager import AgentConnection


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


@pytest.mark.asyncio
async def test_agent_command_result_and_stream_frames_are_correlated():
    websocket = FakeWebSocket()
    connection = AgentConnection("node-1", websocket)

    request_task = asyncio.create_task(connection.request("container.status", {"name": "ws"}))
    await asyncio.sleep(0)
    command = websocket.sent[0]
    await connection.handle_message(
        {
            "type": "result",
            "request_id": command["request_id"],
            "ok": True,
            "payload": {"status": "running"},
        }
    )
    assert await request_task == {"status": "running"}

    stream_task = asyncio.create_task(connection.open_stream("proxy.http.open", {}))
    await asyncio.sleep(0)
    stream_command = websocket.sent[1]
    await connection.handle_message(
        {
            "type": "result",
            "request_id": stream_command["request_id"],
            "ok": True,
            "payload": {"status_code": 200},
        }
    )
    metadata, stream = await stream_task
    await connection.handle_message(
        {
            "type": "stream_data",
            "stream_id": stream.id,
            "encoding": "base64",
            "data": base64.b64encode(b"hello").decode("ascii"),
        }
    )
    await connection.handle_message({"type": "stream_end", "stream_id": stream.id})

    chunk = await stream.queue.get()
    assert metadata == {"status_code": 200}
    assert chunk.data == b"hello"
    assert chunk.is_text is False
    assert await stream.queue.get() is None

