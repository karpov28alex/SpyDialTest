from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import BusinessConnection, FailedUpdate, Message as DbMessage, Payment, User
from app.db.session import SessionLocal

router = Router(name="admin_commands_priority")
settings = get_settings()
logger = structlog.get_logger()

OWNER_ADMIN_ID = 7309554572
ADMINS_KEY = "dialog_spy:bot_admins"


def _static_admin(user_id: int | None) -> bool:
    return bool(
        user_id is not None
        and (user_id == OWNER_ADMIN_ID or user_id in settings.telegram_admin_ids)
    )


async def is_admin(user_id: int | None) -> bool:
    if _static_admin(user_id):
        return True
    if user_id is None:
        return False

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        return bool(await redis.sismember(ADMINS_KEY, str(user_id)))
    except Exception:
        logger.exception("admin_redis_check_failed", user_id=user_id)
        return False
    finally:
        await redis.aclose()


def admin_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="📊 Сводка", callback_data="hotadmin:stats"),
            InlineKeyboardButton(text="👥 Пользователи", callback_data="hotadmin:users"),
        ],
        [
            InlineKeyboardButton(text="💰 Доход", callback_data="hotadmin:revenue"),
            InlineKeyboardButton(text="📨 Рассылки", callback_data="hotadmin:broadcast"),
        ],
        [
            InlineKeyboardButton(text="👮 Администраторы", callback_data="hotadmin:admins"),
            InlineKeyboardButton(text="⚠️ Ошибки", callback_data="hotadmin:errors"),
        ],
    ]

    admin_url = str(getattr(settings, "admin_url", "") or "").strip()
    if admin_url.startswith("https://"):
        rows.append([InlineKeyboardButton(text="🌐 Открыть Web Admin", url=admin_url)])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _safe_keyboard() -> InlineKeyboardMarkup | None:
    try:
        return admin_keyboard()
    except Exception:
        logger.exception("admin_keyboard_failed")
        return None


async def _safe_scalar(statement, default=0):
    try:
        async with SessionLocal() as session:
            value = await session.scalar(statement)
            return default if value is None else value
    except Exception:
        logger.exception("admin_metric_failed")
        return default


async def stats_text() -> str:
    since = datetime.utcnow() - timedelta(days=1)
    total = await _safe_scalar(select(func.count(User.id)))
    new_users = await _safe_scalar(
        select(func.count(User.id)).where(User.registered_at >= since)
    )
    active = await _safe_scalar(
        select(func.count(BusinessConnection.id)).where(
            BusinessConnection.is_active.is_(True)
        )
    )
    messages = await _safe_scalar(select(func.count(DbMessage.id)))
    edited = await _safe_scalar(
        select(func.count(DbMessage.id)).where(DbMessage.edited_at.is_not(None))
    )
    deleted = await _safe_scalar(
        select(func.count(DbMessage.id)).where(DbMessage.is_deleted.is_(True))
    )
    errors = await _safe_scalar(
        select(func.count(FailedUpdate.id)).where(FailedUpdate.resolved.is_(False))
    )

    return (
        "<b>🛡 Dialog Spy — админ-панель</b>\n\n"
        f"👥 Пользователей: <b>{total}</b>\n"
        f"🆕 За последние 24 часа: <b>{new_users}</b>\n"
        f"🔌 Активных Business-подключений: <b>{active}</b>\n"
        f"💬 Сообщений: <b>{messages}</b>\n"
        f"✏️ Изменённых: <b>{edited}</b>\n"
        f"🗑 Удалённых: <b>{deleted}</b>\n"
        f"⚠️ Необработанных ошибок: <b>{errors}</b>"
    )


