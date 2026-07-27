from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import SessionDep, SettingsDep
from app.core.security import create_token, validate_telegram_init_data
from app.db.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TelegramAuthRequest(BaseModel):
    init_data: str


@router.post("/telegram")
async def telegram_auth(body: TelegramAuthRequest, session: SessionDep, settings: SettingsDep) -> dict:
    try:
        tg_user = validate_telegram_init_data(
            body.init_data, settings.telegram_bot_token, settings.init_data_max_age_seconds
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = await session.scalar(select(User).where(User.telegram_id == int(tg_user["id"])))
    if not user:
        raise HTTPException(status_code=403, detail="Run /start first")
    return {
        "access_token": create_token(
            str(user.id), "access", timedelta(minutes=settings.access_token_ttl_minutes), settings
        ),
        "token_type": "bearer",
    }
