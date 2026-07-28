from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes.admin import AdminAuth, Session
from app.db.models import User
from app.services.access import access_state, get_monetization_settings, grant_access, payment_plans

router = APIRouter(prefix="/api/admin/monetization", tags=["admin-monetization"])


class MonetizationPatch(BaseModel):
    free_trial_enabled: bool | None = None
    show_trial_in_profile: bool | None = None
    show_tariffs: bool | None = None
    trial_days: int | None = Field(default=None, ge=1, le=30)
    referral_bonus_days: int | None = Field(default=None, ge=1, le=30)
    entry_price_rub: int | None = Field(default=None, ge=1, le=100000)
    weekly_price_rub: int | None = Field(default=None, ge=1, le=100000)
    fallback_three_day_price_rub: int | None = Field(default=None, ge=1, le=100000)
    payment_placeholder_url: str | None = Field(default=None, max_length=1024)

    @field_validator(
        "trial_days",
        "referral_bonus_days",
        "entry_price_rub",
        "weekly_price_rub",
        "fallback_three_day_price_rub",
        mode="before",
    )
    @classmethod
    def parse_integer_fields(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, str):
            value = value.strip().replace(" ", "")
        return int(value)

    @field_validator("payment_placeholder_url", mode="before")
    @classmethod
    def normalize_payment_url(cls, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None


class GrantAccessRequest(BaseModel):
    telegram_id: int
    days: int = Field(ge=1, le=3650)



def serialize_config(row) -> dict:
    return {
        "free_trial_enabled": row.free_trial_enabled,
        "show_trial_in_profile": row.show_trial_in_profile,
        "show_tariffs": row.show_tariffs,
        "trial_days": row.trial_days,
        "referral_bonus_days": row.referral_bonus_days,
        "entry_price_rub": row.entry_price_rub,
        "weekly_price_rub": row.weekly_price_rub,
        "fallback_three_day_price_rub": row.fallback_three_day_price_rub,
        "payment_placeholder_url": row.payment_placeholder_url,
        "plans": payment_plans(row),
    }


@router.get("/settings")
async def settings(_: AdminAuth, session: Session) -> dict:
    row = await get_monetization_settings(session)
    return serialize_config(row)


@router.patch("/settings")
async def patch_settings(body: MonetizationPatch, admin: AdminAuth, session: Session) -> dict:
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=422, detail="Не переданы настройки для сохранения")
    row = await get_monetization_settings(session, lock=True)
    for key, value in values.items():
        setattr(row, key, value)
    row.updated_by = admin
    try:
        await session.flush()
        result = serialize_config(row)
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Не удалось сохранить настройки тарифов. Проверьте введённые значения.") from exc
    return {"ok": True, "settings": result}


@router.post("/grant-access")
async def grant_user_access(body: GrantAccessRequest, _: AdminAuth, session: Session) -> dict:
    user = await session.scalar(select(User).where(User.telegram_id == body.telegram_id).with_for_update())
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь с таким Telegram ID не найден")
    try:
        subscription = await grant_access(session, user=user, days=body.days)
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Не удалось выдать доступ пользователю") from exc
    state = await access_state(session, user)
    return {
        "ok": True,
        "user": {"id": user.id, "telegram_id": user.telegram_id, "username": user.username},
        "subscription_id": subscription.id,
        "access": {"active": state.active, "source": state.source, "ends_at": state.ends_at},
    }


@router.get("/user/{telegram_id}")
async def user_access(telegram_id: int, _: AdminAuth, session: Session) -> dict:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    state = await access_state(session, user)
    return {
        "telegram_id": user.telegram_id,
        "username": user.username,
        "name": " ".join(filter(None, [user.first_name, user.last_name])),
        "access": {"active": state.active, "source": state.source, "ends_at": state.ends_at},
    }
