"""Bounded, acknowledged file transfers over worker commands.

Each chunk is acknowledged before the next is sent. Downloads are pulled by
consumers, so a slow HTTP client cannot fill the shared worker tunnel.
"""
import base64
import contextlib
import uuid
import anyio

CHUNK_BYTES = 256 * 1024


async def abort_transfer(connection, transfer_id):
    with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
        await connection.request("transfer.close", {"transfer_id": transfer_id}, timeout=5)


async def upload_transfer(connection, chunks, *, payload, limit):
    transfer_id = str(uuid.uuid4())
    completed = False
    try:
        await connection.request("transfer.open", {**payload, "transfer_id": transfer_id})
        offset = 0
        async for data in chunks:
            for start in range(0, len(data), CHUNK_BYTES):
                chunk = data[start:start + CHUNK_BYTES]
                if offset + len(chunk) > limit:
                    raise ValueError("Transfer exceeds the configured size limit.")
                await connection.request("transfer.write", {
                    "transfer_id": transfer_id, "offset": offset,
                    "data": base64.b64encode(chunk).decode("ascii"),
                })
                offset += len(chunk)
        result = await connection.request("transfer.finish", {"transfer_id": transfer_id}, timeout=600)
        completed = True
        return {**result, "transfer_id": transfer_id}
    finally:
        # HTTP bodies remain open until consumed by the upstream request.
        if not completed or payload.get("purpose") != "http":
            await abort_transfer(connection, transfer_id)


async def download_chunks(connection, transfer_id, size):
    offset = 0
    try:
        while offset < size:
            result = await connection.request("transfer.read", {
                "transfer_id": transfer_id, "offset": offset,
            })
            chunk = base64.b64decode(result["data"], validate=True)
            if not chunk or len(chunk) > CHUNK_BYTES or offset + len(chunk) > size:
                raise RuntimeError("Invalid or truncated worker file transfer.")
            offset += len(chunk)
            yield chunk
    finally:
        await abort_transfer(connection, transfer_id)
