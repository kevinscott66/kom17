"""User-side RollyPay payment creation for the ``/topup`` flow.

The rouble counterpart to :mod:`crypto_invoices`. RollyPay is the first
*fiat* provider with a full in-bot cycle: YooKassa and Stripe ship only
the crediting half (their ``/topup`` rows render honest "checkout in the
bot is not available" copy and rely on an operator issuing the payment
externally), whereas here the bot mints the payment itself and hands the
user a pay button — SBP, card or crypto, chosen on RollyPay's own page.

Degraded-mode doctrine, same as every other method: a missing
``ROLLYPAY_API_KEY`` must not crash the menu. :meth:`RollyPayTopupService
.create_payment` returns :attr:`TopupInvoiceOutcome.NOT_CONFIGURED` and
the handler renders the localized unavailable copy.

The coin figures on the buttons are a *quote*, not a promise the credit
path reads back. The callback recomputes coins from the amount RollyPay
says was actually paid, so a button rendered against a stale FX fix
under-promises or over-promises by a few coins and the wallet still
lands on the server-derived number. That asymmetry is deliberate: it is
the same rule that keeps ``metadata.coins`` off the YooKassa credit path
(R-FIX-003).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from loguru import logger

from telegram_invite_bot.services.payments.crypto_invoices import TopupInvoiceOutcome
from telegram_invite_bot.services.payments.rates import (
    FALLBACK_USD_TO_RUB,
    coins_for_rub,
)
from telegram_invite_bot.services.payments.rollypay_client import (
    PaymentResult,
    RollyPayClient,
    RollyPayError,
)

if TYPE_CHECKING:
    from telegram_invite_bot.config.settings import Settings

log = logger.bind(component="services.payments.rollypay_invoices")

#: Rouble amounts offered on the /topup → RollyPay screen.
#:
#: Chosen to land on round coin figures at the offline anchor
#: (USD/RUB = 90, 900 coins per USD): 100 ₽ → 1000 🪙, and so on up.
#: Away from the anchor the numbers stop being round, which is correct —
#: the coin is priced in dollars and the rouble leg follows the fix
#: rather than pretending not to (see ``rates.coins_for_rub``).
RUB_AMOUNTS: Final[tuple[int, ...]] = (100, 250, 500, 1000)


@dataclass(frozen=True, slots=True)
class RollyPayTopup:
    """What the handler needs to render the pay button."""

    outcome: TopupInvoiceOutcome
    pay_url: str | None = None
    payment_id: str | None = None


def quote_coins(amount_rub: int, usd_to_rub: float = FALLBACK_USD_TO_RUB) -> int:
    """Coins a button should advertise for ``amount_rub``.

    Thin wrapper over :func:`coins_for_rub` so the keyboard and the
    credit path cannot drift into two different formulas — the only
    difference between the quote and the credit is *when* the FX fix is
    read, never *how* the price is computed.
    """
    return coins_for_rub(Decimal(amount_rub), usd_to_rub)


def build_order_id(user_id: int) -> str:
    """A per-attempt, merchant-unique ``order_id``.

    Fresh per click rather than derived from ``(user_id, amount)``: a
    user who opens a payment, wanders off, and comes back to click again
    should get a live payment rather than a 409 or a resurrected one
    that has already expired. RollyPay's payments expire in 30 minutes,
    so reusing the id across attempts would strand the second click
    behind a dead page.

    Double-crediting is not a concern here — the callback is idempotent
    on ``payment_id``, so two payments for one intent simply means the
    user can pay either, and paying both credits both, which is exactly
    what paying twice should do.
    """
    return f"topup-{user_id}-{uuid.uuid4().hex[:16]}"


class RollyPayTopupService:
    """Mint RollyPay payments carrying the user_id in metadata."""

    def __init__(
        self,
        api_key: str | None,
        *,
        client_factory: Callable[[str], RollyPayClient] = RollyPayClient,
    ) -> None:
        self._api_key = api_key
        self._client_factory = client_factory

    def available(self) -> bool:
        """True iff an API key is configured.

        Sync, unlike :meth:`CryptoTopupService.available` — Crypto Pay's
        token can be set at runtime through an in-bot panel and so has
        to be re-resolved from the database on every call; RollyPay's
        key comes from ``.env`` only, so there is nothing to await.
        """
        return bool(self._api_key)

    async def create_payment(
        self,
        *,
        user_id: int,
        amount_rub: int,
        description: str | None = None,
    ) -> RollyPayTopup:
        """Create one payment; never raises."""
        if not self._api_key:
            log.info("rollypay-topup: API key not configured — degraded")
            return RollyPayTopup(outcome=TopupInvoiceOutcome.NOT_CONFIGURED)
        client = self._client_factory(self._api_key)
        order_id = build_order_id(user_id)
        try:
            result: PaymentResult = await client.create_payment(
                # Two decimals: RollyPay quotes and echoes amounts as
                # decimal strings, and sending "100" where the callback
                # will say "100.00" makes reconciliation by eye harder
                # than it needs to be.
                amount=f"{amount_rub}.00",
                order_id=order_id,
                user_id=user_id,
                description=description or f"Пополнение баланса на {amount_rub} ₽",
            )
        except RollyPayError as exc:
            log.bind(uid=user_id, rub=amount_rub, order_id=order_id).warning(
                "rollypay-topup: create_payment failed: {e}", e=exc
            )
            return RollyPayTopup(outcome=TopupInvoiceOutcome.FAILED)
        if not result.pay_url.startswith("https://"):
            # Empty, plain-http, or a scheme that is not a web page at
            # all. The last case is the one worth a guard rather than a
            # shrug: this string goes straight onto an inline button's
            # ``url``, and a ``tg://`` payload there is a deep link the
            # user taps believing it is a payment page. RollyPay is
            # trusted — we hold their API key — but "trusted" describes
            # the account, not every response that arrives claiming to
            # be from it, and https is what a hosted checkout is.
            log.bind(uid=user_id, payment_id=result.payment_id).warning(
                "rollypay-topup: create_payment returned no usable https pay_url"
            )
            return RollyPayTopup(outcome=TopupInvoiceOutcome.FAILED)
        log.bind(
            uid=user_id,
            rub=amount_rub,
            order_id=order_id,
            payment_id=result.payment_id,
        ).info("rollypay-topup: payment created")
        return RollyPayTopup(
            outcome=TopupInvoiceOutcome.CREATED,
            pay_url=result.pay_url,
            payment_id=result.payment_id,
        )


def build_rollypay_topup_service(*, settings: Settings) -> RollyPayTopupService:
    """Production wiring — and the place the two-key rule is enforced.

    The service is handed a key **only when both halves are present**.
    That is not belt-and-braces around
    :attr:`~telegram_invite_bot.config.settings.PaymentsConfig.
    rollypay_configured`; it is the same rule applied where it bites.
    An api key without a signing secret is the one combination that
    takes money and cannot give anything back: the bot happily mints a
    pay page, the user pays, and ``/rollypay-webhook`` answers 503 to
    every callback because it has nothing to verify them with. The coins
    never land, and the only trace is a log line on a box the payer
    cannot read.

    Withholding the key turns that into the harmless failure instead —
    the method renders as unavailable and nobody is charged. An operator
    who set one half sees a dead button, which is the correct signal,
    and the webhook's own 503 says the other half of the same sentence.
    """
    cfg = settings.payments
    api_key = (
        cfg.rollypay_api_key.get_secret_value()
        if cfg.rollypay_configured and cfg.rollypay_api_key is not None
        else None
    )
    return RollyPayTopupService(api_key)
