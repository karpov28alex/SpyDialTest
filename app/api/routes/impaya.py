from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, SessionDep
from app.core.config import Settings, get_settings
from app.core.security import create_token, decode_token
from app.db.models import Payment, Subscription, SubscriptionStatus, User
from app.db.session import get_session
from app.services.impaya import ImpayaClient, ImpayaError, successful_state

router = APIRouter(prefix="/api/payments/impaya", tags=["payments"])


def _start_token(user: User, settings: Settings) -> str:
    return create_token(
        str(user.id),
        "impaya_payment_start",
        timedelta(hours=24),
        settings,
    )


def payment_start_url(user: User, settings: Settings) -> str:
    token = _start_token(user, settings)
    return f"{settings.public_base_url.rstrip('/')}/api/payments/impaya/start/{token}"


def _return_url(settings: Settings, kind: str, operation_id: str) -> str:
    configured = (
        settings.impaya_return_success_url
        if kind == "success"
        else settings.impaya_return_fail_url
    )
    base = configured or (
        f"{settings.public_base_url.rstrip('/')}/api/payments/impaya/return/{kind}"
    )
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}operation_id={quote(operation_id)}"


def _page(title: str, message: str, *, ok: bool, bot_username: str) -> HTMLResponse:
    accent = "#7f45ff" if ok else "#ff5d82"
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    safe_bot = html.escape(bot_username.lstrip("@"))
    body = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;box-sizing:border-box;background:#08060d;color:#fff;font:16px/1.5 system-ui}}
