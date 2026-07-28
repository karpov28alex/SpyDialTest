from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
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
from app.services.access import access_ends_at, refresh_subscription_status
from app.services.users import register_or_update_user

router = Router(name="commands")
settings = get_settings()


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Открыть Dialog Spy", web_app=WebAppInfo(url=settings.mini_app_url))],
        [InlineKeyboardButton(text="📖 Как пользоваться", callback_data="help"), InlineKeyboardButton(text="🔌 Подключение", callback_data="connect")],
        [InlineKeyboardButton(text="💎 Мой статус", callback_data="status"), InlineKeyboardButton(text="⚙️ Настройки", web_app=WebAppInfo(url=f"{settings.mini_app_url}?screen=profile"))],
    ])


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Сводка", callback_data="admin:stats"), InlineKeyboardButton(text="👥 Последние пользователи", callback_data="admin:users")],
        [InlineKeyboardButton(text="💰 Доход и подписки", callback_data="admin:revenue"), InlineKeyboardButton(text="📨 Рассылки", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="⚠️ Ошибки", callback_data="admin:errors"), InlineKeyboardButton(text="🖥 Система", callback_data="admin:system")],
        [InlineKeyboardButton(text="🌐 Открыть Web Admin", web_app=WebAppInfo(url=settings.admin_url))],
    ])


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in settings.telegram_admin_ids)


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
        "<b>Dialog Spy</b> — архив Telegram Business.\n\n"
        "Сохраняет поддерживаемые сообщения, изменения, удаления и защищённые медиа. "
        "Обычные новые сообщения доступны в Mini App.\n\nВыберите действие:",
        reply_markup=main_keyboard(),
    )
    if created:
        await message.answer("Продолжая использование, вы принимаете условия оферты и политики конфиденциальности.")


@router.message(Command("app"))
async def app_command(message: Message) -> None:
    await message.answer("Откройте архив и настройки:", reply_markup=main_keyboard())


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await send_help(message)


async def send_help(target: Message | CallbackQuery) -> None:
    text = (
        "<b>Как пользоваться Dialog Spy</b>\n\n"
        "1. Откройте настройки Telegram → Telegram Business → Чат-боты.\n"
        "2. Подключите Dialog Spy и выдайте доступ к сообщениям.\n"
        "3. Архив появится в Mini App.\n"
        "4. При изменении или удалении сохранённого сообщения бот пришлёт уведомление.\n"
        "5. Для поддерживаемого одноразового медиа не открывайте его: нажмите «Ответить» и отправьте любой текст.\n\n"
        "Подключение может быть доступно и без Premium — ориентируйтесь на наличие раздела Telegram Business."
    )
    if isinstance(target, CallbackQuery):
        await target.message.answer(text, reply_markup=main_keyboard())
        await target.answer()
    else:
        await target.answer(text, reply_markup=main_keyboard())


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery) -> None:
    await send_help(callback)


@router.callback_query(F.data == "connect")
async def connect_callback(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "<b>Подключение</b>\n\nНастройки Telegram → Telegram Business → Чат-боты → Dialog Spy → разрешить сообщения → Сохранить.",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


async def status_text(telegram_id: int) -> str:
    async with SessionLocal() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id).with_for_update())
        if not user:
            return "Сначала выполните /start"
        status = refresh_subscription_status(user)
        connection = await session.scalar(select(BusinessConnection).where(BusinessConnection.owner_user_id == user.id, BusinessConnection.is_active.is_(True)))
        ends_at = access_ends_at(user)
    return f"<b>Статус Dialog Spy</b>\n\nBusiness: {'✅ подключён' if connection else '⚠️ не подключён'}\nДоступ: {status.value}\nАктивен до: {ends_at.astimezone(UTC).strftime('%d.%m.%Y · %H:%M UTC')}"


@router.message(Command("status"))
async def status_command(message: Message) -> None:
    if message.from_user:
        await message.answer(await status_text(message.from_user.id), reply_markup=main_keyboard())


@router.callback_query(F.data == "status")
async def status_callback(callback: CallbackQuery) -> None:
    await callback.message.answer(await status_text(callback.from_user.id), reply_markup=main_keyboard())
    await callback.answer()


@router.message(Command("privacy"))
async def privacy_command(message: Message) -> None:
    await message.answer("Политика конфиденциальности и оферта публикуются на рабочем домене. Для экспорта или удаления данных обратитесь в поддержку.")


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
        "<b>📊 Dialog Spy — сводка</b>\n\n"
        f"Пользователей: <b>{total}</b> (+{today} за 24 часа)\n"
        f"Business-подключений: <b>{active}</b>\n"
        f"Сообщений: <b>{messages}</b>\n"
        f"Изменённых: <b>{edited}</b>\n"
        f"Удалённых: <b>{deleted}</b>\n"
        f"Доход за месяц: <b>{revenue:,.2f} ₽</b>\n"
        f"Очередь: <b>{queued}</b> · Ошибки: <b>{errors}</b>"
    )


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Команда недоступна.")
        return
    await message.answer(await admin_stats(), reply_markup=admin_keyboard())


