from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    WebAppInfo,
)
from PIL import Image, ImageDraw, ImageFont
from redis.asyncio import Redis
from sqlalchemy import func, or_, select

from app.core.config import get_settings
from app.db.models import (
    BusinessConnection,
    FailedUpdate,
    Job,
    Message as DbMessage,
    Payment,
    SubscriptionStatus,
    User,
)
from app.db.session import SessionLocal
from app.services.users import register_or_update_user

router = Router(name="admin_console")
settings = get_settings()
logger = structlog.get_logger()

OWNER_ADMIN_ID = 7309554572
ADMINS_KEY = "dialog_spy:bot_admins"
STATE_PREFIX = "dialog_spy:admin_state:"
INSTRUCTION_KEY = "dialog_spy:bot_instruction_v2"
LEGACY_INSTRUCTION_KEY = "dialog_spy:bot_instruction"
CAMPAIGNS_KEY = "dialog_spy:campaigns"
CAMPAIGN_PREFIX = "dialog_spy:campaign:"
CAMPAIGN_USERS_PREFIX = "dialog_spy:campaign_users:"
BROADCAST_HISTORY_KEY = "dialog_spy:broadcast_history"
DEFAULT_INSTRUCTION = (
    "<b>Инструкция по подключению Dialog Spy</b>\n\n"
    "1. Откройте Telegram → Настройки.\n"
    "2. Перейдите в Telegram Business → Чат-боты.\n"
    "3. Выберите Dialog Spy и разрешите доступ к сообщениям.\n"
    "4. Нажмите «Сохранить».\n"
    "5. Архив и уведомления появятся после подключения.\n\n"
    "Подключение Telegram Business может быть доступно и без Premium."
)


def redis_client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def static_admin(user_id: int | None) -> bool:
    return bool(user_id and (user_id == OWNER_ADMIN_ID or user_id in settings.telegram_admin_ids))


async def is_admin(user_id: int | None) -> bool:
    if static_admin(user_id):
        return True
    if not user_id:
        return False
    redis = redis_client()
    try:
        return bool(await redis.sismember(ADMINS_KEY, str(user_id)))
    except Exception:
        logger.exception("admin_check_failed", user_id=user_id)
        return False
    finally:
        await redis.aclose()


async def get_state(user_id: int) -> dict[str, Any] | None:
    redis = redis_client()
    try:
        raw = await redis.get(f"{STATE_PREFIX}{user_id}")
        return json.loads(raw) if raw else None
    finally:
        await redis.aclose()


async def set_state(user_id: int, state: dict[str, Any]) -> None:
    redis = redis_client()
    try:
        await redis.set(f"{STATE_PREFIX}{user_id}", json.dumps(state, ensure_ascii=False), ex=3600)
    finally:
        await redis.aclose()


async def clear_state(user_id: int) -> None:
    redis = redis_client()
    try:
        await redis.delete(f"{STATE_PREFIX}{user_id}")
    finally:
        await redis.aclose()


class AdminStateFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return bool(
            message.from_user
            and await is_admin(message.from_user.id)
            and await get_state(message.from_user.id)
        )


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def home_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="crm:analytics"), InlineKeyboardButton(text="👥 Пользователи", callback_data="crm:users")],
        [InlineKeyboardButton(text="📨 Рассылки", callback_data="crm:broadcasts"), InlineKeyboardButton(text="📣 Реклама", callback_data="crm:campaigns")],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="crm:instruction"), InlineKeyboardButton(text="👮 Администраторы", callback_data="crm:admins")],
        [InlineKeyboardButton(text="⚠️ Ошибки", callback_data="crm:errors"), InlineKeyboardButton(text="🖥 Система", callback_data="crm:system")],
    ]
    admin_url = str(getattr(settings, "admin_url", "") or "").strip()
    if admin_url.startswith("https://"):
        rows.append([InlineKeyboardButton(text="🌐 Web Admin", web_app=WebAppInfo(url=admin_url))])
    return keyboard(rows)


def back(target: str = "home") -> InlineKeyboardMarkup:
    return keyboard([[InlineKeyboardButton(text="◀️ Назад", callback_data=f"crm:{target}")]])


def user_menu(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📱 Открыть Dialog Spy", web_app=WebAppInfo(url=settings.mini_app_url))],
        [InlineKeyboardButton(text="📖 Инструкция по пользованию", callback_data="crm:help")],
    ]
    if admin:
        rows.append([InlineKeyboardButton(text="🛡 Админ-панель", callback_data="crm:home")])
    return keyboard(rows)


