from typing import Annotated

from fastapi import APIRouter, Header, Request

from app.api.routes.webhook import telegram_webhook

router = APIRouter(tags=["telegram"])


@router.post("/api/telegram/webhook/{secret}", status_code=200, include_in_schema=False)
async def legacy_telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    """Backward-compatible endpoint for previously configured Telegram webhooks.

    It delegates to the same authenticated, deduplicated processing pipeline as
    the canonical /telegram/webhook/{secret} endpoint, so old Telegram webhook
    configuration cannot silently drop Business updates during deployments.
    """
    return await telegram_webhook(secret, request, x_telegram_bot_api_secret_token)
