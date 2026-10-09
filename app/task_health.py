"""In-process liveness of the controller's background loops.

Each loop reports ``success`` or ``failure`` once per iteration; ``lifespan``
registers the task handle so the health report can tell a dead task from a
slow one. State is per process and resets on restart.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class _TaskState:
    name: str
    label: str
    stale_after_seconds: float
    enabled: bool = True
    task: asyncio.Task | None = None
    registered_at: float = field(default_factory=time.time)
    last_success_at: float | None = None
    last_error_at: float | None = None
    last_error: str = ""
    consecutive_failures: int = 0


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class TaskHealthTracker:
    def __init__(self) -> None:
        self._tasks: dict[str, _TaskState] = {}

    def register(
        self,
        name: str,
        label: str,
        task: asyncio.Task | None,
        *,
        stale_after_seconds: float,
        enabled: bool = True,
    ) -> None:
        self._tasks[name] = _TaskState(
            name=name,
            label=label,
            stale_after_seconds=stale_after_seconds,
            enabled=enabled,
            task=task,
        )

    def _state(self, name: str) -> _TaskState:
        state = self._tasks.get(name)
        if state is None:
            # Loops started outside lifespan (tests, scripts) still report.
            state = self._tasks[name] = _TaskState(
                name=name, label=name, stale_after_seconds=float("inf")
            )
        return state

    def success(self, name: str) -> None:
        state = self._state(name)
        state.last_success_at = time.time()
        state.consecutive_failures = 0

    def failure(self, name: str, exc: BaseException) -> None:
        state = self._state(name)
        state.last_error_at = time.time()
        state.last_error = f"{type(exc).__name__}: {exc}"[:500]
        state.consecutive_failures += 1

    def clear(self) -> None:
        self._tasks.clear()

    def snapshot(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        return [classify_task(state, now) for state in self._tasks.values()]


def classify_task(state: _TaskState, now: float) -> dict:
    """Return ``{name, label, status, message, ...}`` for one loop."""
    status, message = "ok", "Çalışıyor."
    task = state.task
    if not state.enabled:
        status, message = "disabled", "Yapılandırmada kapalı."
    elif task is not None and task.done():
        status = "down"
        if task.cancelled():
            message = "Görev iptal edildi."
        elif task.exception() is not None:
            exc = task.exception()
            message = f"Görev durdu: {type(exc).__name__}: {exc}"[:500]
        else:
            message = "Görev beklenmedik şekilde sona erdi."
    else:
        reference = state.last_success_at or state.registered_at
        age = now - reference
        if state.last_error_at and (
            state.last_success_at is None or state.last_error_at > state.last_success_at
        ):
            status = "degraded"
            message = f"Son çalışma başarısız: {state.last_error}"
        elif age > state.stale_after_seconds:
            status = "degraded"
            message = (
                f"{int(age)} sn'dir başarılı çalışma yok."
                if state.last_success_at
                else "Henüz başarılı çalışma yok."
            )
        elif state.last_success_at is None:
            message = "Başlatıldı; ilk çalışma bekleniyor."
    return {
        "name": state.name,
        "label": state.label,
        "status": status,
        "message": message,
        "last_success_at": _iso(state.last_success_at),
        "last_error_at": _iso(state.last_error_at),
        "last_error": state.last_error,
        "consecutive_failures": state.consecutive_failures,
    }


task_health = TaskHealthTracker()
