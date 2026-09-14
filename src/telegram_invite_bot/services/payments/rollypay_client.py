"""RollyPay outbound API client.

Provider spec: https://docs.rollypay.io/api/payments/

The webhook *adapter* (:mod:`rollypay`) verifies inbound callbacks. This
module is the complementary OUTBOUND surface: the bot asking RollyPay to
create a payment and handing the user back a ``pay_url``. It never
credits a wallet — the eventual ``payment.paid`` callback does that,
through the same :class:`~telegram_invite_bot.services.payments_service.
PaymentsService` pipeline every other provider uses.

Auth is two headers rather than one:

* ``X-API-Key`` — the terminal key. Note this is a *different* secret
  from the ``signing_secret`` the adapter verifies with; RollyPay issues
  both per terminal and they are not interchangeable. Mixing them up
  produces a 401 here and a silent signature mismatch there, so they are
  separate settings fields with separate names.
* ``X-Nonce`` — a fresh value per request, RollyPay's replay guard.
  Generated here with :func:`uuid.uuid4` so no caller can accidentally
  reuse one; a retried call is a *new* request and gets a new nonce.

Idempotency of the payment itself rides on ``order_id``, which is ours
to choose and must be unique per merchant. :meth:`RollyPayClient.
create_payment` requires it from the caller rather than inventing one,
because the caller is the only layer that knows whether it wants a
retry to reuse the existing payment or open a second one.

Error contract mirrors :class:`~telegram_invite_bot.services.payments.
crypto_client.CryptoPayClient`: any transport failure, non-2xx, or
unparseable response raises :class:`RollyPayError`, so callers branch on
exceptions rather than sentinel return values.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

import httpx
from loguru import logger

from telegram_invite_bot.utils.http_read import send_capped

log = logger.bind(component="services.payments.rollypay_client")

_BASE_URL: Final[str] = "https://rollypay.io/api/v1"

#: Payment methods RollyPay accepts on ``payment_method``. Omitting the
#: field lets the payer choose on RollyPay's own page, which is what the
#: top-up flow does — one less decision in the bot, and the hosted page
#: knows which methods the terminal actually has enabled.
PAYMENT_METHODS: Final[tuple[str, ...]] = ("sbp", "card", "intl_card", "crypto")


class RollyPayError(Exception):
    """RollyPay API was unreachable or answered with an error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PaymentResult:
    """Subset of ``POST /payments`` the top-up flow needs."""

    payment_id: str
    pay_url: str
    order_id: str
    amount: str
    status: str


class RollyPayClient:
    """Thin async HTTP client over the RollyPay payments API.

    Stateless past the key and base URL. Tests inject an
    ``httpx.AsyncClient`` bound to a ``MockTransport``; production may
    pass a long-lived client so the connection pool persists.
    """

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = _BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def create_payment(
        self,
        *,
        amount: str,
        order_id: str,
        user_id: int,
        description: str | None = None,
        payment_currency: str = "RUB",
        payment_method: str | None = None,
    ) -> PaymentResult:
        """Create a payment and return its hosted ``pay_url``.

        ``user_id`` goes into ``metadata``, which RollyPay echoes back
        verbatim inside the *signed* callback — that is the credit
        routing contract with :meth:`~telegram_invite_bot.services.
        payments.rollypay.RollyPayAdapter.parse_event`. It is safe to
        trust on the way back precisely because the signature covers the
        whole callback body, metadata included.

        Note what is deliberately NOT sent: a coin count. The callback
        recomputes coins from the paid amount, so there is no number
        here that a drifted button label or a moved FX fix could turn
        into an over-credit.
        """
        body: dict[str, Any] = {
            "amount": amount,
            "payment_currency": payment_currency,
            "order_id": order_id,
            "metadata": {"user_id": str(user_id)},
        }
        if description:
            body["description"] = description
        if payment_method:
            body["payment_method"] = payment_method
        result = await self._post("/payments", body)
        pay_url = str(result.get("pay_url") or "")
        return PaymentResult(
            payment_id=str(result.get("payment_id") or ""),
            pay_url=pay_url,
            order_id=str(result.get("order_id") or order_id),
            amount=str(result.get("amount") or amount),
            status=str(result.get("status") or ""),
        )

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        """Fetch a payment by id.

        Not on the credit path — the signed callback is authoritative
        and needs no reverify hop. This exists for operator tooling and
        for reconciling a payment whose callback never arrived (RollyPay
        gives up after 8 delivery attempts).
        """
        return await self._get(f"/payments/{payment_id}")

    async def _get(self, path: str) -> dict[str, Any]:
        if self._client is not None:
            return await self._request(self._client, "GET", path, None)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._request(client, "GET", path, None)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            return await self._request(self._client, "POST", path, body)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._request(client, "POST", path, body)

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """One request, with auth headers and error mapping.

        The nonce is minted per call, not per client: RollyPay rejects a
        repeated one, and a client instance may well outlive several
        requests.
        """
        url = f"{self._base_url}{path}"
        headers = {
            "X-API-Key": self._api_key,
            "X-Nonce": uuid.uuid4().hex,
        }
        try:
            response = await send_capped(
                client, method, url, json=body, headers=headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            log.bind(path=path).warning("rollypay HTTP error: {e}", e=exc)
            raise RollyPayError(f"{method} {path} transport error: {exc}") from exc

        if response.status_code >= 400:
            # The body often carries a machine-readable reason; log it
            # but keep it out of the exception message, which ends up in
            # user-adjacent places.
            log.bind(path=path, status=response.status_code).warning(
                "rollypay API error: {text}", text=response.text[:500]
            )
            raise RollyPayError(
                f"{method} {path} HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RollyPayError(f"{method} {path}: non-JSON response") from exc
        if not isinstance(payload, dict):
            raise RollyPayError(f"{method} {path}: non-object response")
        return payload