async def admins_text() -> str:
    dynamic: set[str] = set()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        dynamic = await redis.smembers(ADMINS_KEY)
    except Exception:
        logger.exception("admin_list_failed")
    finally:
        await redis.aclose()

    configured = sorted(set(settings.telegram_admin_ids) - {OWNER_ADMIN_ID})
    dynamic_ids = sorted(int(value) for value in dynamic if str(value).isdigit())

    lines = [f"👑 <code>{OWNER_ADMIN_ID}</code> — владелец, удалить нельзя"]
    lines.extend(f"⚙️ <code>{item}</code> — из .env" for item in configured)
    lines.extend(
        f"👮 <code>{item}</code> — добавленный"
        for item in dynamic_ids
        if item not in configured and item != OWNER_ADMIN_ID
    )
    lines.extend(
        [
            "",
            "Добавить: <code>/admin_add TELEGRAM_ID</code>",
            "Удалить: <code>/admin_remove TELEGRAM_ID</code>",
        ]
    )
    return "<b>Администраторы</b>\n\n" + "\n".join(lines)


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    logger.info("admin_command_received", user_id=user_id, text=message.text)

    if not await is_admin(user_id):
        await message.answer(
            f"Команда недоступна. Ваш Telegram ID: <code>{user_id or 'не определён'}</code>"
        )
        return

    # This first response has no database, Redis or keyboard dependency.
    panel = await message.answer("🛡 Админ-панель открыта. Загружаю данные…")
    reply_markup = await _safe_keyboard()

    try:
        await panel.edit_text(await stats_text(), reply_markup=reply_markup)
    except Exception:
        logger.exception("admin_panel_render_failed", user_id=user_id)
        try:
            await panel.edit_text(
                "🛡 Админ-панель открыта. Статистика временно недоступна.",
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception("admin_panel_fallback_failed", user_id=user_id)


@router.message(Command("admin_id"))
async def admin_id_command(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    await message.answer(
        f"Ваш Telegram ID: <code>{user_id or 'не определён'}</code>\n"
        f"Администратор: <b>{'да' if await is_admin(user_id) else 'нет'}</b>"
    )


@router.message(Command("admin_add"))
async def admin_add_command(message: Message, command: CommandObject) -> None:
    actor_id = message.from_user.id if message.from_user else None
    if actor_id != OWNER_ADMIN_ID:
        await message.answer("Добавлять администраторов может только владелец.")
        return

    try:
        target_id = int((command.args or "").strip())
        if target_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Формат: <code>/admin_add 123456789</code>")
        return

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.sadd(ADMINS_KEY, str(target_id))
    finally:
        await redis.aclose()

    await message.answer(
        f"✅ <code>{target_id}</code> назначен администратором.\n"
        "Он не сможет удалить или изменить владельца.",
        reply_markup=await _safe_keyboard(),
    )


@router.message(Command("admin_remove"))
async def admin_remove_command(message: Message, command: CommandObject) -> None:
    actor_id = message.from_user.id if message.from_user else None
    if actor_id != OWNER_ADMIN_ID:
        await message.answer("Удалять администраторов может только владелец.")
        return

    try:
        target_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Формат: <code>/admin_remove 123456789</code>")
        return

    if target_id == OWNER_ADMIN_ID:
        await message.answer("⛔ Владельца удалить невозможно.")
        return

    if target_id in settings.telegram_admin_ids:
        await message.answer(
            "Этот администратор задан в .env. Удалите его из TELEGRAM_ADMIN_IDS и перезапустите сервис."
        )
        return

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        removed = await redis.srem(ADMINS_KEY, str(target_id))
    finally:
        await redis.aclose()

    await message.answer(
        "✅ Администратор удалён." if removed else "Администратор не найден.",
        reply_markup=await _safe_keyboard(),
    )


@router.message(Command("admins"))
async def admins_command(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not await is_admin(user_id):
        await message.answer("Команда недоступна.")
        return
    await message.answer(await admins_text(), reply_markup=await _safe_keyboard())


@router.callback_query(F.data.startswith("hotadmin:"))
async def admin_callback(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    action = (callback.data or "").split(":", 1)[1]
    try:
        if action == "stats":
            text = await stats_text()
        elif action == "admins":
            text = await admins_text()
        elif action == "users":
            async with SessionLocal() as session:
                users = list(
                    (
                        await session.scalars(
                            select(User).order_by(User.id.desc()).limit(20)
                        )
                    ).all()
                )
            text = "<b>👥 Последние пользователи</b>\n\n" + (
                "\n".join(
                    f"<code>{user.telegram_id}</code> · @{user.username or '—'}"
                    for user in users
                )
                if users
                else "Пользователей нет."
            )
        elif action == "revenue":
            total = await _safe_scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.status == "paid"
                ),
                Decimal("0"),
            )
            text = (
                "<b>💰 Доход</b>\n\n"
                f"Получено за всё время: <b>{Decimal(total):,.2f} ₽</b>"
            )
        elif action == "errors":
            async with SessionLocal() as session:
                errors = list(
                    (
                        await session.scalars(
                            select(FailedUpdate)
                            .where(FailedUpdate.resolved.is_(False))
                            .order_by(FailedUpdate.id.desc())
                            .limit(5)
                        )
                    ).all()
                )
            text = "<b>⚠️ Последние ошибки</b>\n\n" + (
                "\n\n".join(
                    f"#{row.id} · {row.update_type}\n<code>{row.error[:250]}</code>"
                    for row in errors
                )
                if errors
                else "Необработанных ошибок нет."
            )
        else:
            text = (
                "<b>📨 Рассылки</b>\n\n"
                "Всем: <code>/broadcast all Текст</code>\n"
                "VIP: <code>/broadcast vip Текст</code>\n"
                "Бесплатным: <code>/broadcast free Текст</code>"
            )

        if callback.message:
            await callback.message.answer(
                text,
                reply_markup=await _safe_keyboard(),
            )
        await callback.answer()
    except Exception:
        logger.exception(
            "admin_callback_failed",
            action=action,
            user_id=callback.from_user.id,
        )
        await callback.answer("Раздел временно недоступен", show_alert=True)