def broadcasts_keyboard() -> InlineKeyboardMarkup:
    return keyboard([
        [InlineKeyboardButton(text="➕ Создать рассылку", callback_data="crm:broadcast_new")],
        [InlineKeyboardButton(text="📋 Черновик", callback_data="crm:broadcast_draft"), InlineKeyboardButton(text="📊 История", callback_data="crm:broadcast_history")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="crm:home")],
    ])


def instruction_keyboard() -> InlineKeyboardMarkup:
    return keyboard([
        [InlineKeyboardButton(text="👁 Предпросмотр", callback_data="crm:instruction_preview")],
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data="crm:instruction_text")],
        [InlineKeyboardButton(text="➕ Добавить медиа", callback_data="crm:instruction_media")],
        [InlineKeyboardButton(text="🗑 Очистить медиа", callback_data="crm:instruction_clear")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="crm:home")],
    ])


def users_keyboard() -> InlineKeyboardMarkup:
    return keyboard([
        [InlineKeyboardButton(text="🔎 Найти пользователя", callback_data="crm:user_search")],
        [InlineKeyboardButton(text="📥 Экспорт CSV", callback_data="crm:user_export")],
        [InlineKeyboardButton(text="💎 VIP", callback_data="crm:user_segment:vip"), InlineKeyboardButton(text="🆓 Бесплатные", callback_data="crm:user_segment:free")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="crm:home")],
    ])


async def safe_scalar(statement, default: Any = 0) -> Any:
    try:
        async with SessionLocal() as session:
            value = await session.scalar(statement)
            return default if value is None else value
    except Exception:
        logger.exception("admin_metric_failed")
        return default


async def stats(days: int) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(days=days)
    total = int(await safe_scalar(select(func.count(User.id))))
    vip = int(await safe_scalar(select(func.count(User.id)).where(User.subscription_status.in_([SubscriptionStatus.vip, SubscriptionStatus.active]))))
    blocked = int(await safe_scalar(select(func.count(User.id)).where(User.blocked_bot_at.is_not(None))))
    return {
        "days": days,
        "total": total,
        "new": int(await safe_scalar(select(func.count(User.id)).where(User.registered_at >= since))),
        "vip": vip,
        "free": max(total - vip, 0),
        "blocked": blocked,
        "alive": max(total - blocked, 0),
        "connections": int(await safe_scalar(select(func.count(BusinessConnection.id)).where(BusinessConnection.is_active.is_(True)))),
        "revenue": Decimal(await safe_scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid", Payment.paid_at >= since), Decimal("0"))),
        "messages": int(await safe_scalar(select(func.count(DbMessage.id)).where(DbMessage.sent_at >= since))),
    }


def chart_png(data: dict[str, Any]) -> bytes:
    image = Image.new("RGB", (1200, 720), "#090713")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=28)
    title = ImageFont.load_default(size=40)
    draw.rounded_rectangle((35, 35, 1165, 685), 35, fill="#120d22", outline="#7738e6", width=4)
    draw.text((75, 70), f"Dialog Spy · {data['days']} дн.", font=title, fill="#ffffff")
    values = [("Живые", data["alive"], "#55d69d"), ("Заблокировали", data["blocked"], "#ff668a"), ("VIP", data["vip"], "#ad6cff"), ("Бесплатные", data["free"], "#718cff")]
    maximum = max([item[1] for item in values] + [1])
    y = 180
    for label, value, color in values:
        draw.text((80, y), label, font=font, fill="#ffffff")
        draw.rounded_rectangle((360, y, 1010, y + 38), 18, fill="#28203d")
        width = int(650 * value / maximum)
        if width:
            draw.rounded_rectangle((360, y, 360 + width, y + 38), 18, fill=color)
        draw.text((1040, y), str(value), font=font, fill="#ffffff")
        y += 95
    draw.text((80, 610), f"Новые: {data['new']} · Доход: {data['revenue']:,.2f} ₽ · Сообщения: {data['messages']}", font=font, fill="#d9cdf2")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def send_analytics(message: Message, days: int) -> None:
    data = await stats(days)
    caption = (
        f"<b>📊 Сводка за {days} дн.</b>\n\n"
        f"Новых: <b>{data['new']}</b>\nЖивых: <b>{data['alive']}</b> · заблокировали: <b>{data['blocked']}</b>\n"
        f"VIP: <b>{data['vip']}</b> · бесплатных: <b>{data['free']}</b>\n"
        f"Доход: <b>{data['revenue']:,.2f} ₽</b>\nСообщений: <b>{data['messages']}</b>\nBusiness: <b>{data['connections']}</b>"
    )
    ranges = keyboard([
        [InlineKeyboardButton(text="Сегодня", callback_data="crm:range:1"), InlineKeyboardButton(text="7 дней", callback_data="crm:range:7"), InlineKeyboardButton(text="30 дней", callback_data="crm:range:30")],
        [InlineKeyboardButton(text="🗓 Свой период", callback_data="crm:range:custom")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="crm:home")],
    ])
    try:
        await message.answer_photo(BufferedInputFile(chart_png(data), filename=f"analytics-{days}.png"), caption=caption, reply_markup=ranges)
    except Exception:
        logger.exception("analytics_chart_failed")
        await message.answer(caption, reply_markup=ranges)


