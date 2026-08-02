from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, SessionDep
from app.api.routes import impaya as legacy
from app.core.config import Settings, get_settings
from app.core.security import decode_token
from app.db.models import User
from app.db.session import get_session
from app.services.access import get_monetization_settings

router = APIRouter(prefix="/api/payments/impaya", tags=["payments"])


async def _sync_price(session: AsyncSession, settings: Settings) -> None:
    monetization = await get_monetization_settings(session)
    settings.impaya_initial_amount_rub = int(monetization.entry_price_rub)


@router.get("/config")
async def impaya_config(user: CurrentUser, session: SessionDep, settings: Settings = Depends(get_settings)) -> dict:
    await _sync_price(session, settings)
    return await legacy.impaya_config(user, settings)


@router.post("/invoice")
async def create_invoice_for_current_user(user: CurrentUser, session: SessionDep, settings: Settings = Depends(get_settings)) -> dict:
    await _sync_price(session, settings)
    return await legacy._create_invoice(user, session, settings)


@router.get("/start/{token}", include_in_schema=False)
async def start_payment(token: str, session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    await _sync_price(session, settings)
    user_id = int(decode_token(token, "impaya_payment_start", settings))
    user = await session.get(User, user_id)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found")
    result = await legacy._create_invoice(user, session, settings)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(result["payment_url"], status_code=303)


@router.get("/return/success", include_in_schema=False)
async def payment_success(operation_id: str, session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)):
    await _sync_price(session, settings)
    return await legacy.payment_success(operation_id, session, settings)
