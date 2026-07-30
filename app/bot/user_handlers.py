from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.models import BusinessConnection, Payment, Subscription, SubscriptionStatus, User, UserSettings
from app.db.session import SessionLocal
from app.services.users import register_or_update_user

router = Router(name="user-menu")
settings = get_settings()


def user_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Открыть Dialog Spy", web_app=WebAppInfo(url=settings.mini_app_url))],
            [
                InlineKeyboardButton(text="👤 Профиль", callback_data="user:profile"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="user:settings"),
            ],
            [
                InlineKeyboardButton(text="💎 Подписка", callback_data="user:subscription"),
                InlineKeyboardButton(text="🚫 Отменить подписку", callback_data="user:cancel"),
            ],
            [InlineKeyboardButton(text="📖 Инструкция", callback_data="help")],
        ]
    )


def settings_keyboard(prefs: UserSettings) -> InlineKeyboardMarkup:
    notification_text = "🔔 Уведомления: включены" if prefs.notifications_enabled else "🔕 Уведомления: выключены"
    media_text = "🛡 Скрытые медиа: сохранять" if prefs.save_protected_media else "🛡 Скрытые медиа: не сохранять"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=notification_text, callback_data="user:toggle:notifications_enabled")],
            [InlineKeyboardButton(text=media_text, callback_data="user:toggle:save_protected_media")],
            [InlineKeyboardButton(text="✏️ Изменения сообщений", callback_data="user:toggle:notify_edits")],
            [InlineKeyboardButton(text="🗑 Удалённые сообщения", callback_data="user:toggle:notify_deletions")],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="user:menu")],
        ]
    )


async def _user(telegram_id: int) -> User | None:
    async with SessionLocal() as session:
        return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def _profile_text(telegram_id: int) -> str:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return "Профиль ещё не создан. Отправьте /start."
        connection = await session.scalar(
            select(BusinessConnection)
            .where(BusinessConnection.owner_user_id == user.id, BusinessConnection.is_active.is_(True))
            .order_by(BusinessConnection.id.desc())
        )
        prefs = user.settings or await session.get(UserSettings, user.id)
        status = user.subscription_status.value if hasattr(user.subscription_status, "value") else str(user.subscription_status)
        return (
            "<b>👤 Профиль Dialog Spy</b>\n\n"
            f"Telegram ID: <code>{user.telegram_id}</code>\n"
            f"Telegram Business: <b>{'подключён' if connection else 'не подключён'}</b>\n"
            f"Доступ: <b>{'активен' if status in {'trial', 'referral', 'vip', 'active'} and not user.is_access_disabled else 'не активен'}</b>\n"
            f"Уведомления: <b>{'включены' if prefs and prefs.notifications_enabled else 'выключены'}</b>\n"
            f"Сохранение скрытых медиа: <b>{'включено' if prefs and prefs.save_protected_media else 'выключено'}</b>"
        )


async def _settings(telegram_id: int) -> UserSettings | None:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return None
        return user.settings or await session.get(UserSettings, user.id)


async def _subscription_text(telegram_id: int) -> str:
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return "Профиль ещё не создан. Отправьте /start."
        subscription = await session.scalar(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.status.in_(["active", "vip"]),
                Subscription.ends_at > datetime.utcnow(),
            )
            .order_by(Subscription.ends_at.desc())
        )
        if subscription:
            cancelled = "auto_renew_cancelled" in (subscription.source or "")
            return (
                "<b>💎 VIP-подписка</b>\n\n"
                f"Статус: <b>активна</b>\n"
                f"Действует до: <b>{subscription.ends_at:%d.%m.%Y %H:%M}</b>\n"
                f"Автопродление: <b>{'отключено' if cancelled else 'включено'}</b>"
            )
        return (
            "<b>💎 VIP-подписка</b>\n\n"
            "Стоимость пробной VIP подписки — <b>20 ₽ за 1 день</b>.\n"
            "Далее — 125 ₽ каждые 7 дней. Возможно частичное списание 70 ₽ за 3 дня."
        )


