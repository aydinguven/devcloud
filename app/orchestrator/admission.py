"""Serialize resource admission across requests and controller processes."""

import asyncio
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from sqlalchemy import text

# The database lock is authoritative; this also avoids needless SQLite contention.
_admission_locks = WeakKeyDictionary()


@asynccontextmanager
async def admission_transaction(db):
    # Callers enter with read-only state. Never carry a stale read snapshot into
    # the reservation transaction, or hold the lock while contacting a worker.
    await db.commit()
    loop = asyncio.get_running_loop()
    lock = _admission_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        try:
            if db.bind.dialect.name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            elif db.bind.dialect.name == "postgresql":
                await db.execute(text("SELECT pg_advisory_xact_lock(731947201)"))
            else:
                raise RuntimeError("Unsupported admission database")
            yield
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