async def instruction_data() -> dict[str, Any]:
    redis = redis_client()
    try:
        raw = await redis.get(INSTRUCTION_KEY)
        if raw:
            data = json.loads(raw)
            return {"text": data.get("text") or DEFAULT_INSTRUCTION, "media": data.get("media") or []}
        legacy = await redis.hgetall(LEGACY_INSTRUCTION_KEY)
        media = [{"type": "video", "file_id": legacy[key]} for key in ("video1", "video2") if legacy.get(key)]
        return {"text": legacy.get("text") or DEFAULT_INSTRUCTION, "media": media}
    finally:
        await redis.aclose()


async def save_instruction(data: dict[str, Any]) -> None:
    redis = redis_client()
    try:
        await redis.set(INSTRUCTION_KEY, json.dumps(data, ensure_ascii=False))
    finally:
        await redis.aclose()


async def send_instruction(message: Message) -> None:
    data = await instruction_data()
    media = data["media"][:10]
    album = [InputMediaPhoto(media=x["file_id"]) if x["type"] == "photo" else InputMediaVideo(media=x["file_id"]) for x in media]
    if len(album) > 1:
        await message.answer_media_group(album)
    elif len(album) == 1:
        if media[0]["type"] == "photo":
            await message.answer_photo(media[0]["file_id"])
        else:
            await message.answer_video(media[0]["file_id"], supports_streaming=True)
    await message.answer(data["text"], reply_markup=user_menu(await is_admin(message.chat.id)))