@router.message(Command("broadcast"))
async def broadcast_command(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Команда недоступна.")
        return
    raw=(command.args or "").strip()
    if not raw or " " not in raw:
        await message.answer(
            "<b>Рассылка</b>\n\nФормат:\n<code>/broadcast all Текст</code>\n<code>/broadcast vip Текст</code>\n<code>/broadcast free Текст</code>\n\nРассылка ставится в надёжную очередь worker-а."
        )
        return
    audience,text=raw.split(" ",1)
    audience=audience.lower()
    if audience not in {"all","vip","free"} or not text.strip():
        await message.answer("Аудитория: all, vip или free. После неё укажите текст.")
        return
    async with SessionLocal() as session, session.begin():
        stmt=select(User).where(User.blocked_bot_at.is_(None))
        if audience=="vip":
            stmt=stmt.where(User.subscription_status.in_([SubscriptionStatus.vip,SubscriptionStatus.active]))
        elif audience=="free":
            stmt=stmt.where(User.subscription_status.in_([SubscriptionStatus.trial,SubscriptionStatus.referral,SubscriptionStatus.expired]))
        users=list((await session.scalars(stmt)).all())
        stamp=int(datetime.now(UTC).timestamp())
        for user in users:
            session.add(Job(kind="send_text",payload={"telegram_id":user.telegram_id,"text":text.strip()},status="queued",available_at=datetime.now(UTC),idempotency_key=f"broadcast:{stamp}:{audience}:{user.id}"))
    await message.answer(f"✅ Рассылка поставлена в очередь. Аудитория: <b>{audience}</b>. Получателей: <b>{len(users)}</b>.",reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    async with SessionLocal() as session:
        if action == "stats":
            text = await admin_stats()
        elif action == "users":
            rows = list((await session.scalars(select(User).order_by(User.id.desc()).limit(10))).all())
            text = "<b>👥 Последние пользователи</b>\n\n" + ("\n".join(f"{u.telegram_id} · @{u.username or '—'} · {u.subscription_status.value}" for u in rows) if rows else "Пользователей нет")
        elif action == "revenue":
            month_start=datetime.now(UTC).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
            month=await session.scalar(select(func.coalesce(func.sum(Payment.amount),0)).where(Payment.status=="paid",Payment.paid_at>=month_start)) or Decimal("0")
            total=await session.scalar(select(func.coalesce(func.sum(Payment.amount),0)).where(Payment.status=="paid")) or Decimal("0")
            vip=await session.scalar(select(func.count(User.id)).where(User.subscription_status.in_([SubscriptionStatus.vip,SubscriptionStatus.active]))) or 0
            text=f"<b>💰 Доход и подписки</b>\n\nЗа месяц: <b>{month:,.2f} ₽</b>\nЗа всё время: <b>{total:,.2f} ₽</b>\nVIP/активных: <b>{vip}</b>"
        elif action == "errors":
            rows = list((await session.scalars(select(FailedUpdate).where(FailedUpdate.resolved.is_(False)).order_by(FailedUpdate.id.desc()).limit(5))).all())
            text = "<b>⚠️ Последние ошибки</b>\n\n" + ("\n\n".join(f"#{e.id} {e.update_type}: {e.error[:180]}" for e in rows) if rows else "Ошибок нет.")
        elif action == "system":
            queued=await session.scalar(select(func.count(Job.id)).where(Job.status=="queued")) or 0
            dead=await session.scalar(select(func.count(Job.id)).where(Job.status=="dead")) or 0
            text=f"<b>🖥 Система</b>\n\nВерсия: {settings.app_version}\nGit: {settings.git_sha}\nAPI: работает\nОчередь: {queued}\nНеуспешных заданий: {dead}"
        else:
            text="<b>📨 Рассылки</b>\n\n<code>/broadcast all Текст</code> — всем\n<code>/broadcast vip Текст</code> — VIP и активным\n<code>/broadcast free Текст</code> — trial и бесплатным\n\nОтправка идёт через worker и не блокирует бота."
    await callback.message.answer(text, reply_markup=admin_keyboard())
    await callback.answer()


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("Неизвестная команда. Доступны: /start, /app, /help, /status, /privacy", reply_markup=main_keyboard())
