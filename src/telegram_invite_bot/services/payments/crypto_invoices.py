"""User-side Crypto Pay invoice creation for the ``/topup`` flow (A1: L-81).

Legacy ``_crypto_create_invoice`` (bot.py:18295-18324) called
``CryptoPaySDK.createInvoice`` with the user_id stuffed into the
invoice ``payload`` and returned a ``pay_url`` — the eventual
``invoice_paid`` webhook then read that payload back to know whom to
credit (``webhook/payments.py`` → :class:`CryptoAdapter.parse_event`,
which mirrors the legacy double-nested ``payload.payload`` reading
order). This module is the new-pipeline equivalent, built on the
already-ported outbound :class:`CryptoPayClient` instead of the SDK.

Degraded-mode doctrine: a missing ``CRYPTO_PAY_TOKEN`` (neither the
``economy.runtime_secrets`` override nor ``.env``) must NOT crash the
menu — :meth:`CryptoTopupService.create_invoice` returns the typed
:attr:`TopupInvoiceOutcome.NOT_CONFIGURED` and the handler renders a
localized "method unavailable" row, matching the legacy
``crypto_payment_not_configured`` posture (bot.py:18279-18285) and the
webhook side's 503.

The token resolver is injected as a zero-arg coroutine factory so the
service stays testable without an :class:`EngineRegistry` — production
wires :func:`telegram_invite_bot.services.payments.secret_resolver.
resolve_crypto_token` through :func:`build_crypto_topup_service`, and
the resolver is re-invoked per call (NOT cached) so a token set at
runtime via ``/set_crypto_token`` takes effect immediately (T-027).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.services.payments.crypto_client import (
    CryptoPayClient,
    CryptoPayError,
    CryptoPayUnconfirmed,
    InvoiceResult,
)
from telegram_invite_bot.services.payments.rates import (
    COINS_PER_USD as _COINS_PER_USD,
)
from telegram_invite_bot.services.payments.rates import (
    INVOICE_FIAT,
    USD_PEGGED_ASSETS,
)
from telegram_invite_bot.services.payments.secret_resolver import resolve_crypto_token

if TYPE_CHECKING:
    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="services.payments.crypto_invoices")

#: Crypto assets offered on the /topup → Crypto Pay screen. Mirrors
#: legacy ``_crypto_currency_keyboard`` (bot.py:18053-18061: USDT
#: (TRC20) / BTC / TON).
TOPUP_ASSETS: tuple[str, ...] = ("USDT", "BTC", "TON")

#: Fixed USD-equivalent invoice amounts. Mirrors legacy
#: ``cb_crypto_currency`` (bot.py:18334: ``amounts_usd = [5, 10, 25, 50]``).
#: "USD-equivalent" is now literal rather than aspirational — see
#: :meth:`CryptoTopupService._mint`.
CRYPTO_USD_AMOUNTS: tuple[int, ...] = (5, 10, 25, 50)

#: Coins credited per USD, re-exported so /topup callers keep their
#: import site. T-019 (R5) moved the definition to
#: :mod:`~telegram_invite_bot.services.payments.rates` — the invoice
#: button and the crediting webhook now read the same object, so the
#: coins a user is promised cannot drift from the coins they get.
COINS_PER_USD = _COINS_PER_USD

# Invoice lifetime: there is deliberately no constant here. Legacy
# passed ``expires_in: 3600`` (bot.py:18309); the new CryptoPayClient
# does not forward an expiry at all (the API default is no expiry) —
# acceptable, because a stale invoice that gets paid still credits
# correctly through the idempotent webhook.
#
# #1613: this was written as a ``#:`` doc-comment, which binds to the
# NEXT definition. With no constant left to bind to, Sphinx was
# attaching a note about invoice lifetime to ``TopupInvoiceOutcome``.


class TopupInvoiceOutcome(StrEnum):
    """Typed result of a create-invoice attempt for the handler to render."""

    CREATED = "created"
    NOT_CONFIGURED = "not_configured"
    """No Crypto Pay token anywhere — render the degraded copy."""
    FAILED = "failed"
    """Token present but the API call failed — render "try later"."""


@dataclass(frozen=True, slots=True)
class TopupInvoice:
    """The two fields the handler needs to render the pay button."""

    outcome: TopupInvoiceOutcome
    pay_url: str | None = None
    invoice_id: int | None = None


class CryptoTopupService:
    """Mint Crypto Pay top-up invoices with the user_id as payload."""

    def __init__(
        self,
        token_resolver: Callable[[], Awaitable[str | None]],
        *,
        client_factory: Callable[[str], CryptoPayClient] = CryptoPayClient,
    ) -> None:
        self._token_resolver = token_resolver
        self._client_factory = client_factory

    async def available(self) -> bool:
        """True iff a Crypto Pay token is currently configured."""
        return await self._token_resolver() is not None

    async def create_invoice(
        self,
        *,
        user_id: int,
        asset: str,
        amount_usd: int,
        coins: int,
        description: str | None = None,
    ) -> TopupInvoice:
        """Create one invoice; never raises.

        ``payload=str(user_id)`` is the credit-routing contract with
        :class:`CryptoAdapter.parse_event` (legacy bot.py:18308:
        ``"payload": str(user_id)``). ``coins`` is display-only here —
        the webhook recomputes the credit from the PAID amount, so a
        drifted button label can't inflate the credit.

        ``amount_usd`` is dollars, and #1232 is what it took for that to
        become true on the wire as well as in the parameter name.
        """
        token = await self._token_resolver()
        if token is None:
            log.info("crypto-topup: token not configured — degraded")
            return TopupInvoice(outcome=TopupInvoiceOutcome.NOT_CONFIGURED)
        client = self._client_factory(token)
        try:
            result = await self._mint(
                client,
                asset=asset,
                amount_usd=amount_usd,
                user_id=user_id,
                description=description or f"Top-up: {coins} coins",
            )
        except CryptoPayError as exc:
            log.bind(uid=user_id, asset=asset, usd=amount_usd).warning(
                "crypto-topup: createInvoice failed: {e}", e=exc
            )
            return TopupInvoice(outcome=TopupInvoiceOutcome.FAILED)
        if not result.pay_url:
            log.bind(uid=user_id, invoice_id=result.invoice_id).warning(
                "crypto-topup: createInvoice returned no pay_url"
            )
            return TopupInvoice(outcome=TopupInvoiceOutcome.FAILED)
        log.bind(uid=user_id, asset=asset, usd=amount_usd, invoice_id=result.invoice_id).info(
            "crypto-topup: invoice created"
        )
        return TopupInvoice(
            outcome=TopupInvoiceOutcome.CREATED,
            pay_url=result.pay_url,
            invoice_id=result.invoice_id,
        )

    async def _mint(
        self,
        client: CryptoPayClient,
        *,
        asset: str,
        amount_usd: int,
        user_id: int,
        description: str,
    ) -> InvoiceResult:
        """Mint the invoice in dollars, with one narrow legacy fallback.

        #1232: the screen promises ``$N``, so the invoice has to be
        denominated in dollars. Passing a bare ``asset`` instead makes
        Crypto Pay read ``amount`` as N units of that asset — five
        bitcoin for a five-dollar button, five orders of magnitude off.

        The fallback exists because the fiat form is the new one and
        this path is live in production. If the provider refuses it
        outright — ``ok:false`` or a 4xx, i.e. a decision taken before
        anything was minted — a stablecoin is within rounding of the
        dollar anyway, so retrying in asset units keeps the one leg that
        works today rather than taking the whole method down with the
        change that was meant to fix it. BTC and TON get no fallback: a
        correctly-priced invoice or none, never a wrong one. And an
        outcome we could not read (:class:`CryptoPayUnconfirmed` — a
        timeout, a 5xx) is never retried, because the first invoice may
        well exist and the user would be shown the second.
        """
        try:
            return await client.create_invoice(
                asset=asset,
                amount=str(amount_usd),
                payload=str(user_id),
                description=description,
                fiat=INVOICE_FIAT,
            )
        except CryptoPayUnconfirmed:
            raise
        except CryptoPayError as exc:
            if asset not in USD_PEGGED_ASSETS:
                raise
            log.bind(uid=user_id, asset=asset, usd=amount_usd).warning(
                "crypto-topup: fiat invoice refused ({e}) — retrying in asset units", e=exc
            )
            return await client.create_invoice(
                asset=asset,
                amount=str(amount_usd),
                payload=str(user_id),
                description=description,
            )


def build_crypto_topup_service(
    *, registry: EngineRegistry, settings: Settings
) -> CryptoTopupService:
    """Production wiring: resolver = runtime-secret override → ``.env``."""

    async def _resolve() -> str | None:
        return await resolve_crypto_token(registry=registry, settings=settings)

    return CryptoTopupService(_resolve)