@router.message(CommandStart())
async def start(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    async with SessionLocal() as session, session.begin():
        user, created = await register_or_update_user(session, telegram_id=message.from_user.id, username=message.from_user.username, first_name=message.from_user.first_name, last_name=message.from_user.last_name, language_code=message.from_user.language_code, start_parameter=command.args)
    if created and command.args and command.args.startswith("ad_"):
        code = command.args.removeprefix("ad_")
        redis = redis_client()
        try:
            if await redis.exists(f"{CAMPAIGN_PREFIX}{code}"):
                await redis.sadd(f"{CAMPAIGN_USERS_PREFIX}{code}", str(user.telegram_id))
                await redis.hincrby(f"{CAMPAIGN_PREFIX}{code}", "registrations", 1)
        finally:
            await redis.aclose()
    await message.answer("<b>Dialog Spy</b> — приватный архив Telegram Business.", reply_markup=user_menu(await is_admin(message.from_user.id)))


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not await is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Нет доступа.")
        return
    await clear_state(message.from_user.id)
    await message.answer("<b>🛡 Dialog Spy — центр управления</b>\n\nВсе действия доступны по кнопкам.", reply_markup=home_keyboard())


@router.callback_query(F.data.startswith("crm:"))
async def callbacks(callback: CallbackQuery) -> None:
    message = callback.message
    if not message:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    action = parts[1]
    user_id = callback.from_user.id
    if action == "help":
        await send_instruction(message)
        await callback.answer()
        return
    if not await is_admin(user_id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        if action in {"home", "cancel"}:
            await clear_state(user_id)
            await message.answer("<b>🛡 Центр управления</b>", reply_markup=home_keyboard())
        elif action == "analytics":
            await send_analytics(message, 7)
        elif action == "range":
            if parts[2] == "custom":
                await set_state(user_id, {"mode": "analytics_dates"})
                await message.answer("Введите период: <code>01.07.2026 - 31.07.2026</code>", reply_markup=back("analytics"))
            else:
                await send_analytics(message, int(parts[2]))
        elif action == "users":
            await clear_state(user_id)
            await message.answer("<b>👥 Пользователи</b>", reply_markup=users_keyboard())
        elif action == "user_search":
            await set_state(user_id, {"mode": "user_search"})
            await message.answer("Введите Telegram ID, username или имя.", reply_markup=back("users"))
        elif action == "user_export":
            await export_users(message)
        elif action == "user_segment":
            await show_segment(message, parts[2])
        elif action == "user":
            await show_user(message, int(parts[2]))
        elif action == "broadcasts":
            await clear_state(user_id)
            await message.answer("<b>📨 Рассылки</b>", reply_markup=broadcasts_keyboard())
        elif action == "broadcast_new":
            await set_state(user_id, {"mode": "broadcast_audience", "audience": None, "text": "", "media": [], "buttons": []})
            await message.answer("Кому отправить?", reply_markup=keyboard([[InlineKeyboardButton(text="👥 Всем", callback_data="crm:audience:all")], [InlineKeyboardButton(text="💎 VIP", callback_data="crm:audience:vip"), InlineKeyboardButton(text="🆓 Бесплатным", callback_data="crm:audience:free")], [InlineKeyboardButton(text="✖️ Отмена", callback_data="crm:cancel")]]))
        elif action == "audience":
            state = await get_state(user_id) or {}
            state.update({"mode": "broadcast_content", "audience": parts[2]})
            await set_state(user_id, state)
            await message.answer("Отправьте текст, фото, видео или альбом до 10 файлов. Затем нажмите «Контент готов».", reply_markup=keyboard([[InlineKeyboardButton(text="✅ Контент готов", callback_data="crm:broadcast_content_done")], [InlineKeyboardButton(text="✖️ Отмена", callback_data="crm:cancel")]]))
        elif action == "broadcast_content_done":
            state = await get_state(user_id) or {}
            if not state.get("text") and not state.get("media"):
                await callback.answer("Добавьте контент", show_alert=True)
                return
            state["mode"] = "broadcast_buttons"
            await set_state(user_id, state)
            await message.answer("Добавить кнопки со ссылками?", reply_markup=keyboard([[InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="crm:broadcast_add_button")], [InlineKeyboardButton(text="➡️ Далее без кнопок", callback_data="crm:broadcast_preview")], [InlineKeyboardButton(text="✖️ Отмена", callback_data="crm:cancel")]]))
        elif action == "broadcast_add_button":
            state = await get_state(user_id) or {}
            state["mode"] = "broadcast_button"
            await set_state(user_id, state)
            await message.answer("Отправьте: <code>Название | https://example.com</code>", reply_markup=back("broadcasts"))
        elif action == "broadcast_preview":
            await broadcast_preview(message, user_id)
        elif action == "broadcast_send":
            await launch_broadcast(message, user_id)
        elif action == "broadcast_save":
            state = await get_state(user_id)
            redis = redis_client()
            try:
                if state:
                    await redis.set(f"{STATE_PREFIX}{user_id}:draft", json.dumps(state, ensure_ascii=False))
            finally:
                await redis.aclose()
            await clear_state(user_id)
            await message.answer("💾 Черновик сохранён.", reply_markup=broadcasts_keyboard())
        elif action == "broadcast_draft":
            redis = redis_client()
            try:
                raw = await redis.get(f"{STATE_PREFIX}{user_id}:draft")
            finally:
                await redis.aclose()
            if raw:
                await set_state(user_id, json.loads(raw))
                await broadcast_preview(message, user_id)
            else:
                await message.answer("Черновиков нет.", reply_markup=broadcasts_keyboard())
        elif action == "broadcast_history":
            redis = redis_client()
            try:
                rows = await redis.lrange(BROADCAST_HISTORY_KEY, 0, 9)
            finally:
                await redis.aclose()
            await message.answer("<b>📊 История</b>\n\n" + ("\n".join(json.loads(row)["summary"] for row in rows) if rows else "Пока пусто."), reply_markup=broadcasts_keyboard())
        elif action == "instruction":
            await clear_state(user_id)
            await message.answer("<b>📖 Редактор инструкции</b>", reply_markup=instruction_keyboard())
        elif action == "instruction_preview":
            await send_instruction(message)
        elif action == "instruction_text":
            await set_state(user_id, {"mode": "instruction_text"})
            await message.answer("Отправьте новый текст инструкции.", reply_markup=back("instruction"))
        elif action == "instruction_media":
            data = await instruction_data()
            await set_state(user_id, {"mode": "instruction_media", "media": data["media"]})
            await message.answer("Отправьте до 10 фото/видео, затем нажмите «Готово».", reply_markup=keyboard([[InlineKeyboardButton(text="✅ Готово", callback_data="crm:instruction_media_done")], [InlineKeyboardButton(text="◀️ Назад", callback_data="crm:instruction")]]))
        elif action == "instruction_media_done":
            state = await get_state(user_id) or {}
            data = await instruction_data()
            data["media"] = (state.get("media") or [])[:10]
            await save_instruction(data)
            await clear_state(user_id)
            await message.answer("✅ Медиа сохранены.", reply_markup=instruction_keyboard())
        elif action == "instruction_clear":
            data = await instruction_data()
            data["media"] = []
            await save_instruction(data)
            await message.answer("Медиа очищены.", reply_markup=instruction_keyboard())
        elif action == "campaigns":
            await clear_state(user_id)
            await message.answer("<b>📣 Рекламные кампании</b>", reply_markup=keyboard([[InlineKeyboardButton(text="➕ Создать", callback_data="crm:campaign_new")], [InlineKeyboardButton(text="📋 Список", callback_data="crm:campaign_list")], [InlineKeyboardButton(text="◀️ Назад", callback_data="crm:home")]]))
        elif action == "campaign_new":
            await set_state(user_id, {"mode": "campaign_name"})
            await message.answer("Введите название кампании.", reply_markup=back("campaigns"))
        elif action == "campaign_list":
            await campaign_list(message)
        elif action == "campaign":
            await campaign_detail(message, parts[2])
        elif action == "admins":
            await admins_menu(message, user_id)
        elif action in {"admin_add", "admin_remove"}:
            if user_id != OWNER_ADMIN_ID:
                await callback.answer("Только владелец", show_alert=True)
                return
            await set_state(user_id, {"mode": action})
            await message.answer("Введите Telegram ID.", reply_markup=back("admins"))
        elif action == "errors":
            async with SessionLocal() as session:
                rows = list((await session.scalars(select(FailedUpdate).where(FailedUpdate.resolved.is_(False)).order_by(FailedUpdate.id.desc()).limit(5))).all())
            await message.answer("<b>⚠️ Ошибки</b>\n\n" + ("\n\n".join(f"#{e.id} {e.update_type}: <code>{e.error[:200]}</code>" for e in rows) if rows else "Ошибок нет."), reply_markup=back())
        elif action == "system":
            queued = await safe_scalar(select(func.count(Job.id)).where(Job.status == "queued"))
            dead = await safe_scalar(select(func.count(Job.id)).where(Job.status == "dead"))
            await message.answer(f"<b>🖥 Система</b>\n\nВерсия: {settings.app_version}\nGit: <code>{settings.git_sha}</code>\nОчередь: {queued}\nОшибок заданий: {dead}", reply_markup=back())
        await callback.answer()
    except Exception:
        logger.exception("crm_callback_failed", callback_data=callback.data, user_id=user_id)
        await callback.answer("Ошибка раздела", show_alert=True)


async def export_users(message: Message) -> None:
    async with SessionLocal() as session:
        users = list((await session.scalars(select(User).order_by(User.id))).all())
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(["telegram_id", "username", "first_name", "registered_at", "last_seen_at", "subscription", "blocked", "referrer_user_id"])
    for user in users:
        writer.writerow([user.telegram_id, user.username or "", user.first_name or "", user.registered_at.isoformat(), user.last_seen_at.isoformat(), user.subscription_status.value, bool(user.blocked_bot_at), user.referrer_user_id or ""])
    await message.answer_document(BufferedInputFile(("\ufeff" + stream.getvalue()).encode(), filename=f"users-{datetime.now(UTC).date()}.csv"), caption=f"Пользователей: <b>{len(users)}</b>", reply_markup=users_keyboard())


async def show_segment(message: Message, segment: str) -> None:
    async with SessionLocal() as session:
        stmt = select(User).order_by(User.id.desc()).limit(20)
        if segment == "vip":
            stmt = stmt.where(User.subscription_status.in_([SubscriptionStatus.vip, SubscriptionStatus.active]))
        else:
            stmt = stmt.where(User.subscription_status.not_in([SubscriptionStatus.vip, SubscriptionStatus.active]))
        users = list((await session.scalars(stmt)).all())
    await message.answer("<b>Сегмент</b>\n\n" + ("\n".join(f"<code>{u.telegram_id}</code> · @{u.username or '—'} · {u.subscription_status.value}" for u in users) if users else "Пусто."), reply_markup=users_keyboard())


async def show_user(message: Message, user_id: int) -> None:
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
        if not user:
            await message.answer("Не найден.", reply_markup=users_keyboard())
            return
        referrer = await session.get(User, user.referrer_user_id) if user.referrer_user_id else None
        paid = Decimal(await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.user_id == user.id, Payment.status == "paid")) or 0)
    source = f"пригласил @{referrer.username or referrer.telegram_id}" if referrer else "прямой вход / поиск Telegram"
    await message.answer(f"<b>👤 {user.first_name or user.username or user.telegram_id}</b>\n\nID: <code>{user.telegram_id}</code>\nUsername: @{user.username or '—'}\nСтатус: <b>{user.subscription_status.value}</b>\nИсточник: {source}\nРегистрация: {user.registered_at:%d.%m.%Y %H:%M}\nПоследняя активность: {user.last_seen_at:%d.%m.%Y %H:%M}\nОплачено: <b>{paid:,.2f} ₽</b>", reply_markup=users_keyboard())


