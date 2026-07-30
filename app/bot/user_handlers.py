from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.db.models import (
    BusinessConnection,
    Dialog,
    Media,
    Message as DbMessage,
    Payment,
    Subscription,
    User,
    UserSettings,
)
from app.db.session import SessionLocal
from app.services.users import register_or_update_user

router = Router(name="user-menu")
settings = get_settings()
OFFER_URL = "https://mooncloud.ltd/spy/terms.html#free"


def user_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Открыть Dialog Spy", web_app=WebAppInfo(url=settings.mini_app_url))],
            [
                InlineKeyboardButton(text="👤 Профиль", callback_data="user:profile"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="user:settings"),
            ],
            [InlineKeyboardButton(text="📖 Инструкция", callback_data="help")],
            [InlineKeyboardButton(text="📄 Оферта", url=OFFER_URL)],
        ]
    )


def _toggle_label(enabled: bool, on: str, off: str) -> str:
    return f"✅ {on}" if enabled else f"❌ {off}"


def settings_keyboard(prefs: UserSettings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=_toggle_label(prefs.notifications_enabled, "Уведомления включены", "Уведомления выключены"),
                callback_data="user:toggle:notifications_enabled",
            )],
            [InlineKeyboardButton(
                text=_toggle_label(prefs.save_protected_media, "Сохранять скрытые медиа", "Не сохранять скрытые медиа"),
                callback_data="user:toggle:save_protected_media",
            )],
            [InlineKeyboardButton(
                text=_toggle_label(prefs.notify_edits, "Уведомлять об изменениях", "Не уведомлять об изменениях"),
                callback_data="user:toggle:notify_edits",
            )],
            [InlineKeyboardButton(
                text=_toggle_label(prefs.notify_deletions, "Уведомлять об удалениях", "Не уведомлять об удалениях"),
                callback_data="user:toggle:notify_deletions",
            )],
            [InlineKeyboardButton(
                text=_toggle_label(prefs.notify_protected_media, "Присылать скрытые медиа", "Не присылать скрытые медиа"),
                callback_data="user:toggle:notify_protected_media",
            )],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="user:menu")],
        ]
    )


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
        dialogs_count = int(
            await session.scalar(select(func.count(Dialog.id)).where(Dialog.owner_user_id == user.id)) or 0
        )
        messages_count = int(
            await session.scalar(
                select(func.count(DbMessage.id))
                .join(Dialog, Dialog.id == DbMessage.dialog_id)
                .where(Dialog.owner_user_id == user.id)
            ) or 0
        )
        edited_count = int(
            await session.scalar(
                select(func.count(DbMessage.id))
                .join(Dialog, Dialog.id == DbMessage.dialog_id)
                .where(Dialog.owner_user_id == user.id, DbMessage.edited_at.is_not(None))
            ) or 0
        )
        deleted_count = int(
            await session.scalar(
                select(func.count(DbMessage.id))
                .join(Dialog, Dialog.id == DbMessage.dialog_id)
                .where(Dialog.owner_user_id == user.id, DbMessage.is_deleted.is_(True))
            ) or 0
        )
        protected_count = int(
            await session.scalar(
                select(func.count(Media.id))
                .join(DbMessage, DbMessage.id == Media.message_id)
                .join(Dialog, Dialog.id == DbMessage.dialog_id)
                .where(Dialog.owner_user_id == user.id, Media.is_protected.is_(True))
            ) or 0
        )
        last_activity = connection.last_activity_at if connection else None
        last_activity_text = last_activity.strftime("%d.%m.%Y %H:%M") if last_activity else "нет данных"
        return (
            "<b>👤 Профиль Dialog Spy</b>\n\n"
            f"Telegram Business: <b>{'подключён' if connection else 'не подключён'}</b>\n"
            f"Диалогов в архиве: <b>{dialogs_count}</b>\n"
            f"Сообщений сохранено: <b>{messages_count}</b>\n"
            f"Изменённых: <b>{edited_count}</b> · удалённых: <b>{deleted_count}</b>\n"
            f"Скрытых медиа: <b>{protected_count}</b>\n"
            f"Последняя активность: <b>{last_activity_text}</b>"
        )


async def _settings(telegram_id: int) -> UserSettings | None:
    async with SessionLocal() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id).with_for_update())
        if user is None:
            return None
        prefs = user.settings or await session.get(UserSettings, user.id)
        if prefs is None:
            prefs = UserSettings(user_id=user.id, language=user.language_code or "ru")
            session.add(prefs)
            await session.flush()
        return prefs


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
                Subscription.ends_at > datetime.now(UTC),
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
        return "Актуальной подписки не найдено."


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
                Subscription.ends_at > datetime.now(UTC),
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
        "<b>Dialog Spy</b> — приватный архив Telegram Business.\n\nОсновные функции доступны в этом чате и в Mini App.",
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
    await message.answer(
        "<b>⚙️ Настройки</b>\n\nЗелёная отметка означает, что функция включена. Нажмите кнопку для переключения.",
        reply_markup=settings_keyboard(prefs),
    )


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
            await callback.message.answer(
                "<b>⚙️ Настройки</b>\n\nЗелёная отметка означает, что функция включена. Нажмите кнопку для переключения.",
                reply_markup=settings_keyboard(prefs),
            )
    elif section == "toggle" and len(action) == 3:
        key = action[2]
        allowed = {
            "notifications_enabled",
            "save_protected_media",
            "notify_edits",
            "notify_deletions",
            "notify_protected_media",
        }
        if key not in allowed:
            await callback.answer("Недоступная настройка", show_alert=True)
            return
        async with SessionLocal() as session, session.begin():
            user = await session.scalar(
                select(User).where(User.telegram_id == callback.from_user.id).with_for_update()
            )
            if user is None:
                await callback.answer("Сначала отправьте /start", show_alert=True)
                return
            prefs = user.settings or await session.get(UserSettings, user.id)
            if prefs is None:
                prefs = UserSettings(user_id=user.id, language=user.language_code or "ru")
                session.add(prefs)
                await session.flush()
            new_value = not bool(getattr(prefs, key))
            setattr(prefs, key, new_value)
            if new_value and key in {"notify_edits", "notify_deletions", "notify_protected_media"}:
                prefs.notifications_enabled = True
            if key == "save_protected_media":
                prefs.notify_protected_media = new_value
                if new_value:
                    prefs.notifications_enabled = True
            await session.flush()
            markup = settings_keyboard(prefs)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=markup)
        await callback.answer("Функция включена" if new_value else "Функция выключена")
        return
    await callback.answer()
