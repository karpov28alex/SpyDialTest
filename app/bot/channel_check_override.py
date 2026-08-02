from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.bot.enhanced_user_menu import enhanced_user_keyboard
from app.db.models import User
from app.db.session import SessionLocal
from app.services.access import access_state, get_monetization_settings
from app.services.access_funnel import (
    check_channel_membership,
    get_funnel_config,
    mark_channel_verified,
)
from app.services.users import activate_trial_after_channel, has_active_business

router = Router(name="channel-check-override")


@router.callback_query(F.data == "funnel:check_channel")
async def check_channel_once(callback: CallbackQuery) -> None:
    config = await get_funnel_config()
    if not config.enabled or not config.channel_required:
        await callback.answer("Проверка подписки сейчас отключена.", show_alert=True)
        return

    ok = await check_channel_membership(
        callback.bot,
        user_id=callback.from_user.id,
        channel_id=config.channel_id,
    )
    if not ok:
        await callback.answer(config.subscription_error_text, show_alert=True)
        return

    await mark_channel_verified(callback.from_user.id)

    async with SessionLocal() as session, session.begin():
        user = await session.scalar(
            select(User)
            .where(User.telegram_id == callback.from_user.id)
            .with_for_update()
        )
        if not user:
            await callback.answer("Сначала отправьте /start", show_alert=True)
            return

        started = await activate_trial_after_channel(session, user=user)
        business_connected = await has_active_business(session, user.id)
        state = await access_state(session, user)
        monetization = await get_monetization_settings(session)

    await callback.answer(config.subscription_success_text, show_alert=True)
    if not callback.message:
        return

    if config.business_required and not business_connected and not state.active:
        await callback.message.answer(config.business_required_text)
        return

    if started:
        await callback.message.answer(
            config.trial_started_text.format(days=monetization.trial_days),
            reply_markup=enhanced_user_keyboard(),
        )
        return

    if state.active:
        await callback.message.answer(
            "<b>Phantom</b> — приватный архив Telegram Business.",
            reply_markup=enhanced_user_keyboard(),
        )
        return

    await callback.message.answer(config.payment_required_text)
