from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.session_settings import SessionSettings


async def session_timeout_minutes(db: AsyncSession) -> int:
    record = await db.get(SessionSettings, 1)
    return record.timeout_minutes if record else settings.ACCESS_TOKEN_EXPIRE_MINUTES