async def search_users(message: Message, query: str) -> None:
    clean = query.strip().lstrip("@")
    conditions = [User.username.ilike(f"%{clean}%"), User.first_name.ilike(f"%{clean}%"), User.last_name.ilike(f"%{clean}%")]
    if clean.isdigit():
        conditions.append(User.telegram_id == int(clean))
    async with SessionLocal() as session:
        users = list((await session.scalars(select(User).where(or_(*conditions)).order_by(User.last_seen_at.desc()).limit(10))).all())
    if not users:
        await message.answer("Ничего не найдено.", reply_markup=users_keyboard())
        return
    await message.answer("Результаты:", reply_markup=keyboard([[InlineKeyboardButton(text=f"{u.first_name or u.username or u.telegram_id} · {u.subscription_status.value}", callback_data=f"crm:user:{u.id}")] for u in users] + [[InlineKeyboardButton(text="◀️ Назад", callback_data="crm:users")]]))


async def broadcast_preview(message: Message, user_id: int) -> None:
    state = await get_state(user_id) or {}
    audience = state.get("audience", "all")
    stmt = select(func.count(User.id)).where(User.blocked_bot_at.is_(None))
    if audience == "vip":
        stmt = stmt.where(User.subscription_status.in_([SubscriptionStatus.vip, SubscriptionStatus.active]))
    elif audience == "free":
        stmt = stmt.where(User.subscription_status.not_in([SubscriptionStatus.vip, SubscriptionStatus.active]))
    count = await safe_scalar(stmt)
    await message.answer(f"<b>Предпросмотр</b>\n\nАудитория: <b>{audience}</b>\nПолучателей: <b>{count}</b>\nМедиа: <b>{len(state.get('media') or [])}</b>\nКнопок: <b>{len(state.get('buttons') or [])}</b>\n\n{state.get('text') or '<i>Без текста</i>'}", reply_markup=keyboard([[InlineKeyboardButton(text="🚀 Запустить", callback_data="crm:broadcast_send")], [InlineKeyboardButton(text="💾 Сохранить", callback_data="crm:broadcast_save")], [InlineKeyboardButton(text="✖️ Отмена", callback_data="crm:cancel")]]))


