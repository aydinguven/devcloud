from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


TourStatus = Literal["not_started", "in_progress", "paused", "completed"]
TourChoice = Literal["show", "skip"]


class OnboardingSettingsUpdate(BaseModel):
    enabled: bool = Field(strict=True)
    current_version: int = Field(strict=True, ge=1, le=10000)


class OnboardingSettingsOut(OnboardingSettingsUpdate):
    enabled_at: datetime | None = None
    updated_at: datetime | None = None


class OnboardingTopicChoice(BaseModel):
    topic_id: str = Field(min_length=1, max_length=64)
    choice: TourChoice


class OnboardingStateUpdate(BaseModel):
    tour_version: int = Field(strict=True, ge=1)
    expected_revision: int = Field(strict=True, ge=0)
    status: TourStatus | None = None
    current_topic: str | None = Field(default=None, max_length=64)
    current_step: str | None = Field(default=None, max_length=32)
    topic_choice: OnboardingTopicChoice | None = None


class OnboardingRestartRequest(BaseModel):
    tour_version: int = Field(strict=True, ge=1)
    expected_revision: int = Field(strict=True, ge=0)


class OnboardingStateOut(BaseModel):
    enabled: bool
    auto_offer: bool
    tour_version: int
    status: TourStatus
    current_topic: str
    current_step: str
    topic_choices: dict[str, TourChoice]
    revision: int
    features: dict[str, bool]
    context: dict[str, str | None]
