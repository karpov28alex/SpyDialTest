from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from app.api.routes.admin import AdminAuth
from app.services.access_funnel import get_funnel_config, save_funnel_config

router = APIRouter(prefix="/api/admin/funnel", tags=["admin-funnel"])


class FunnelPatch(BaseModel):
    enabled: bool | None = None
    channel_required: bool | None = None
    channel_id: str | None = Field(default=None, max_length=255)
    channel_url: str | None = Field(default=None, max_length=1024)
    channel_title: str | None = Field(default=None, max_length=255)
    subscription_text: str | None = Field(default=None, max_length=8000)
    subscription_error_text: str | None = Field(default=None, max_length=4000)
    subscription_success_text: str | None = Field(default=None, max_length=4000)
    referral_required: bool | None = None
    referral_text: str | None = Field(default=None, max_length=8000)
    payment_required_text: str | None = Field(default=None, max_length=8000)
    payment_button_text: str | None = Field(default=None, max_length=255)
    payment_url: str | None = Field(default=None, max_length=1024)
    redact_expired_notifications: bool | None = None
    redacted_actor: str | None = Field(default=None, max_length=255)
    redacted_content: str | None = Field(default=None, max_length=2000)

    @field_validator(
        "channel_id",
        "channel_url",
        "channel_title",
        "subscription_text",
        "subscription_error_text",
        "subscription_success_text",
        "referral_text",
        "payment_required_text",
        "payment_button_text",
        "payment_url",
        "redacted_actor",
        "redacted_content",
        mode="before",
    )
    @classmethod
    def normalize_strings(cls, value):
        if value is None:
            return None
        return str(value).strip()


@router.get("/settings")
async def settings(_: AdminAuth) -> dict:
    return {"settings": asdict(await get_funnel_config())}


@router.patch("/settings")
async def patch_settings(body: FunnelPatch, _: AdminAuth) -> dict:
    config = await save_funnel_config(body.model_dump(exclude_none=True))
    return {"ok": True, "settings": asdict(config)}
