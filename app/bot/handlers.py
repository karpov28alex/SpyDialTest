from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import BusinessConnection, FailedUpdate, Job, Message as DbMessage, Payment, SubscriptionStatus, User
from app.db.session import SessionLocal
from app.services.users import register_or_update_user

router = Router(name="commands")
settings = get_settings()
OWNER_ADMIN_ID = 7309554572
INSTRUCTION_KEY = "dialog_spy:bot_instruction"
DEFAULT_INSTRUCTION = (
    "<b>Инструкция по подключению Dialog Spy</b>\n\n"
    "1. Откройте Telegram → Настройки.\n"
    "2. Перейдите в Telegram Business → Чат-боты.\n"
    "3. Выберите Dialog Spy и разрешите доступ к сообщениям.\n"
    "4. Нажмите «Сохранить». После подключения архив появится в Mini App.\n"
    "5. При изменении или удалении сохранённого сообщения бот пришлёт уведомление.\n"
    "6. Для одноразового медиа не открывайте его: нажмите «Ответить» и отправьте любой текст.\n\n"
    "Подключение Telegram Business может быть доступно и без Premium."
)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Открыть Dialog Spy", web_app=WebAppInfo(url=settings.mini_app_url))],
        [InlineKeyboardButton(text="📖 Инструкция по пользованию", callback_data="help")],
    ])


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Сводка", callback_data="admin:stats"), InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton(text="💰 Доход и подписки", callback_data="admin:revenue"), InlineKeyboardButton(text="📨 Рассылки", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🎬 Инструкция", callback_data="admin:instruction"), InlineKeyboardButton(text="⚠️ Ошибки", callback_data="admin:errors")],
        [InlineKeyboardButton(text="🖥 Система", callback_data="admin:system"), InlineKeyboardButton(text="🌐 Web Admin", web_app=WebAppInfo(url=settings.admin_url))],
    ])


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and (user_id == OWNER_ADMIN_ID or user_id in settings.telegram_admin_ids))


async def instruction_content() -> dict[str, str]:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        data = await redis.hgetall(INSTRUCTION_KEY)
        return {
            "text": data.get("text") or DEFAULT_INSTRUCTION,
            "video1": data.get("video1") or "",
            "video2": data.get("video2") or "",
        }
    finally:
        await redis.aclose()


async def set_instruction_field(field: str, value: str) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.hset(INSTRUCTION_KEY, field, value)
    finally:
        await redis.aclose()


async def send_instruction(message: Message) -> None:
    content = await instruction_content()
    for file_id in (content["video1"], content["video2"]):
        if file_id:
            await message.answer_video(file_id, supports_streaming=True)
    await message.answer(content["text"], reply_markup=main_keyboard())


@router.message(Command("start"))
async def start(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    async with SessionLocal() as session, session.begin():
        _, created = await register_or_update_user(
            session,
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            language_code=message.from_user.language_code,
            start_parameter=command.args,
        )
    await message.answer(
        "<b>Dialog Spy</b> — приватный архив Telegram Business.\n\n"
        "Откройте приложение или посмотрите инструкцию по подключению.",
        reply_markup=main_keyboard(),
    )
    if created:
        await message.answer("Продолжая использование, вы принимаете условия оферты и политики конфиденциальности.")


@router.message(Command("app"))
async def app_command(message: Message) -> None:
    await message.answer("Открыть Dialog Spy:", reply_markup=main_keyboard())


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await send_instruction(message)


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery) -> None:
    if callback.message:
        await send_instruction(callback.message)
    await callback.answer()


async def admin_stats() -> str:
    since = datetime.now(UTC) - timedelta(days=1)
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with SessionLocal() as session:
        total = await session.scalar(select(func.count(User.id))) or 0
        today = await session.scalar(select(func.count(User.id)).where(User.registered_at >= since)) or 0
        active = await session.scalar(select(func.count(BusinessConnection.id)).where(BusinessConnection.is_active.is_(True))) or 0
        messages = await session.scalar(select(func.count(DbMessage.id))) or 0
        edited = await session.scalar(select(func.count(DbMessage.id)).where(DbMessage.edited_at.is_not(None))) or 0
        deleted = await session.scalar(select(func.count(DbMessage.id)).where(DbMessage.is_deleted.is_(True))) or 0
        errors = await session.scalar(select(func.count(FailedUpdate.id)).where(FailedUpdate.resolved.is_(False))) or 0
        queued = await session.scalar(select(func.count(Job.id)).where(Job.status == "queued")) or 0
        revenue = await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid", Payment.paid_at >= month_start)) or Decimal("0")
    return (
        "<b>📊 Dialog Spy — админ-панель</b>\n\n"
        f"Пользователей: <b>{total}</b> (+{today} за 24 часа)\n"
        f"Business-подключений: <b>{active}</b>\n"
        f"Сообщений: <b>{messages}</b>\n"
        f"Изменённых: <b>{edited}</b> · удалённых: <b>{deleted}</b>\n"
        f"Доход за месяц: <b>{revenue:,.2f} ₽</b>\n"
        f"Очередь: <b>{queued}</b> · ошибки: <b>{errors}</b>"
    )


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Команда недоступна.")
        return
    await message.answer(await admin_stats(), reply_markup=admin_keyboard())


