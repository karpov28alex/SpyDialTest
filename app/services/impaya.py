from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings


class ImpayaError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


@dataclass(slots=True)
class ImpayaInvoice:
    invoice_id: str
    transaction_id: str | None
    customer_operation_id: str
    amount: int
    raw: dict[str, Any]


class ImpayaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_url = settings.impaya_api_url.rstrip("/")
        self.payment_form_url = settings.impaya_payment_form_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        if not self.settings.impaya_token:
            raise ImpayaError("IMPAYA_TOKEN is not configured")
        return {
            "Authorization": f"Bearer {self.settings.impaya_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method,
                    f"{self.api_url}{path}",
                    headers=self._headers(),
                    **kwargs,
                )
        except httpx.HTTPError as exc:
            raise ImpayaError(f"Impaya connection error: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ImpayaError(
                f"Impaya returned non-JSON response ({response.status_code})"
            ) from exc

        if response.status_code >= 400:
            raise ImpayaError(
                payload.get("error_message") or payload.get("message") or "Impaya request failed",
                code=payload.get("error_code"),
                payload=payload,
            )
        if payload.get("success") is False:
            raise ImpayaError(
                payload.get("error_message") or payload.get("message") or "Impaya operation failed",
                code=payload.get("error_code"),
                payload=payload,
            )
        return payload

    async def create_initial_invoice(
        self,
        *,
        customer_operation_id: str,
        telegram_id: int,
        success_url: str,
        fail_url: str,
        amount_rub: int,
    ) -> ImpayaInvoice:
        if not self.settings.impaya_terminal_name:
            raise ImpayaError("IMPAYA_TERMINAL_NAME is not configured")

        amount_minor = int(amount_rub) * 100
        title = "VIP-доступ Phantom на 1 день"
        body = {
            "action": "pay",
            "amount": amount_minor,
            "customer_operation_id": customer_operation_id,
            "description": title,
            "custom_params": f'{{"telegram_id":{telegram_id},"kind":"initial"}}',
            "customization_form": {
                "title": "Подписка Phantom",
                "button_label": f"Оплатить {amount_rub} ₽",
            },
            "goods": [
                {
                    "name": title,
                    "price": amount_minor,
                    "quantity": 1,
                    "tax": 6,
                    "payment_subject_type": 4,
                    "payment_method_type": 4,
                    "agent_type": 0,
                }
            ],
            "lifetime": self.settings.impaya_invoice_lifetime,
            "merchant_user_id": f"tg_{telegram_id}",
            "payment_option_action": "bind_recurrent",
            "preferred_payment_option": {
                "card": {
                    "routing": "hard",
                    "terminal_name": self.settings.impaya_terminal_name,
                }
            },
            "redirect_data": {
                "success_redirect_url": success_url,
                "fail_redirect_url": fail_url,
            },
        }
        payload = await self._request("POST", "/invoice", json=body)
        invoice_id = payload.get("invoice_id")
        if not invoice_id:
            raise ImpayaError("Impaya did not return invoice_id", payload=payload)
        return ImpayaInvoice(
            invoice_id=str(invoice_id),
            transaction_id=payload.get("transaction_id"),
            customer_operation_id=customer_operation_id,
            amount=amount_minor,
            raw=payload,
        )

    async def transaction_state(
        self,
        *,
        customer_operation_id: str,
        extended: bool = False,
    ) -> dict[str, Any]:
        path = "/order/state/extended" if extended else "/order/state"
        return await self._request(
            "GET",
            path,
            params={
                "terminal_name": self.settings.impaya_terminal_name,
                "customer_operation_id": customer_operation_id,
            },
        )

    def payment_url(self, invoice_id: str) -> str:
        return f"{self.payment_form_url}/{invoice_id}"


def successful_state(payload: dict[str, Any]) -> bool:
    state = payload.get("state")
    if not state and isinstance(payload.get("transaction"), dict):
        state = payload["transaction"].get("state")
    return str(state or "").upper() in {
        "COMPLETED",
        "CONFIRMED",
        "CAPTURED",
        "PAID",
        "SUCCESS",
    }