async def launch_broadcast(message: Message, admin_id: int) -> None:
    state = await get_state(admin_id) or {}
    audience = state.get("audience")
    if audience not in {"all", "vip", "free"}:
        await message.answer("Черновик повреждён.", reply_markup=broadcasts_keyboard())
        return
    async with SessionLocal() as session, session.begin():
        stmt = select(User).where(User.blocked_bot_at.is_(None))
        if audience == "vip":
            stmt = stmt.where(User.subscription_status.in_([SubscriptionStatus.vip, SubscriptionStatus.active]))
        elif audience == "free":
            stmt = stmt.where(User.subscription_status.not_in([SubscriptionStatus.vip, SubscriptionStatus.active]))
        users = list((await session.scalars(stmt)).all())
        stamp = secrets.token_hex(6)
        now = datetime.now(UTC)
        for user in users:
            session.add(Job(kind="broadcast_send", payload={"telegram_id": user.telegram_id, "text": state.get("text") or "", "media": state.get("media") or [], "buttons": state.get("buttons") or []}, status="queued", available_at=now, idempotency_key=f"broadcast:{stamp}:{user.id}"))
    redis = redis_client()
    try:
        await redis.lpush(BROADCAST_HISTORY_KEY, json.dumps({"summary": f"{datetime.now(UTC):%d.%m.%Y %H:%M} · {audience} · {len(users)} получателей"}, ensure_ascii=False))
        await redis.ltrim(BROADCAST_HISTORY_KEY, 0, 99)
    finally:
        await redis.aclose()
    await clear_state(admin_id)
    await message.answer(f"✅ Поставлено в очередь: <b>{len(users)}</b>.", reply_markup=broadcasts_keyboard())


