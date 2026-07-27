import asyncio

from aiogram import Bot

from app.core.config import get_settings


async def main() -> None:
    settings = get_settings()
    bot = Bot(settings.telegram_bot_token)
    url = f"{settings.public_base_url.rstrip('/')}/telegram/webhook/{settings.telegram_webhook_secret}"
    await bot.set_webhook(
        url=url,
        secret_token=settings.telegram_webhook_secret,
        allowed_updates=[
            "message",
            "callback_query",
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
        ],
        drop_pending_updates=False,
    )
    print(f"Webhook configured: {url}")
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
