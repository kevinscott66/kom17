"""Crypto Pay (CryptoBot) outbound API client (T-027).

The webhook *adapter* (:mod:`crypto`) only verifies signatures and
parses inbound ``invoice_paid`` notifications. This module is the
complementary OUTBOUND surface — the bot calling Crypto Pay's HTTP API:

* :meth:`CryptoPayClient.create_invoice` → ``createInvoice``. Used by the
  self-service top-up flow to mint a payable invoice and hand the user a
  ``pay_url``. The eventual payment comes back through the webhook
  adapter, so this client never credits a wallet itself.
* :meth:`CryptoPayClient.transfer` → ``transfer``. The payout primitive
  behind ``/withdraw``: sends ``asset`` to a Telegram ``user_id`` from
  the *app's* Crypto Pay wallet. Idempotent on ``spend_id`` — Crypto Pay
  guarantees a given ``spend_id`` transfers at most once, which lets the
  withdraw service retry safely without double-paying.

Provider spec: https://help.crypt.bot/crypto-pay-api

Auth: a single ``Crypto-Pay-API-Token`` header carries the app token
(same ``CRYPTO_PAY_TOKEN`` the webhook adapter hashes for signature
verification). Mainnet base URL is ``https://pay.crypt.bot/api``; the
testnet host (``https://testnet-pay.crypt.bot/api``) is injectable via
``base_url`` so staging can point at the test bot without code changes.

Error contract: every API call returns ``{"ok": bool, ...}``. ``ok:true``
carries ``result``; ``ok:false`` carries ``error: {code, name}``. We map
``false`` to a raised :class:`CryptoPayError` (or the
:class:`CryptoPayInsufficientFunds` subclass when the app wallet can't
cover a transfer) so callers branch on exceptions, not return-value
sentinels — the withdraw service must distinguish "retry later" from
"refund the user" and a typed exception makes that unambiguous.

That split is two-way, and a payout needs three. Beside "it worked" and
"it was refused" sits "we don't know": a socket timeout, a 502 from a
gateway, a body that isn't JSON. Reading those as a refusal is not a
conservative default — it is the expensive one, because the withdraw
service reacts to a refusal by putting the request back in a queue where
an admin can reject it and refund coins for crypto that already left.
:class:`CryptoPayUnconfirmed` names that third answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from telegram_invite_bot.utils.http_read import send_capped

log = logger.bind(component="services.payments.crypto_client")

_MAINNET_BASE_URL = "https://pay.crypt.bot/api"

# Crypto Pay's own error name for "the app's wallet balance is below the
# requested transfer amount". Surfaced as a distinct exception so the
# withdraw service can tell the user "try again later" and alert the
# operator to top up the app wallet — rather than treating it like a
# user-input error.
_INSUFFICIENT_FUNDS_ERROR = "NOT_ENOUGH_COINS_TO_TRANSFER"


class CryptoPayError(Exception):
    """Crypto Pay API returned ``ok:false`` or was unreachable.

    Carries the provider's ``error.name`` (or a transport description)
    in :attr:`name` so the caller can log it without re-parsing.
    """

    def __init__(self, message: str, *, name: str | None = None) -> None:
        super().__init__(message)
        self.name = name


class CryptoPayInsufficientFunds(CryptoPayError):
    """The app's Crypto Pay wallet can't cover the requested transfer.

    Operator problem (top up the app wallet), not a user-input problem
    — the withdraw service must NOT consume the user's escrow on this
    error; it leaves the request pending for a retry.
    """


class CryptoPayUnconfirmed(CryptoPayError):
    """The call went out but its outcome never came back.

    A transport error, a 5xx, or a body we can't parse all say the same
    thing: the request may have executed and we did not see the answer.
    For a read that is merely annoying. For ``transfer`` it is the
    difference between "no money moved" and "the user is holding the
    crypto and we don't know it" — and the withdraw service treats those
    two oppositely, so guessing is not available.

    Retrying is always safe on ``transfer``: ``spend_id`` makes the
    provider execute it at most once, so a retry either confirms the
    original transfer or raises this again.

    A 4xx is deliberately NOT unconfirmed. A bad token, a malformed body
    or a rate-limit is refused before the provider touches a wallet, and
    calling those unknown would strand the request in a status no admin
    surface can clear — trading a money bug for a stuck-queue bug.
    """


@dataclass(frozen=True, slots=True)
class InvoiceResult:
    """Subset of ``createInvoice`` → ``result`` the top-up flow needs."""

    invoice_id: int
    pay_url: str
    asset: str
    amount: str


@dataclass(frozen=True, slots=True)
class TransferResult:
    """Subset of ``transfer`` → ``result`` the withdraw flow needs."""

    transfer_id: int
    spend_id: str
    asset: str
    amount: str


class CryptoPayClient:
    """Thin async HTTP client over the Crypto Pay API.

    Stateless past the token + base URL. Tests inject an
    ``httpx.AsyncClient`` bound to a ``MockTransport``; production wires
    a long-lived client through DI so the connection pool persists
    (mirrors :class:`WeatherService`'s shared-client posture).
    """

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = _MAINNET_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def create_invoice(
        self,
        *,
        asset: str,
        amount: str,
        payload: str,
        description: str | None = None,
        fiat: str | None = None,
    ) -> InvoiceResult:
        """Mint a payable invoice. ``payload`` round-trips back to us in
        the webhook (Crypto Pay echoes it on ``invoice_paid``); the
        top-up flow stuffs the user_id there so the credit path knows
        whom to credit. ``amount`` is a decimal string per the API.

        ``fiat`` decides what ``amount`` MEANS, and #1232 is the bill for
        getting that wrong. Left ``None``, the call takes the provider's
        default ``currency_type="crypto"``: ``amount`` is read as that
        many units of ``asset``, so ``asset="BTC", amount="5"`` mints an
        invoice for five bitcoin. Set (to ``"USD"``), the invoice is
        denominated in that fiat currency and ``asset`` narrows the
        settlement options to the coin the user picked — the only form
        that can honour a button reading ``$5``.
        """
        body: dict[str, Any] = {"amount": amount, "payload": payload}
        if fiat:
            body["currency_type"] = "fiat"
            body["fiat"] = fiat
            # Documented as a comma-separated string, and one element is
            # the whole point: the user already chose their asset on the
            # previous screen, so leaving it at the provider default
            # would let them settle in a coin the button never offered.
            body["accepted_assets"] = asset
        else:
            body["asset"] = asset
        if description:
            body["description"] = description
        result = await self._post("createInvoice", body)
        pay_url = result.get("pay_url") or result.get("bot_invoice_url") or ""
        return InvoiceResult(
            invoice_id=int(result.get("invoice_id") or 0),
            pay_url=str(pay_url),
            asset=str(result.get("asset") or asset),
            amount=str(result.get("amount") or amount),
        )

    async def transfer(
        self,
        *,
        user_id: int,
        asset: str,
        amount: str,
        spend_id: str,
        comment: str | None = None,
    ) -> TransferResult:
        """Send ``amount`` of ``asset`` to ``user_id`` from the app wallet.

        ``spend_id`` (≤64 chars) is the idempotency key: Crypto Pay
        executes a given ``spend_id`` at most once, so a retried call
        returns the original transfer instead of double-paying. Raises
        :class:`CryptoPayInsufficientFunds` when the app wallet is short,
        :class:`CryptoPayError` on any other API/transport failure.
        """
        result = await self._post(
            "transfer",
            {
                "user_id": user_id,
                "asset": asset,
                "amount": amount,
                "spend_id": spend_id,
                # disable_send_notification omitted → user gets the
                # native Crypto Pay "you received X" DM (legacy parity).
                **({"comment": comment} if comment else {}),
            },
        )
        return TransferResult(
            transfer_id=int(result.get("transfer_id") or 0),
            spend_id=str(result.get("spend_id") or spend_id),
            asset=str(result.get("asset") or asset),
            amount=str(result.get("amount") or amount),
        )

    async def _post(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST ``body`` to ``{base_url}/{method}``, unwrap ``result``.

        Centralises auth header, ``ok:false`` → exception mapping, and
        transport-error wrapping so the public methods stay declarative.
        """
        if self._client is not None:
            return await self._post_with(self._client, method, body)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._post_with(client, method, body)

    async def _post_with(
        self, client: httpx.AsyncClient, method: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{self._base_url}/{method}"
        try:
            response = await send_capped(
                client,
                "POST",
                url,
                json=body,
                headers={"Crypto-Pay-API-Token": self._token},
            )
        except httpx.HTTPError as exc:
            # Covers the read timeout, which is the whole reason
            # ``CryptoPayUnconfirmed`` exists: the request was written to
            # the socket, so the provider may well have executed it.
            log.bind(method=method).warning("crypto-pay HTTP error: {e}", e=exc)
            raise CryptoPayUnconfirmed(f"{method} transport error: {exc}") from exc

        if response.status_code != 200:
            # 4xx is a refusal decided before anything executed (bad
            # token, malformed body, rate limit). 5xx — and anything else
            # non-200 — can just as easily land *after* execution, from a
            # gateway that lost the backend's answer.
            if 400 <= response.status_code < 500:
                raise CryptoPayError(f"{method} HTTP {response.status_code}", name="http_error")
            raise CryptoPayUnconfirmed(f"{method} HTTP {response.status_code}", name="http_error")
        try:
            payload = response.json()
        except ValueError as exc:
            # 200 with a body we can't read is most likely a proxy or
            # captive-portal interstitial standing in front of a request
            # that may have gone through.
            raise CryptoPayUnconfirmed(f"{method} non-JSON response") from exc
        if not isinstance(payload, dict):
            raise CryptoPayUnconfirmed(f"{method} non-object response")

        if not payload.get("ok"):
            error = payload.get("error")
            name = None
            if isinstance(error, dict):
                name = str(error.get("name") or error.get("code") or "")
            if name == _INSUFFICIENT_FUNDS_ERROR:
                raise CryptoPayInsufficientFunds(f"{method}: app wallet insufficient", name=name)
            raise CryptoPayError(f"{method}: api error {name}", name=name)

        result = payload.get("result")
        if not isinstance(result, dict):
            # The provider said ``ok`` — this is the branch where the
            # transfer most likely DID happen and only the receipt is
            # missing, so it is the last thing that may be read as a
            # refusal.
            raise CryptoPayUnconfirmed(f"{method}: ok:true but no result object")
        return result
