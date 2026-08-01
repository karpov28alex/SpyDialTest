from __future__ import annotations

from urllib.parse import quote

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy import select

from app.bot.admin_console import home_keyboard, is_admin, user_menu
from app.bot.setup import bot
from app.core.config import get_settings
from app.db.models import Referral, User
from app.db.session import SessionLocal
from app.services.access import access_state
from app.services.access_funnel import (
    channel_gate_passed,
    check_channel_membership,
    get_funnel_config,
    mark_channel_verified,
    redis_client,
    save_funnel_config,
)
from app.services.users import activate_trial_after_channel, referral_code, register_or_update_user

router = Router(name="access_funnel")
settings = get_settings()
EDIT_STATE_PREFIX = "phantom:funnel:edit:"

EDITABLE_FIELDS = {
    "channel_id": "ID или @username канала",
    "channel_url": "Ссылка на канал",
    "channel_title": "Название канала",
    "subscription_text": "Текст требования подписки",
    "subscription_error_text": "Текст ошибки проверки",
    "subscription_success_text": "Текст успешной проверки",
    "referral_text": "Текст после окончания trial",
    "referral_started_text": "Текст о переходе друга",
    "referral_bonus_success_text": "Текст начисления бонуса",
    "referral_share_text": "Текст приглашения другу",
    "payment_required_text": "Текст обязательной оплаты",
    "payment_button_text": "Текст кнопки оплаты",
    "payment_url": "Ссылка оплаты",
    "redacted_actor": "Маска имени",
    "redacted_content": "Маска содержимого",
}


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_keyboard(channel_url: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if channel_url.startswith("https://"):
        rows.append([InlineKeyboardButton(text="📢 Подписаться на канал", url=channel_url)])
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="funnel:check_channel")])
    return keyboard(rows)


def expired_keyboard(payment_url: str, payment_button_text: str, referral_available: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if referral_available:
        rows.append([InlineKeyboardButton(text="👥 Пригласить друга", callback_data="funnel:invite")])
    if payment_url.startswith("https://"):
        rows.append([InlineKeyboardButton(text=payment_button_text, url=payment_url)])
    return keyboard(rows)


def referral_share_url(link: str, text: str) -> str:
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text, safe='')}"


def funnel_admin_keyboard(config) -> InlineKeyboardMarkup:
    toggle = lambda value: "✅" if value else "❌"
    return keyboard([
        [InlineKeyboardButton(text="✅ Воронка доступа обязательна", callback_data="funnel:locked")],
        [InlineKeyboardButton(text="🔒 Подписка на канал обязательна", callback_data="funnel:channel_locked")],
        [InlineKeyboardButton(text=f"{toggle(config.referral_required)} Приглашение друга", callback_data="funnel:toggle:referral_required")],
        [InlineKeyboardButton(text=f"{toggle(config.redact_expired_notifications)} Цензура уведомлений", callback_data="funnel:toggle:redact_expired_notifications")],
        [InlineKeyboardButton(text="📢 Канал", callback_data="funnel:fields:channel"), InlineKeyboardButton(text="💳 Оплата", callback_data="funnel:fields:payment")],
        [InlineKeyboardButton(text="📝 Тексты", callback_data="funnel:fields:texts"), InlineKeyboardButton(text="🔒 Маски", callback_data="funnel:fields:redaction")],
        [InlineKeyboardButton(text="🌐 Полные настройки", web_app=WebAppInfo(url=f"{settings.admin_url.rstrip('/')}/funnel.html"))],
        [InlineKeyboardButton(text="◀️ В админ-панель", callback_data="crm:home")],
    ])


def extended_admin_keyboard() -> InlineKeyboardMarkup:
    rows = [list(row) for row in home_keyboard().inline_keyboard]
    rows.insert(max(len(rows) - 1, 0), [InlineKeyboardButton(text="🔐 Доступ и воронка", callback_data="funnel:admin")])
    return keyboard(rows)


async def show_funnel_admin(message: Message) -> None:
    config = await get_funnel_config()
    await message.answer(
        "<b>🔐 Доступ и воронка</b>\n\n"
        f"Канал: <b>{config.channel_title or 'не указан'}</b>\n"
        f"ID: <code>{config.channel_id or 'не указан'}</code>\n"
        f"Оплата: <code>{config.payment_url or 'не указана'}</code>\n\n"
        "Воронка и подписка на канал обязательны. Платёжный провайдер пока не подключён.",
        reply_markup=funnel_admin_keyboard(config),
    )


async def send_access_screen(message: Message, user: User) -> None:
    config = await get_funnel_config()
    if not await channel_gate_passed(bot, user_id=user.telegram_id, config=config):
        await message.answer(config.subscription_text, reply_markup=subscription_keyboard(config.channel_url))
        return

    async with SessionLocal() as session, session.begin():
        db_user = await session.get(User, user.id, with_for_update=True)
        if db_user:
            await activate_trial_after_channel(session, user=db_user)
            state = await access_state(session, db_user)
            referral_available = db_user.referral_bonus_granted_at is None
        else:
            state = await access_state(session, user)
            referral_available = user.referral_bonus_granted_at is None

    if not state.active:
        text = config.referral_text if config.referral_required and referral_available else config.payment_required_text
        await message.answer(
            text,
            reply_markup=expired_keyboard(config.payment_url, config.payment_button_text, referral_available),
        )
        return

    await message.answer(
        "<b>Phantom</b> — приватный архив Telegram Business.",
        reply_markup=user_menu(await is_admin(user.telegram_id)),
    )


