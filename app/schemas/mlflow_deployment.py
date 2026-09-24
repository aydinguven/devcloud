from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.mlflow_deployment import MlflowDeploymentStatus


class MlflowDeploymentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=60, pattern=r"^[a-zA-Z0-9_\- ]+$")
    model_name: str = Field(min_length=1, max_length=255)
    model_version: str = Field(min_length=1, max_length=64)
    flavor_id: str = Field(min_length=1, max_length=50)
    auto_stop_minutes: int = Field(default=0, ge=0, le=1440)
    gunicorn_workers: int = Field(default=1, ge=1, le=16)

    model_config = {"str_strip_whitespace": True}

    @field_validator("model_version")
    @classmethod
    def immutable_numeric_version(cls, value: str) -> str:
        if not value.isdigit() or int(value) < 1:
            raise ValueError("Dağıtım için pozitif, sayısal bir model versiyonu gereklidir.")
        return str(int(value))


class MlflowDeploymentEventOut(BaseModel):
    sequence: int
    level: str
    message: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MlflowDeploymentOut(BaseModel):
    id: str
    user_id: int
    build_id: str | None = None
    workspace_id: str | None = None
    name: str
    model_name: str
    model_version: str
    run_id: str
    source_uri: str
    flavor_id: str
    auto_stop_minutes: int
    gunicorn_workers: int
    status: MlflowDeploymentStatus
    status_message: str
    error_message: str | None = None
    build_status: str | None = None
    build_status_message: str | None = None
    build_error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    endpoint_url: str
    health_url: str

    model_config = {"from_attributes": True}


class MlflowDeploymentCreated(MlflowDeploymentOut):
    access_token: str


class MlflowDeploymentList(BaseModel):
    deployments: list[MlflowDeploymentOut]


class MlflowDeploymentEvents(BaseModel):
    events: list[MlflowDeploymentEventOut]



class MlflowDeploymentTokenOut(BaseModel):
    access_token: str
