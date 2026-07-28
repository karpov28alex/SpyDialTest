"""Compatibility entrypoint for the button-driven Telegram admin console."""

from app.bot.admin_console import OWNER_ADMIN_ID, is_admin, router

__all__ = ["OWNER_ADMIN_ID", "is_admin", "router"]
