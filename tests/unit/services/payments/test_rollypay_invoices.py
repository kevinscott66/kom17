"""Unit tests for the user-side RollyPay payment minting.

The load-bearing test here is the two-key gate
(:func:`build_rollypay_topup_service`): an api key configured *without*
its signing secret is the one combination that takes money and cannot
give anything back — the bot mints a pay page, the user pays, and
``/rollypay-webhook`` answers 503 to every callback because it has
nothing to verify them with. The rest pins the degraded-mode contract
(never raise, always return an outcome) and the quote formula.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from telegram_invite_bot.services.payments.crypto_invoices import TopupInvoiceOutcome
from telegram_invite_bot.services.payments.rates import (
    COINS_PER_USD,
    FALLBACK_USD_TO_RUB,
)
from telegram_invite_bot.services.payments.rollypay_client import (
    PaymentResult,
    RollyPayClient,
    RollyPayError,
)
from telegram_invite_bot.services.payments.rollypay_invoices import (
    RUB_AMOUNTS,
    RollyPayTopupService,
    build_order_id,
    build_rollypay_topup_service,
    quote_coins,
)


def _settings(*, api_key: str | None, secret: str | None) -> Any:
    """A settings double whose ``rollypay_configured`` is the real AND."""
    return SimpleNamespace(
        payments=SimpleNamespace(
            rollypay_api_key=(
                SimpleNamespace(get_secret_value=lambda: api_key) if api_key is not None else None
            ),
            rollypay_signing_secret=(
                SimpleNamespace(get_secret_value=lambda: secret) if secret is not None else None
            ),
            rollypay_configured=bool(api_key and secret),
        )
    )


class FakeClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.calls: list[dict[str, Any]] = []
        self.result: PaymentResult | None = None
        self.error: Exception | None = None

    async def create_payment(self, **kwargs: Any) -> PaymentResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _result(*, pay_url: str) -> PaymentResult:
    return PaymentResult(
        payment_id="pay_1",
        pay_url=pay_url,
        order_id="topup-42-abc",
        amount="100.00",
        status="pending",
    )


def _service(client: FakeClient, api_key: str | None = "rp_key") -> RollyPayTopupService:
    return RollyPayTopupService(
        api_key,
        client_factory=lambda _key: cast("RollyPayClient", client),
    )


# ---------------------------------------------------------------------------
# The two-key gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("api_key", "secret", "expected"),
    [
        ("rp_key", "rp_secret", True),
        # Half-configured: the money-taking half without the
        # money-crediting half. Must render as unavailable.
        ("rp_key", None, False),
        (None, "rp_secret", False),
        (None, None, False),
    ],
)
def test_service_available_only_when_both_secrets_present(
    api_key: str | None, secret: str | None, expected: bool
) -> None:
    service = build_rollypay_topup_service(settings=_settings(api_key=api_key, secret=secret))
    assert service.available() is expected


@pytest.mark.asyncio
async def test_half_configured_service_mints_nothing() -> None:
    """Not merely a dead button — no payment may be created either."""
    service = build_rollypay_topup_service(settings=_settings(api_key="rp_key", secret=None))
    payment = await service.create_payment(user_id=42, amount_rub=100)
    assert payment.outcome is TopupInvoiceOutcome.NOT_CONFIGURED
    assert payment.pay_url is None


# ---------------------------------------------------------------------------
# Quote formula
# ---------------------------------------------------------------------------


def test_quote_coins_lands_round_at_the_offline_anchor() -> None:
    # 100 ₽ at 90 ₽/$ × 900 coins/$ = 1000 🪙, the product ratio the
    # RUB_AMOUNTS table was chosen against.
    assert quote_coins(100) == 1000
    assert quote_coins(100, FALLBACK_USD_TO_RUB) == 1000


def test_quote_coins_follows_the_fix() -> None:
    # The coin is priced in dollars; a rouble that buys half as many
    # dollars must buy half as many coins (R11 / T-020).
    assert quote_coins(100, 180.0) == 500
    assert quote_coins(100, 45.0) == 2000


@pytest.mark.parametrize("rub", RUB_AMOUNTS)
def test_every_offered_amount_quotes_a_positive_credit(rub: int) -> None:
    assert quote_coins(rub) > 0
    # Sanity on the anchor: coins scale linearly with roubles.
    assert quote_coins(rub) == int(rub * COINS_PER_USD / FALLBACK_USD_TO_RUB)


# ---------------------------------------------------------------------------
# order_id
# ---------------------------------------------------------------------------


def test_order_id_is_fresh_per_attempt() -> None:
    # Reusing an id across clicks would strand the second click behind
    # the first (expired) payment page.
    assert build_order_id(42) != build_order_id(42)
    assert build_order_id(42).startswith("topup-42-")


# ---------------------------------------------------------------------------
# create_payment outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_payment_sends_two_decimals_and_the_user_id() -> None:
    client = FakeClient("rp_key")
    client.result = _result(pay_url="https://pay.test/1")
    payment = await _service(client).create_payment(user_id=42, amount_rub=250)

    assert payment.outcome is TopupInvoiceOutcome.CREATED
    assert payment.pay_url == "https://pay.test/1"
    sent = client.calls[0]
    assert sent["amount"] == "250.00"
    # The user_id is the whole point of the metadata — without it the
    # webhook has nobody to credit.
    assert sent["user_id"] == 42
    assert sent["order_id"].startswith("topup-42-")


@pytest.mark.asyncio
async def test_create_payment_without_key_is_degraded_not_an_error() -> None:
    client = FakeClient("unused")
    payment = await _service(client, api_key=None).create_payment(user_id=42, amount_rub=100)
    assert payment.outcome is TopupInvoiceOutcome.NOT_CONFIGURED
    assert client.calls == []


@pytest.mark.asyncio
async def test_create_payment_swallows_provider_errors() -> None:
    client = FakeClient("rp_key")
    client.error = RollyPayError("provider is down")
    payment = await _service(client).create_payment(user_id=42, amount_rub=100)
    assert payment.outcome is TopupInvoiceOutcome.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pay_url",
    [
        # A 200 with no pay_url is a provider contract violation;
        # rendering a card with no button would be worse than failing.
        "",
        "http://pay.test/1",
        # The one that is not merely broken: this string lands on an
        # inline button's ``url``, where a deep link would be tapped in
        # the belief that it is a payment page.
        "tg://resolve?domain=someone",
        "javascript:alert(1)",
    ],
)
async def test_create_payment_refuses_a_pay_url_that_is_not_https(pay_url: str) -> None:
    client = FakeClient("rp_key")
    client.result = _result(pay_url=pay_url)
    payment = await _service(client).create_payment(user_id=42, amount_rub=100)
    assert payment.outcome is TopupInvoiceOutcome.FAILED
    assert payment.pay_url is None
