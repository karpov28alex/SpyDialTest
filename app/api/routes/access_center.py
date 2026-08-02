from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.bot.setup import bot
from app.services.access_center import build_access_center

router = APIRouter(prefix="/api/access-center", tags=["access-center"])


@router.get("")
async def access_center(user: CurrentUser, session: SessionDep) -> dict:
    """Return the same onboarding and access state used by the Telegram bot."""
    return await build_access_center(session=session, user=user, bot=bot)