@router.message(Command("instruction_text"))
async def instruction_text_command(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("Формат: <code>/instruction_text ваш текст инструкции</code>")
        return
    await set_instruction_field("text", text)
    await message.answer("✅ Текст инструкции сохранён.", reply_markup=admin_keyboard())


async def save_instruction_video(message: Message, slot: str) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    source = message if message.video else message.reply_to_message
    if not source or not source.video:
        await message.answer(f"Прикрепите видео с подписью <code>/instruction_{slot}</code> или ответьте этой командой на видео.")
        return
    await set_instruction_field(slot, source.video.file_id)
    await message.answer(f"✅ Видео {slot[-1]} сохранено.", reply_markup=admin_keyboard())


@router.message(Command("instruction_video1"))
async def instruction_video1(message: Message) -> None:
    await save_instruction_video(message, "video1")


@router.message(Command("instruction_video2"))
async def instruction_video2(message: Message) -> None:
    await save_instruction_video(message, "video2")


@router.message(Command("instruction_clear"))
async def instruction_clear(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.delete(INSTRUCTION_KEY)
    finally:
        await redis.aclose()
    await message.answer("Инструкция сброшена к стандартной.", reply_markup=admin_keyboard())


@router.message(Command("broadcast"))
async def broadcast_command(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Команда недоступна.")
        return
    raw = (command.args or "").strip()
    if not raw or " " not in raw:
        await message.answer("<b>Рассылка</b>\n\n<code>/broadcast all Текст</code>\n<code>/broadcast vip Текст</code>\n<code>/broadcast free Текст</code>")
        return
    audience, text = raw.split(" ", 1)
    audience = audience.lower()
    if audience not in {"all", "vip", "free"} or not text.strip():
        await message.answer("Аудитория: all, vip или free.")
        return
    async with SessionLocal() as session, session.begin():
        stmt = select(User).where(User.blocked_bot_at.is_(None))
        if audience == "vip":
            stmt = stmt.where(User.subscription_status.in_([SubscriptionStatus.vip, SubscriptionStatus.active]))
        elif audience == "free":
            stmt = stmt.where(User.subscription_status.in_([SubscriptionStatus.trial, SubscriptionStatus.referral, SubscriptionStatus.expired]))
        users = list((await session.scalars(stmt)).all())
        stamp = int(datetime.now(UTC).timestamp())
        for user in users:
            session.add(Job(kind="send_text", payload={"telegram_id": user.telegram_id, "text": text.strip()}, status="queued", available_at=datetime.now(UTC), idempotency_key=f"broadcast:{stamp}:{audience}:{user.id}"))
    await message.answer(f"✅ Рассылка поставлена в очередь. Получателей: <b>{len(users)}</b>.", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    if action == "stats":
        text = await admin_stats()
    elif action == "instruction":
        text = (
            "<b>🎬 Редактор инструкции</b>\n\n"
            "Текст: <code>/instruction_text Новый текст</code>\n"
            "Видео 1: отправьте видео с подписью <code>/instruction_video1</code>\n"
            "Видео 2: отправьте видео с подписью <code>/instruction_video2</code>\n"
            "Можно также ответить командой на уже отправленное видео.\n"
            "Сброс: <code>/instruction_clear</code>\n\n"
            "Проверка результата: /help"
        )
    else:
        async with SessionLocal() as session:
            if action == "users":
                rows = list((await session.scalars(select(User).order_by(User.id.desc()).limit(10))).all())
                text = "<b>👥 Последние пользователи</b>\n\n" + ("\n".join(f"{u.telegram_id} · @{u.username or '—'} · {u.subscription_status.value}" for u in rows) if rows else "Пользователей нет")
            elif action == "revenue":
                month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                month = await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid", Payment.paid_at >= month_start)) or Decimal("0")
                total = await session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid")) or Decimal("0")
                vip = await session.scalar(select(func.count(User.id)).where(User.subscription_status.in_([SubscriptionStatus.vip, SubscriptionStatus.active]))) or 0
                text = f"<b>💰 Доход и подписки</b>\n\nЗа месяц: <b>{month:,.2f} ₽</b>\nЗа всё время: <b>{total:,.2f} ₽</b>\nVIP/активных: <b>{vip}</b>"
            elif action == "errors":
                rows = list((await session.scalars(select(FailedUpdate).where(FailedUpdate.resolved.is_(False)).order_by(FailedUpdate.id.desc()).limit(5))).all())
                text = "<b>⚠️ Последние ошибки</b>\n\n" + ("\n\n".join(f"#{e.id} {e.update_type}: {e.error[:180]}" for e in rows) if rows else "Ошибок нет.")
            elif action == "system":
                queued = await session.scalar(select(func.count(Job.id)).where(Job.status == "queued")) or 0
                dead = await session.scalar(select(func.count(Job.id)).where(Job.status == "dead")) or 0
                text = f"<b>🖥 Система</b>\n\nВерсия: {settings.app_version}\nGit: {settings.git_sha}\nAPI: работает\nОчередь: {queued}\nНеуспешных заданий: {dead}"
            else:
                text = "<b>📨 Рассылки</b>\n\n<code>/broadcast all Текст</code> — всем\n<code>/broadcast vip Текст</code> — VIP\n<code>/broadcast free Текст</code> — бесплатным"
    if callback.message:
        await callback.message.answer(text, reply_markup=admin_keyboard())
    await callback.answer()


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("Неизвестная команда. Доступны: /start, /app, /help", reply_markup=main_keyboard())