.card{{width:min(480px,100%);padding:28px;border:1px solid #3d2b54;border-radius:24px;background:linear-gradient(145deg,#1b112b,#0d0914);box-shadow:0 24px 80px #0008}}
.icon{{width:58px;height:58px;border-radius:18px;display:grid;place-items:center;background:{accent};font-size:28px}}
h1{{margin:18px 0 8px}}p{{color:#b9acc8}}a{{display:block;margin-top:24px;padding:14px;text-align:center;border-radius:14px;color:#fff;text-decoration:none;font-weight:800;background:linear-gradient(135deg,#a33cff,#6527d8)}}
</style></head><body><main class="card"><div class="icon">{'✓' if ok else '!'}</div><h1>{safe_title}</h1><p>{safe_message}</p><a href="https://t.me/{safe_bot}">Вернуться в Phantom</a></main></body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)


@router.get("/config")
async def impaya_config(
    user: CurrentUser,
    settings: Settings = Depends(get_settings),
) -> dict:
    return {
        "enabled": settings.impaya_enabled,
        "test_mode": settings.impaya_test_mode,
        "amount_rub": settings.impaya_initial_amount_rub,
        "access_days": settings.impaya_initial_access_days,
        "payment_url": payment_start_url(user, settings) if settings.impaya_enabled else None,
    }


@router.post("/invoice")
async def create_invoice_for_current_user(
    user: CurrentUser,
    session: SessionDep,
    settings: Settings = Depends(get_settings),
) -> dict:
    return await _create_invoice(user, session, settings)


async def _create_invoice(user: User, session: AsyncSession, settings: Settings) -> dict:
    if not settings.impaya_enabled:
        raise HTTPException(status_code=503, detail="Impaya payments are disabled")
    if not settings.impaya_token or not settings.impaya_terminal_name:
        raise HTTPException(status_code=503, detail="Impaya is not configured")

    operation_id = f"ph_{user.telegram_id}_{uuid.uuid4().hex[:20]}"
    amount_rub = settings.impaya_initial_amount_rub
    payment = Payment(
        user_id=user.id,
        provider="impaya",
        external_id=operation_id,
        amount=Decimal(amount_rub),
        currency="RUB",
        status="pending",
        recurring=False,
        payload={"kind": "initial", "test_mode": settings.impaya_test_mode},
    )
    session.add(payment)
    await session.flush()

    client = ImpayaClient(settings)
    try:
        invoice = await client.create_initial_invoice(
            customer_operation_id=operation_id,
            telegram_id=user.telegram_id,
            success_url=_return_url(settings, "success", operation_id),
            fail_url=_return_url(settings, "fail", operation_id),
            amount_rub=amount_rub,
        )
    except ImpayaError as exc:
        payment.status = "failed"
        payment.payload = {
            **payment.payload,
            "error_code": exc.code,
            "error_message": str(exc),
            "response": exc.payload,
        }
        await session.commit()
        raise HTTPException(status_code=502, detail=f"Impaya: {exc}") from exc

    payment.payload = {
        **payment.payload,
        "invoice_id": invoice.invoice_id,
        "transaction_id": invoice.transaction_id,
        "response": invoice.raw,
    }
    await session.commit()
    return {
        "operation_id": operation_id,
        "invoice_id": invoice.invoice_id,
        "payment_url": client.payment_url(invoice.invoice_id),
        "amount_rub": amount_rub,
        "test_mode": settings.impaya_test_mode,
    }


@router.get("/start/{token}", include_in_schema=False)
async def start_payment(
    token: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    try:
        user_id = int(decode_token(token, "impaya_payment_start", settings))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=403, detail="Invalid or expired payment link") from exc
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    result = await _create_invoice(user, session, settings)
    return RedirectResponse(result["payment_url"], status_code=303)


async def _payment_by_operation(session: AsyncSession, operation_id: str) -> Payment | None:
    return await session.scalar(
        select(Payment).where(
            Payment.provider == "impaya",
            Payment.external_id == operation_id,
        )
    )


@router.get("/return/success", include_in_schema=False)
async def payment_success(
    operation_id: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    payment = await _payment_by_operation(session, operation_id)
    if not payment:
        return _page(
            "Платёж не найден",
            "Мы не смогли найти эту операцию. Вернитесь в бот и попробуйте снова.",
            ok=False,
            bot_username=settings.telegram_bot_username,
        )
    if payment.status == "paid":
        return _page(
            "Оплата подтверждена",
            "VIP-доступ уже активирован.",
            ok=True,
            bot_username=settings.telegram_bot_username,
        )

    client = ImpayaClient(settings)
    try:
        state = await client.transaction_state(
            customer_operation_id=operation_id,
            extended=True,
        )
    except ImpayaError as exc:
        payment.payload = {**payment.payload, "status_check_error": str(exc)}
        await session.commit()
        return _page(
            "Платёж проверяется",
            "Impaya ещё не подтвердила операцию. Вернитесь в бот через минуту.",
            ok=False,
            bot_username=settings.telegram_bot_username,
        )

    payment.payload = {**payment.payload, "extended_state": state}
    if not successful_state(state):
        payment.status = "processing"
        await session.commit()
        return _page(
            "Платёж обрабатывается",
            "Подтверждение ещё не получено. Доступ активируется после проверки статуса.",
            ok=False,
            bot_username=settings.telegram_bot_username,
        )

    now = datetime.now(UTC)
    user = await session.get(User, payment.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    current_end = user.vip_ends_at if user.vip_ends_at and user.vip_ends_at > now else now
    new_end = current_end + timedelta(days=settings.impaya_initial_access_days)
    user.vip_ends_at = new_end
    user.subscription_status = SubscriptionStatus.vip
    payment.status = "paid"
    payment.paid_at = now
    session.add(
        Subscription(
            user_id=user.id,
            status="active",
            source="impaya_initial",
            starts_at=now,
            ends_at=new_end,
        )
    )
    await session.commit()
    return _page(
        "Оплата подтверждена",
        f"VIP-доступ Phantom активирован на {settings.impaya_initial_access_days} день.",
        ok=True,
        bot_username=settings.telegram_bot_username,
    )


@router.get("/return/fail", include_in_schema=False)
async def payment_fail(
    operation_id: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    payment = await _payment_by_operation(session, operation_id)
    if payment and payment.status != "paid":
        payment.status = "failed"
        payment.payload = {**payment.payload, "redirect_result": "fail"}
        await session.commit()
    return _page(
        "Оплата не завершена",
        "Средства не списаны. Вернитесь в бот и повторите попытку.",
        ok=False,
        bot_username=settings.telegram_bot_username,
    )


@router.post("/check/{operation_id}")
async def check_payment(
    operation_id: str,
    user: CurrentUser,
    session: SessionDep,
    settings: Settings = Depends(get_settings),
) -> dict:
    payment = await _payment_by_operation(session, operation_id)
    if not payment or payment.user_id != user.id:
        raise HTTPException(status_code=404, detail="Payment not found")
    state = await ImpayaClient(settings).transaction_state(
        customer_operation_id=operation_id,
        extended=True,
    )
    return {
        "operation_id": operation_id,
        "status": payment.status,
        "confirmed": successful_state(state),
        "impaya": state,
    }
