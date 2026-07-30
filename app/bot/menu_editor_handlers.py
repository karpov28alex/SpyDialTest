from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis

from app.bot.handlers import INSTRUCTION_KEY, is_admin
from app.core.config import get_settings

router = Router(name="menu-editor")
settings = get_settings()
CONTENT_KEY = "dialog_spy:user_menu_content"
DEFAULTS = {
    "settings": "Зелёная отметка означает, что функция включена. Нажмите кнопку для переключения.",
    "offer_url": "https://mooncloud.ltd/spy/terms.html#free",
    "show_offer": "1",
}


class MenuEdit(StatesGroup):
    settings = State()
    offer = State()
    instruction = State()


async def get_menu_content() -> dict[str, str]:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        data = await redis.hgetall(CONTENT_KEY)
    finally:
        await redis.aclose()
    return {**DEFAULTS, **{key: value for key, value in data.items() if value}}


async def set_menu_content(field: str, value: str) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        if field == "instruction":
            await redis.hset(INSTRUCTION_KEY, "text", value)
        else:
            await redis.hset(CONTENT_KEY, field, value)
    finally:
        await redis.aclose()


def editor_keyboard(show_offer: bool = True) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Текст настроек", callback_data="menuedit:settings")],
        [InlineKeyboardButton(text="📄 Ссылка оферты", callback_data="menuedit:offer")],
        [InlineKeyboardButton(
            text=f"{'✅' if show_offer else '❌'} Кнопка «Оферта»",
            callback_data="menuedit:toggle_offer",
        )],
        [InlineKeyboardButton(text="📖 Текст инструкции", callback_data="menuedit:instruction")],
        [InlineKeyboardButton(text="👁 Предпросмотр", callback_data="menuedit:preview")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="menuedit:cancel")],
    ])


async def _editor_markup() -> InlineKeyboardMarkup:
    data = await get_menu_content()
    return editor_keyboard(data.get("show_offer", "1") == "1")


@router.message(Command("settings"))
async def configurable_settings_command(message: Message) -> None:
    from app.bot import user_handlers
    if not message.from_user:
        return
    prefs = await user_handlers._settings(message.from_user.id)
    if prefs is None:
        await message.answer("Профиль ещё не создан. Отправьте /start.")
        return
    content = await get_menu_content()
    await message.answer(
        f"<b>⚙️ Настройки</b>\n\n{content['settings']}",
        reply_markup=user_handlers.settings_keyboard(prefs),
    )


@router.callback_query(F.data == "user:settings")
async def configurable_settings_callback(callback: CallbackQuery) -> None:
    from app.bot import user_handlers
    prefs = await user_handlers._settings(callback.from_user.id)
    if callback.message and prefs:
        content = await get_menu_content()
        await callback.message.answer(
            f"<b>⚙️ Настройки</b>\n\n{content['settings']}",
            reply_markup=user_handlers.settings_keyboard(prefs),
        )
    await callback.answer()


@router.message(Command("menu_editor"))
async def menu_editor(message: Message) -> None:
    if not await is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Команда недоступна.")
        return
    await message.answer(
        "<b>Редактор пользовательского меню</b>\n\nНастройте текст, ссылку и видимость оферты.",
        reply_markup=await _editor_markup(),
    )


@router.callback_query(F.data.startswith("menuedit:"))
async def menu_editor_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    if action == "cancel":
        await state.clear()
        await callback.answer("Закрыто")
        return
    if action == "toggle_offer":
        data = await get_menu_content()
        enabled = data.get("show_offer", "1") == "1"
        await set_menu_content("show_offer", "0" if enabled else "1")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=await _editor_markup())
        await callback.answer("Кнопка оферты выключена" if enabled else "Кнопка оферты включена")
        return
    if action == "preview":
        data = await get_menu_content()
        status = "показывается" if data.get("show_offer", "1") == "1" else "скрыта"
        text = (
            "<b>Текущие настройки меню</b>\n\n"
            f"<b>Настройки:</b> {data['settings']}\n\n"
            f"<b>Оферта:</b> {data['offer_url']}\n"
            f"<b>Кнопка оферты:</b> {status}"
        )
        if callback.message:
            await callback.message.answer(text, reply_markup=await _editor_markup())
        await callback.answer()
        return
    state_map = {
        "settings": MenuEdit.settings,
        "offer": MenuEdit.offer,
        "instruction": MenuEdit.instruction,
    }
    target = state_map.get(action)
    if target is None:
        await callback.answer("Неизвестный раздел", show_alert=True)
        return
    await state.set_state(target)
    prompt = "Отправьте новый текст одним сообщением. HTML-разметка поддерживается."
    if action == "offer":
        prompt = "Отправьте новую полную HTTPS-ссылку оферты."
    if callback.message:
        await callback.message.answer(prompt)
    await callback.answer()


@router.message(MenuEdit.settings)
async def save_settings_text(message: Message, state: FSMContext) -> None:
    await _save_text(message, state, "settings")


@router.message(MenuEdit.instruction)
async def save_instruction_text(message: Message, state: FSMContext) -> None:
    await _save_text(message, state, "instruction")


@router.message(MenuEdit.offer)
async def save_offer_url(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value.startswith("https://"):
        await message.answer("Ссылка должна начинаться с https://")
        return
    await set_menu_content("offer_url", value)
    await state.clear()
    await message.answer("✅ Ссылка оферты сохранена.", reply_markup=await _editor_markup())


async def _save_text(message: Message, state: FSMContext, field: str) -> None:
    value = (message.text or "").strip()
    if not value:
        await message.answer("Текст не может быть пустым.")
        return
    await set_menu_content(field, value)
    await state.clear()
    await message.answer("✅ Изменения сохранены.", reply_markup=await _editor_markup())