async def campaign_list(message: Message) -> None:
    redis = redis_client()
    try:
        codes = await redis.smembers(CAMPAIGNS_KEY)
        rows = [(code, await redis.hgetall(f"{CAMPAIGN_PREFIX}{code}")) for code in codes]
    finally:
        await redis.aclose()
    rows = [(code, row) for code, row in rows if row]
    if not rows:
        await message.answer("Кампаний нет.", reply_markup=back("campaigns"))
        return
    await message.answer("Кампании:", reply_markup=keyboard([[InlineKeyboardButton(text=f"{row.get('name', code)} · {row.get('registrations', '0')} рег.", callback_data=f"crm:campaign:{code}")] for code, row in rows] + [[InlineKeyboardButton(text="◀️ Назад", callback_data="crm:campaigns")]]))


async def campaign_detail(message: Message, code: str) -> None:
    redis = redis_client()
    try:
        row = await redis.hgetall(f"{CAMPAIGN_PREFIX}{code}")
        registrations = await redis.scard(f"{CAMPAIGN_USERS_PREFIX}{code}")
    finally:
        await redis.aclose()
    cost = Decimal(row.get("cost", "0"))
    cpa = cost / registrations if registrations else Decimal("0")
    username = settings.telegram_bot_username.lstrip("@")
    await message.answer(f"<b>📣 {row.get('name', code)}</b>\n\nПлощадка: {row.get('source', '—')}\nСтоимость: <b>{cost:,.2f} ₽</b>\nРегистраций: <b>{registrations}</b>\nЦена регистрации: <b>{cpa:,.2f} ₽</b>\n\n<code>https://t.me/{username}?start=ad_{code}</code>", reply_markup=back("campaigns"))


async def admins_menu(message: Message, user_id: int) -> None:
    redis = redis_client()
    try:
        dynamic = sorted(int(x) for x in await redis.smembers(ADMINS_KEY) if x.isdigit())
    finally:
        await redis.aclose()
    rows: list[list[InlineKeyboardButton]] = []
    if user_id == OWNER_ADMIN_ID:
        rows += [[InlineKeyboardButton(text="➕ Добавить", callback_data="crm:admin_add")], [InlineKeyboardButton(text="➖ Удалить", callback_data="crm:admin_remove")]]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="crm:home")])
    text = f"<b>👮 Администраторы</b>\n\n👑 <code>{OWNER_ADMIN_ID}</code> — владелец"
    text += "".join(f"\n👮 <code>{item}</code>" for item in dynamic if item != OWNER_ADMIN_ID)
    await message.answer(text, reply_markup=keyboard(rows))


