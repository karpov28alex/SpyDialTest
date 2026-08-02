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
            "protocol": self.settings.impaya_protocol,
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        normalized_path = path if path.startswith("/") else f"/{path}"
        url = f"{self.api_url}{normalized_path}"
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                response = await client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise ImpayaError(f"Impaya connection error for {url}: {exc}") from exc

        response_text = response.text[:1000]
        try:
            payload = response.json()
        except ValueError as exc:
            raise ImpayaError(
                f"Impaya returned non-JSON response ({response.status_code}) for {url}: {response_text!r}",
                payload={
                    "status_code": response.status_code,
                    "url": url,
                    "body": response_text,
                    "location": response.headers.get("location"),
                },
            ) from exc

        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            raise ImpayaError(
                payload.get("error_message")
                or payload.get("message")
                or error.get("message")
                or f"Impaya request failed ({response.status_code})",
                code=payload.get("error_code") or error.get("code"),
                payload={**payload, "status_code": response.status_code, "url": url},
            )
        if payload.get("success") is False:
            raise ImpayaError(
                payload.get("error_message") or payload.get("message") or "Impaya operation failed",
                code=payload.get("error_code"),
                payload={**payload, "status_code": response.status_code, "url": url},
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
        body = {
            "action": "authorize",
            "amount": amount_minor,
            "customer_operation_id": customer_operation_id,
            "customization_form": {
                "button_label": f"Привязать карту за {amount_rub} ₽",
            },
            "goods": [
                {
                    "name": "Привязка карты",
                    "price": amount_minor,
                    "tax": 6,
                    "payment_subject_type": 4,
                    "payment_method_type": 4,
                    "agent_type": 0,
                    "supplier": {
                        "name": "",
                        "inn": "",
                        "phone_numbers": None,
                    },
                    "quantity": 1,
                }
            ],
            "lifetime": self.settings.impaya_invoice_lifetime,
            "payment_option_action": "bind_recurrent",
            "post_action": "void",
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
        payload = await self._request("POST", self.settings.impaya_invoice_path, json=body)
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

    async def recurrent_pay(
        self,
        *,
        customer_operation_id: str,
        amount_rub: int,
        binding_id: str,
        impaya_user_id: str,
        merchant_user_id: str,
        description: str,
    ) -> dict[str, Any]:
        amount_minor = int(amount_rub) * 100
        body = {
            "amount": amount_minor,
            "customer_operation_id": customer_operation_id,
            "description": description,
            "terminal_name": self.settings.impaya_terminal_name,
            "merchant_user_id": merchant_user_id,
            "is_recurrent": True,
            "payment_initiator": "MIT",
            "payment_option_data": {
                "impaya_pay": {
                    "binding_id": binding_id,
                    "user_id": impaya_user_id,
                    "merchant_user_id": merchant_user_id,
                }
            },
        }
        return await self._request("POST", self.settings.impaya_pay_path, json=body)

    async def transaction_state(
        self,
        *,
        customer_operation_id: str,
        extended: bool = False,
    ) -> dict[str, Any]:
        path = self.settings.impaya_state_extended_path if extended else self.settings.impaya_state_path
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


def transaction_state_name(payload: dict[str, Any]) -> str:
    state = payload.get("state")
    if not state and isinstance(payload.get("transaction"), dict):
        state = payload["transaction"].get("state")
    return str(state or "").strip()


def successful_state(payload: dict[str, Any]) -> bool:
    return transaction_state_name(payload).upper() in {
        "COMPLETED",
        "CONFIRMED",
        "CAPTURED",
        "PAID",
        "SUCCESS",
    }


def binding_created(payload: dict[str, Any]) -> bool:
    binding = payload.get("binding")
    return bool(
        isinstance(binding, dict)
        and binding.get("created")
        and binding.get("binding_id")
        and binding.get("user_id")
    )