@router.message(CommandStart())
async def start(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    async with SessionLocal() as session, session.begin():
        user, created = await register_or_update_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            language_code=message.from_user.language_code,
            start_parameter=command.args,
        )
        referrer_telegram_id = None
        if created and command.args and command.args.startswith("ref_"):
            referral = await session.scalar(select(Referral).where(Referral.referred_user_id == user.id))
            if referral:
                referrer = await session.get(User, referral.referrer_user_id)
                referrer_telegram_id = referrer.telegram_id if referrer else None

    if referrer_telegram_id:
        config = await get_funnel_config()
        try:
            await bot.send_message(referrer_telegram_id, config.referral_started_text)
        except Exception:
            pass
    await send_access_screen(message, user)


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not await is_admin(user_id):
        await message.answer("⛔ У вас нет доступа к админ-панели.")
        return
    await message.answer("<b>🛡 Phantom — центр управления</b>\n\nВсе основные действия доступны по кнопкам.", reply_markup=extended_admin_keyboard())


@router.callback_query(F.data == "funnel:check_channel")
async def check_channel(callback: CallbackQuery) -> None:
    config = await get_funnel_config()
    ok = await check_channel_membership(bot, user_id=callback.from_user.id, channel_id=config.channel_id)
    if not ok:
        await callback.answer(config.subscription_error_text, show_alert=True)
        return
    await mark_channel_verified(callback.from_user.id)
    async with SessionLocal() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id).with_for_update())
        if user:
            started = await activate_trial_after_channel(session, user=user)
        else:
            started = False
    await callback.answer(config.subscription_success_text, show_alert=True)
    if callback.message and user:
        if started:
            await callback.message.answer("🎉 <b>Доступ активирован</b>\n\nВы получили полный бесплатный доступ к Phantom на пробный период.")
        await send_access_screen(callback.message, user)


@router.callback_query(F.data == "funnel:invite")
async def invite_friend(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
    if not user:
        await callback.answer("Сначала отправьте /start", show_alert=True)
        return
    config = await get_funnel_config()
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{referral_code(user)}"
    text = (
        "<b>👥 Пригласите друга в Phantom</b>\n\n"
        "После перехода друг обязательно подпишется на информационный канал, подключит Telegram Business и начнёт пользоваться Phantom.\n\n"
        f"Ваша ссылка:\n<code>{link}</code>"
    )
    markup = keyboard([
        [InlineKeyboardButton(text="🚀 Отправить другу", url=referral_share_url(link, config.referral_share_text))],
        [InlineKeyboardButton(text="📋 Поделиться ссылкой", switch_inline_query=config.referral_share_text + " " + link)],
    ])
    if callback.message:
        await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.in_({"funnel:channel_locked", "funnel:locked"}))
async def locked_setting(callback: CallbackQuery) -> None:
    await callback.answer("Воронка и подписка на информационный канал обязательны для всех пользователей.", show_alert=True)


@router.callback_query(F.data == "funnel:admin")
async def admin_home(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if callback.message:
        await show_funnel_admin(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("funnel:toggle:"))
async def toggle_setting(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    field = (callback.data or "").split(":", 2)[2]
    config = await get_funnel_config()
    if field not in {"referral_required", "redact_expired_notifications"}:
        await callback.answer("Эту настройку нельзя отключить", show_alert=True)
        return
    updated = await save_funnel_config({field: not getattr(config, field)})
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=funnel_admin_keyboard(updated))
    await callback.answer("Сохранено")


@router.callback_query(F.data.startswith("funnel:fields:"))
async def fields(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    group = (callback.data or "").split(":", 2)[2]
    groups = {
        "channel": ["channel_id", "channel_url", "channel_title"],
        "payment": ["payment_button_text", "payment_url", "payment_required_text"],
        "texts": ["subscription_text", "subscription_error_text", "subscription_success_text", "referral_text", "referral_started_text", "referral_bonus_success_text", "referral_share_text"],
        "redaction": ["redacted_actor", "redacted_content"],
    }
    rows = [[InlineKeyboardButton(text=EDITABLE_FIELDS[name], callback_data=f"funnel:edit:{name}")] for name in groups.get(group, [])]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="funnel:admin")])
    if callback.message:
        await callback.message.answer("Выберите параметр для изменения:", reply_markup=keyboard(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("funnel:edit:"))
async def edit_field(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    field = (callback.data or "").split(":", 2)[2]
    if field not in EDITABLE_FIELDS:
        await callback.answer("Неизвестное поле", show_alert=True)
        return
    redis = redis_client()
    try:
        await redis.set(f"{EDIT_STATE_PREFIX}{callback.from_user.id}", field, ex=900)
    finally:
        await redis.aclose()
    if callback.message:
        await callback.message.answer(f"Отправьте новое значение для поля «{EDITABLE_FIELDS[field]}».\n\nДля отмены отправьте /cancel.")
    await callback.answer()


class FunnelEditFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user or not await is_admin(message.from_user.id):
            return False
        redis = redis_client()
        try:
            return bool(await redis.get(f"{EDIT_STATE_PREFIX}{message.from_user.id}"))
        finally:
            await redis.aclose()


@router.message(FunnelEditFilter(), F.text)
async def receive_edit(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    redis = redis_client()
    try:
        key = f"{EDIT_STATE_PREFIX}{message.from_user.id}"
        field = await redis.get(key)
        await redis.delete(key)
    finally:
        await redis.aclose()
    if message.text.strip() == "/cancel":
        await message.answer("Изменение отменено.")
        return
    if not field or field not in EDITABLE_FIELDS:
        return
    await save_funnel_config({field: message.text})
    await message.answer("✅ Настройка сохранена.")
    await show_funnel_admin(message)
