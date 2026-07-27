from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.models import BusinessConnection, FailedUpdate, Job, Message as DbMessage, User
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
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"), InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton(text="⭐ Выдать VIP", callback_data="admin:vip"), InlineKeyboardButton(text="📨 Рассылки", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="📝 Help-контент", callback_data="admin:help"), InlineKeyboardButton(text="⚠️ Ошибки", callback_data="admin:errors")],
        [InlineKeyboardButton(text="🖥 Система", callback_data="admin:system"), InlineKeyboardButton(text="🌐 Web Admin", web_app=WebAppInfo(url=settings.admin_url))],
    ])


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in settings.telegram_admin_ids)


@router.message(Command("start"))
async def start(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    async with SessionLocal() as session, session.begin():
        _, created = await register_or_update_user(session, telegram_id=message.from_user.id, username=message.from_user.username, first_name=message.from_user.first_name, last_name=message.from_user.last_name, language_code=message.from_user.language_code, start_parameter=command.args)
    await message.answer(
        "<b>Dialog Spy</b> — архив Telegram Business.\n\n"
        "Сохраняет поддерживаемые сообщения, изменения, удаления и защищённые медиа. "
        "Обычные новые сообщения не дублируются в чат с ботом — они доступны в Mini App.\n\n"
        "Выберите действие:", reply_markup=main_keyboard())
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
        "3. Продолжайте общаться в обычном Telegram — архив появится в Mini App.\n"
        "4. При изменении или удалении сохранённого сообщения бот пришлёт уведомление.\n"
        "5. Для поддерживаемого одноразового/защищённого медиа не открывайте его: нажмите «Ответить» и отправьте любой текст.\n\n"
        "Telegram расширяет доступность Business-функций, поэтому подключение может быть доступно и без Premium — ориентируйтесь на наличие раздела Telegram Business в вашем приложении."
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
    await callback.message.answer("<b>Подключение</b>\n\nНастройки Telegram → Telegram Business → Чат-боты → Dialog Spy → разрешить сообщения → Сохранить.", reply_markup=main_keyboard())
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


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Команда недоступна.")
        return
    await message.answer("<b>Dialog Spy Admin</b>\n\nВыберите раздел:", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    async with SessionLocal() as session:
        if action == "stats":
            total = await session.scalar(select(func.count(User.id))) or 0
            today = await session.scalar(select(func.count(User.id)).where(User.registered_at >= datetime.now(UTC) - timedelta(days=1))) or 0
            connections = await session.scalar(select(func.count(BusinessConnection.id)).where(BusinessConnection.is_active.is_(True))) or 0
            messages = await session.scalar(select(func.count(DbMessage.id))) or 0
            errors = await session.scalar(select(func.count(FailedUpdate.id)).where(FailedUpdate.resolved.is_(False))) or 0
            queued = await session.scalar(select(func.count(Job.id)).where(Job.status == "queued")) or 0
            text = f"<b>📊 Статистика</b>\n\nПользователей: {total}\nНовых за 24 часа: {today}\nBusiness-подключений: {connections}\nСообщений в архиве: {messages}\nОшибок: {errors}\nОчередь: {queued}"
        elif action == "users":
            rows = list((await session.scalars(select(User).order_by(User.id.desc()).limit(10))).all())
            text = "<b>👥 Последние пользователи</b>\n\n" + "\n".join(f"{u.telegram_id} · @{u.username or '—'} · {u.subscription_status.value}" for u in rows)
        elif action == "errors":
            rows = list((await session.scalars(select(FailedUpdate).where(FailedUpdate.resolved.is_(False)).order_by(FailedUpdate.id.desc()).limit(5))).all())
            text = "<b>⚠️ Последние ошибки</b>\n\n" + ("\n\n".join(f"#{e.id} {e.update_type}: {e.error[:180]}" for e in rows) if rows else "Ошибок нет.")
        elif action == "system":
            text = f"<b>🖥 Система</b>\n\nВерсия: {settings.app_version}\nGit: {settings.git_sha}\nAPI: запущен\nWorker: проверяйте /health/ready"
        elif action == "vip":
            text = "<b>⭐ VIP</b>\n\nСледующий шаг: мастер выдачи VIP по Telegram ID. Пока используйте веб-админку после настройки авторизации."
        elif action == "broadcast":
            text = "<b>📨 Рассылки</b>\n\nМастер рассылок будет включать аудиторию, текст, медиа, preview и подтверждение. Отправка выполняется только worker-ом."
        else:
            text = "<b>📝 Help-контент</b>\n\nПоддержка двух видео, текста, preview и версий подготовлена к следующей миграции."
    await callback.message.answer(text, reply_markup=admin_keyboard())
    await callback.answer()


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("Неизвестная команда. Доступны: /start, /app, /help, /status, /privacy", reply_markup=main_keyboard())
