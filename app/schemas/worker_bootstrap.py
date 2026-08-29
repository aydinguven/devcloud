from datetime import datetime

from pydantic import BaseModel, Field


class WorkerBootstrapTicketCreated(BaseModel):
    install_url: str
    command: str
    expires_at: datetime


class WorkerBootstrapEnroll(BaseModel):
    name: str = Field(
        min_length=2,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )


class WorkerBootstrapCredentials(BaseModel):
    node_id: str
    enrollment_token: str
    controller_url: str