@router.message(AdminStateFilter())
async def state_input(message: Message) -> None:
    user_id = message.from_user.id
    state = await get_state(user_id) or {}
    mode = state.get("mode")
    if mode == "user_search":
        if message.text:
            await clear_state(user_id)
            await search_users(message, message.text)
        return
    if mode == "analytics_dates":
        try:
            left, right = [x.strip() for x in (message.text or "").split("-", 1)]
            start = datetime.strptime(left, "%d.%m.%Y")
            end = datetime.strptime(right, "%d.%m.%Y")
            days = max((end - start).days + 1, 1)
        except ValueError:
            await message.answer("Формат: <code>01.07.2026 - 31.07.2026</code>")
            return
        await clear_state(user_id)
        await send_analytics(message, days)
        return
    if mode == "broadcast_content":
        media = state.setdefault("media", [])
        if message.photo and len(media) < 10:
            media.append({"type": "photo", "file_id": message.photo[-1].file_id})
        elif message.video and len(media) < 10:
            media.append({"type": "video", "file_id": message.video.file_id})
        if message.text:
            state["text"] = message.text
        elif message.caption:
            state["text"] = message.caption
        await set_state(user_id, state)
        await message.answer(f"Добавлено: медиа {len(media)}/10; текст {'есть' if state.get('text') else 'нет'}.", reply_markup=keyboard([[InlineKeyboardButton(text="✅ Контент готов", callback_data="crm:broadcast_content_done")], [InlineKeyboardButton(text="✖️ Отмена", callback_data="crm:cancel")]]))
        return
    if mode == "broadcast_button":
        if not message.text or "|" not in message.text:
            await message.answer("Формат: <code>Название | https://example.com</code>")
            return
        title, url = [x.strip() for x in message.text.split("|", 1)]
        if not url.startswith(("https://", "http://", "tg://")):
            await message.answer("Некорректная ссылка.")
            return
        state.setdefault("buttons", []).append({"text": title[:64], "url": url})
        state["mode"] = "broadcast_buttons"
        await set_state(user_id, state)
        await message.answer("✅ Кнопка добавлена.", reply_markup=keyboard([[InlineKeyboardButton(text="➕ Ещё кнопку", callback_data="crm:broadcast_add_button")], [InlineKeyboardButton(text="👁 Предпросмотр", callback_data="crm:broadcast_preview")]]))
        return
    if mode == "instruction_text":
        if message.text:
            data = await instruction_data()
            data["text"] = message.text
            await save_instruction(data)
            await clear_state(user_id)
            await message.answer("✅ Текст сохранён.", reply_markup=instruction_keyboard())
        return
    if mode == "instruction_media":
        media = state.setdefault("media", [])
        if len(media) >= 10:
            await message.answer("Лимит — 10 файлов.")
            return
        if message.photo:
            media.append({"type": "photo", "file_id": message.photo[-1].file_id})
        elif message.video:
            media.append({"type": "video", "file_id": message.video.file_id})
        else:
            await message.answer("Поддерживаются фото и видео.")
            return
        await set_state(user_id, state)
        await message.answer(f"Добавлено {len(media)}/10.")
        return
    if mode == "campaign_name":
        if message.text:
            await set_state(user_id, {"mode": "campaign_source", "name": message.text[:120]})
            await message.answer("Введите площадку: VK, Telegram Ads, канал и т. п.")
        return
    if mode == "campaign_source":
        if message.text:
            state.update({"mode": "campaign_cost", "source": message.text[:120]})
            await set_state(user_id, state)
            await message.answer("Введите стоимость размещения в ₽.")
        return
    if mode == "campaign_cost":
        try:
            cost = Decimal((message.text or "").replace(" ", "").replace(",", "."))
            if cost < 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            await message.answer("Введите корректную сумму.")
            return
        code = secrets.token_urlsafe(7).replace("-", "").replace("_", "").lower()
        redis = redis_client()
        try:
            await redis.sadd(CAMPAIGNS_KEY, code)
            await redis.hset(f"{CAMPAIGN_PREFIX}{code}", mapping={"name": state["name"], "source": state["source"], "cost": str(cost), "registrations": "0", "created_at": datetime.now(UTC).isoformat()})
        finally:
            await redis.aclose()
        await clear_state(user_id)
        await campaign_detail(message, code)
        return
    if mode in {"admin_add", "admin_remove"}:
        if user_id != OWNER_ADMIN_ID:
            await clear_state(user_id)
            return
        try:
            target = int((message.text or "").strip())
        except ValueError:
            await message.answer("Введите числовой Telegram ID.")
            return
        redis = redis_client()
        try:
            if mode == "admin_add":
                await redis.sadd(ADMINS_KEY, str(target))
                result = "✅ Администратор добавлен."
            elif target == OWNER_ADMIN_ID:
                result = "⛔ Владельца удалить невозможно."
            elif target in settings.telegram_admin_ids:
                result = "Администратор закреплён в .env."
            else:
                result = "✅ Администратор удалён." if await redis.srem(ADMINS_KEY, str(target)) else "Не найден."
        finally:
            await redis.aclose()
        await clear_state(user_id)
        await message.answer(result)
        await admins_menu(message, user_id)
