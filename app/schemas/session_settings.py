from pydantic import BaseModel, Field


class SessionSettingsUpdate(BaseModel):
    timeout_minutes: int = Field(strict=True, ge=1, le=10080)
