"""Compatibility entrypoint for the button-driven Telegram admin console."""

from aiogram import Router

from app.bot.access_funnel import router as funnel_router
from app.bot.admin_console import OWNER_ADMIN_ID, is_admin, router as console_router
from app.bot.admin_polish import router as polish_router

router = Router(name="admin_entrypoint")
router.include_router(funnel_router)
router.include_router(polish_router)
router.include_router(console_router)

__all__ = ["OWNER_ADMIN_ID", "is_admin", "router"]
