from __future__ import annotations

import hashlib
import json
import secrets
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.node import Node, NodeStatus
from app.models.worker_bootstrap_ticket import WorkerBootstrapTicket
from app.schemas.worker_bootstrap import (
    WorkerBootstrapCredentials,
    WorkerBootstrapEnroll,
)
from app.worker_bootstrap import (
    active_ticket,
    controller_base_url,
    current_platform_release,
)


bootstrap_router = APIRouter(prefix="/api/bootstrap/workers", tags=["Worker Bootstrap"])
worker_bootstrap_template = (
    Path(__file__).resolve().parent.parent / "templates" / "install_worker.sh"
)


@bootstrap_router.get(
    "/{ticket}/install.sh",
    response_class=PlainTextResponse,
    include_in_schema=False,
)
async def worker_install_script(
    ticket: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Render a ticket-bound installer without consuming the ticket."""
    await active_ticket(ticket, db)
    current_platform_release()
    base_url = await controller_base_url(request, db)
    values = {
        "__CONTROLLER_URL__": shlex.quote(base_url),
        "__ENROLLMENT_URL__": shlex.quote(
            f"{base_url}/api/bootstrap/workers/{ticket}/enroll"
        ),
    }
    script = worker_bootstrap_template.read_text(encoding="utf-8")
    for placeholder, value in values.items():
        script = script.replace(placeholder, value)
    return PlainTextResponse(
        script,
        media_type="text/plain",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'inline; filename="install-worker.sh"',
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@bootstrap_router.post(
    "/{ticket}/enroll",
    response_model=WorkerBootstrapCredentials,
    status_code=status.HTTP_201_CREATED,
)
async def enroll_worker(
    ticket: str,
    data: WorkerBootstrapEnroll,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Consume one bootstrap ticket and return one permanent worker credential."""
    current_platform_release()
    base_url = await controller_base_url(request, db)
    record = await active_ticket(ticket, db)
    now = datetime.now(timezone.utc)
    claimed = await db.execute(
        update(WorkerBootstrapTicket)
        .where(
            WorkerBootstrapTicket.id == record.id,
            WorkerBootstrapTicket.used_at.is_(None),
            WorkerBootstrapTicket.expires_at > now,
        )
        .values(
            used_at=now,
            used_by_ip=request.client.host if request.client else None,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await db.rollback()
        raise HTTPException(status_code=410, detail="Worker kurulum bileti kullanılmış.")

    enrollment_token = secrets.token_urlsafe(32)
    node = Node(
        name=data.name,
        schedulable=True,
        labels_json=json.dumps({}, ensure_ascii=False),
        agent_token_hash=hashlib.sha256(
            enrollment_token.encode("utf-8")
        ).hexdigest(),
        status=NodeStatus.PENDING,
    )
    try:
        db.add(node)
        await db.flush()
        await db.execute(
            update(WorkerBootstrapTicket)
            .where(WorkerBootstrapTicket.id == record.id)
            .values(node_id=node.id)
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Bu isimde bir worker zaten var.",
        ) from exc

    return WorkerBootstrapCredentials(
        node_id=node.id,
        enrollment_token=enrollment_token,
        controller_url=base_url,
    )