async def cancel_subscription(telegram_id: int) -> bool:
    async with SessionLocal() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return False
        subscription = await session.scalar(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.status.in_(["active", "vip"]),
                Subscription.ends_at > datetime.utcnow(),
            )
            .order_by(Subscription.ends_at.desc())
            .with_for_update()
        )
        if subscription is None:
            return False
        marker = "auto_renew_cancelled"
        source = subscription.source or "payment"
        if marker not in source:
            subscription.source = f"{source}:{marker}"
        await session.execute(
            update(Payment)
            .where(Payment.user_id == user.id, Payment.recurring.is_(True))
            .values(recurring=False)
        )
        return True


@router.message(Command("start"))
async def start(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    async with SessionLocal() as session, session.begin():
        await register_or_update_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            language_code=message.from_user.language_code,
            start_parameter=command.args,
        )
    await message.answer(
        "<b>Dialog Spy</b> — приватный архив Telegram Business.\n\nВсе основные функции доступны прямо в этом чате и в Mini App.",
        reply_markup=user_keyboard(),
    )


@router.message(Command("menu"))
async def menu_command(message: Message) -> None:
    await message.answer("Выберите раздел:", reply_markup=user_keyboard())


@router.message(Command("profile"))
async def profile_command(message: Message) -> None:
    if message.from_user:
        await message.answer(await _profile_text(message.from_user.id), reply_markup=user_keyboard())


@router.message(Command("settings"))
async def settings_command(message: Message) -> None:
    if not message.from_user:
        return
    prefs = await _settings(message.from_user.id)
    if prefs is None:
        await message.answer("Профиль ещё не создан. Отправьте /start.")
        return
    await message.answer("<b>⚙️ Настройки</b>\n\nНажмите кнопку, чтобы изменить параметр.", reply_markup=settings_keyboard(prefs))


@router.message(Command("subscription"))
async def subscription_command(message: Message) -> None:
    if message.from_user:
        await message.answer(await _subscription_text(message.from_user.id), reply_markup=user_keyboard())


@router.message(Command("cancel"))
async def cancel_command(message: Message) -> None:
    if not message.from_user:
        return
    if await cancel_subscription(message.from_user.id):
        await message.answer(
            "✅ Автоматическое продление отключено. VIP-доступ сохранится до конца уже оплаченного периода.",
            reply_markup=user_keyboard(),
        )
    else:
        await message.answer("Актуальной подписки не найдено.", reply_markup=user_keyboard())


@router.callback_query(F.data.startswith("user:"))
async def user_callback(callback: CallbackQuery) -> None:
    action = callback.data.split(":")
    if len(action) < 2:
        await callback.answer()
        return
    section = action[1]
    if section == "menu":
        if callback.message:
            await callback.message.answer("Выберите раздел:", reply_markup=user_keyboard())
    elif section == "profile":
        if callback.message:
            await callback.message.answer(await _profile_text(callback.from_user.id), reply_markup=user_keyboard())
    elif section == "settings":
        prefs = await _settings(callback.from_user.id)
        if callback.message and prefs:
            await callback.message.answer("<b>⚙️ Настройки</b>\n\nНажмите кнопку, чтобы изменить параметр.", reply_markup=settings_keyboard(prefs))
    elif section == "subscription":
        if callback.message:
            await callback.message.answer(await _subscription_text(callback.from_user.id), reply_markup=user_keyboard())
    elif section == "cancel":
        if callback.message:
            if await cancel_subscription(callback.from_user.id):
                await callback.message.answer("✅ Автоматическое продление отключено. VIP-доступ сохранится до конца оплаченного периода.", reply_markup=user_keyboard())
            else:
                await callback.message.answer("Актуальной подписки не найдено.", reply_markup=user_keyboard())
    elif section == "toggle" and len(action) == 3:
        key = action[2]
        allowed = {"notifications_enabled", "save_protected_media", "notify_edits", "notify_deletions"}
        if key not in allowed:
            await callback.answer("Недоступная настройка", show_alert=True)
            return
        async with SessionLocal() as session, session.begin():
            user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
            prefs = user.settings if user else None
            if prefs is None and user:
                prefs = await session.get(UserSettings, user.id)
            if prefs is None:
                await callback.answer("Сначала отправьте /start", show_alert=True)
                return
            setattr(prefs, key, not bool(getattr(prefs, key)))
            await session.flush()
            markup = settings_keyboard(prefs)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=markup)
        await callback.answer("Настройка сохранена")
        return
    await callback.answer()
