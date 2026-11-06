from datetime import timedelta

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import config
from ..db import get_session
from ..models import ApiKey, utcnow

_api_key: str | None = None


def get_api_key() -> str:
    global _api_key
    if _api_key is None:
        _api_key = config.resolve_api_key()
    return _api_key


async def require_api_access(
    request: Request,
    x_api_key: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
) -> None:
    if not x_api_key and getattr(request.state, "browser_session", False):
        return
    await require_api_key(x_api_key, session)


async def require_api_key(x_api_key: str, session: AsyncSession) -> None:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    # Bootstrap key retained for existing integrations and environment-managed UI.
    if x_api_key == get_api_key():
        return
    # User-managed keys created in Settings.
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key == x_api_key))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    now = utcnow()
    # Throttle writes: only record use at most once a minute per key.
    if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=1):
        row.last_used_at = now
        await session.commit()
