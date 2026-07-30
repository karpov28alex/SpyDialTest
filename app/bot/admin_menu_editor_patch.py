from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import handlers
from app.bot.handlers import is_admin
from app.bot.menu_editor_handlers import editor_keyboard

router = Router(name="admin-menu-editor-entry")
_original_admin_keyboard = handlers.admin_keyboard


def admin_keyboard_with_editor() -> InlineKeyboardMarkup:
    original = _original_admin_keyboard()
    rows = [list(row) for row in original.inline_keyboard]
    editor_row = [InlineKeyboardButton(text="🧩 Меню пользователей", callback_data="admin:menu_editor")]
    insert_at = max(len(rows) - 2, 0)
    rows.insert(insert_at, editor_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


handlers.admin_keyboard = admin_keyboard_with_editor


@router.callback_query(F.data == "admin:menu_editor")
async def open_menu_editor(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if callback.message:
        await callback.message.answer(
            "<b>Редактор пользовательского меню</b>\n\n"
            "Выберите профиль, настройки, оферту или инструкцию.",
            reply_markup=editor_keyboard(),
        )
    await callback.answer()
