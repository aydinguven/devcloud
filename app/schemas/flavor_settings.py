from pydantic import BaseModel


class FlavorSettingsUpdate(BaseModel):
    enabled: bool
