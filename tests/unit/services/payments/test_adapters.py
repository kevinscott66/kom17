"""Unit tests for the three payment-provider adapters (T-025).

Adapters are pure (verify, parse) functions — no DB, no network.
Stripe and YooKassa SDK calls are stubbed via ``sys.modules`` so we
never make real HTTP requests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import types
from decimal import Decimal
from typing import Any

import pytest

from telegram_invite_bot.services.payments import (
    CryptoAdapter,
    StripeAdapter,
    YooKassaAdapter,
)
from telegram_invite_bot.services.payments.base import (
    ParsedEvent,
    Provider,
    UncreditedCause,
    UncreditedPayment,
    fiat_or_none,
)
from telegram_invite_bot.services.payments.rates import (
    COINS_PER_USD,
    FALLBACK_USD_TO_RUB,
)

# ---------------------------------------------------------------------------
# Crypto Pay
# ---------------------------------------------------------------------------


_CRYPTO_TOKEN = "test-token-1"


def _crypto_sign(body: bytes, token: str = _CRYPTO_TOKEN) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _crypto_body(
    amount: str = "2.0",
    *,
    asset: str = "USDT",
    paid_usd_rate: str | None = "1.0",
    fiat: str | None = None,
) -> bytes:
    """One paid-invoice body, in either of the two denominations.

    Every crypto case in this file used to run at ``USDT`` and
    ``paid_usd_rate="1.0"`` — the single configuration in which the
    asset→USD conversion is the identity, so #1236 could delete the
    multiplication outright and the suite stayed green. ``fiat`` selects
    the ``currency_type="fiat"`` shape #1232 mints; ``paid_usd_rate=None``
    omits the key entirely, which is what #1233 turns on.
    """
    invoice: dict[str, Any] = {
        "invoice_id": "INV-X",
        "payload": "111",
        "amount": amount,
    }
    if paid_usd_rate is not None:
        invoice["paid_usd_rate"] = paid_usd_rate
    if fiat is not None:
        invoice["currency_type"] = "fiat"
        invoice["fiat"] = fiat
        invoice["paid_asset"] = asset
    else:
        invoice["asset"] = asset
    return json.dumps({"update_type": "invoice_paid", "payload": invoice}).encode()


def test_crypto_verify_signature_accepts_correct_sig() -> None:
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    body = _crypto_body()
    assert adapter.verify_signature({"Crypto-Pay-API-Signature": _crypto_sign(body)}, body)


def test_crypto_verify_signature_accepts_lowercase_header() -> None:
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    body = _crypto_body()
    assert adapter.verify_signature({"crypto-pay-api-signature": _crypto_sign(body)}, body)


def test_crypto_verify_signature_rejects_wrong_sig() -> None:
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    body = _crypto_body()
    assert not adapter.verify_signature({"crypto-pay-api-signature": "deadbeef" * 8}, body)


def test_crypto_verify_signature_rejects_missing_header() -> None:
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    assert not adapter.verify_signature({}, _crypto_body())


def test_crypto_verify_signature_rejects_empty_token() -> None:
    adapter = CryptoAdapter("")
    body = _crypto_body()
    assert not adapter.verify_signature({"crypto-pay-api-signature": _crypto_sign(body, "")}, body)


def test_crypto_verify_signature_rejects_non_ascii_header() -> None:
    """#916: a high byte must return False, not raise.

    Starlette decodes header values with latin-1, so any byte in
    0x80-0xFF reaches the adapter as a non-ASCII ``str`` — and
    ``hmac.compare_digest`` raises ``TypeError`` on those. Nothing
    registers an ``Exception`` handler on the webhook app, so that
    used to leave the route as a 500 with a full traceback.
    """
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    body = _crypto_body()
    assert not adapter.verify_signature({"crypto-pay-api-signature": "\xff" * 8}, body)


def test_crypto_parse_event_extracts_user_and_coins() -> None:
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    event = adapter.parse_event(_crypto_body("2.0"))
    assert event is not None
    assert event.provider is Provider.CRYPTO
    assert event.external_id == "INV-X"
    assert event.user_id == 111
    # 2.0 USD * 900 = 1800 coins
    assert event.coins == 1800


def test_crypto_parse_event_refuses_a_negative_user_id() -> None:
    """#1982. A Telegram chat id below zero is a group, never a payer.

    ``yookassa`` and ``rollypay`` have refused this since #1698 with an
    explicit ``user_id <= 0``; this adapter tested ``not user_id``,
    which is false for ``-100…`` and so let it through. Downstream
    ``EconomyRepo.get_or_create`` does not check the sign either, so
    the coins land in a wallet keyed to a supergroup and the payer
    gets nothing — with no alert, because the credit succeeded.
    """
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    invoice = {
        "invoice_id": "INV-NEG",
        "payload": "-1001234567890",
        "amount": "2.0",
        "asset": "USDT",
        "paid_usd_rate": "1.0",
    }
    body = json.dumps({"update_type": "invoice_paid", "payload": invoice}).encode()

    assert adapter.parse_event(body) is None


def test_crypto_parse_event_returns_none_on_non_invoice_paid() -> None:
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    body = json.dumps({"update_type": "invoice_expired", "payload": {}}).encode()
    assert adapter.parse_event(body) is None


def test_crypto_parse_event_returns_none_on_malformed_body() -> None:
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    assert adapter.parse_event(b"not json") is None
    assert adapter.parse_event(b"[]") is None


def test_crypto_parse_event_returns_none_on_zero_amount() -> None:
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    assert adapter.parse_event(_crypto_body("0")) is None


def test_crypto_parse_event_floors_fractional_coins() -> None:
    """M-E-1: rounding policy is floor (legacy parity).

    ``amount=0.5 USD`` at the documented ``COINS_PER_USD=900`` rate
    is exactly ``450`` coins — boundary value pins that floor and
    ceil agree at integer-valued products. ``amount=0.5005 USD``
    yields ``450.45``, which floor truncates to ``450`` (ceil would
    have produced ``451``) — the discriminating boundary that
    catches an accidental swap of ``math.floor`` ↔ ``math.ceil``.
    """
    adapter = CryptoAdapter(_CRYPTO_TOKEN)

    # 0.5 * 900 = 450.0 — both floor and ceil agree.
    event = adapter.parse_event(_crypto_body("0.5"))
    assert event is not None
    assert event.coins == 450

    # 0.5005 * 900 = 450.45 — floor=450, ceil=451. Pinning floor.
    event = adapter.parse_event(_crypto_body("0.5005"))
    assert event is not None
    assert event.coins == 450


def test_crypto_parse_event_converts_a_crypto_invoice_at_the_quoted_rate() -> None:
    """#1236: the conversion the old fixtures could not see.

    0.05 BTC at $104 000 is $5 200, i.e. 4 680 000 coins. Under the
    all-USDT fixtures the multiplication was the identity, so nothing
    in the suite distinguished ``amount * rate`` from ``amount``.
    """
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    event = adapter.parse_event(_crypto_body("0.05", asset="BTC", paid_usd_rate="104000"))
    assert event is not None
    assert event.coins == 4_680_000
    assert event.fiat_currency == "BTC"


def test_crypto_parse_event_refuses_a_non_stablecoin_without_a_usable_rate() -> None:
    """#1233: ``float(raw or 1)`` used to read a missing rate as parity.

    The payer moved 0.05 BTC. Crediting that as five cents — 45 coins —
    is not a conservative default, it is a silent loss the payer has no
    way to appeal. Refusing surfaces it to the owner instead, through
    the ``describes_paid_money`` alert that already exists.
    """
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    for rate in (None, "0", "0.0", "", "-3", "nan"):
        body = _crypto_body("0.05", asset="BTC", paid_usd_rate=rate)
        assert adapter.parse_event(body) is None, rate
    # The alert path still reports it as money that moved.
    assert CryptoAdapter.describes_paid_money(_crypto_body("0.05", asset="BTC", paid_usd_rate=None))


def test_crypto_parse_event_still_grants_a_stablecoin_parity() -> None:
    """The half of the old fallback that was actually correct.

    A dollar stablecoin IS a dollar to within rounding, so a rate the
    provider omitted is no reason to refuse one — and refusing would
    have regressed the only leg live in production.
    """
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    for asset in ("USDT", "USDC"):
        event = adapter.parse_event(_crypto_body("2.0", asset=asset, paid_usd_rate=None))
        assert event is not None, asset
        assert event.coins == 2 * COINS_PER_USD


def test_crypto_parse_event_reads_a_fiat_invoice_amount_as_dollars() -> None:
    """#1232's inbound half, and the reason it could not ship alone.

    On a ``currency_type="fiat"`` invoice ``amount`` is already the USD
    figure and ``paid_usd_rate`` describes whatever coin the payer
    settled in. Multiplying them — which is what the crypto branch does
    — would turn this $5 top-up into $521 177 of coins.
    """
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    event = adapter.parse_event(
        _crypto_body("5", asset="BTC", paid_usd_rate="104235.5", fiat="USD")
    )
    assert event is not None
    assert event.coins == 5 * COINS_PER_USD
    assert event.fiat_currency == "USD"
    # No conversion was applied, so no rate is recorded — a ``1`` here
    # would claim a parity nobody quoted.
    assert event.fx_rate is None


def test_crypto_parse_event_refuses_a_fiat_invoice_in_another_currency() -> None:
    """This bot mints dollars, so a euro invoice is not one of ours.

    Reading it as dollars would misprice the credit by the whole cross
    rate, in the payer's favour or the owner's depending on the day.
    """
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    assert adapter.parse_event(_crypto_body("5", fiat="EUR")) is None
    assert adapter.parse_event(_crypto_body("5", fiat="")) is None


def _crypto_body_dict(**overrides: Any) -> bytes:
    """``_crypto_body`` with top-level keys replaced or added.

    ``payload`` is merged rather than replaced, so a case that only
    cares about ``status`` does not have to restate the whole invoice.
    """
    data: dict[str, Any] = json.loads(_crypto_body())
    payload = overrides.pop("payload", None)
    if payload is not None:
        data["payload"].update(payload)
    data.update(overrides)
    return json.dumps(data).encode()


def test_crypto_describes_paid_money_accepts_a_paid_invoice() -> None:
    """#1183: the body ``parse_event`` credits is money that moved."""
    assert CryptoAdapter.describes_paid_money(_crypto_body()) is True


def test_crypto_describes_paid_money_accepts_an_unlisted_paid_update() -> None:
    """#227's asymmetry, ported to Crypto Pay.

    ``parse_event`` is strict on ``update_type`` on purpose — an update
    shape we have never seen must not mint coins. This predicate is
    deliberately the opposite: an update Crypto Pay adds tomorrow,
    arriving with a paid invoice under it, must be refused a credit
    *and* still be reported as money that moved. Silence is the more
    expensive failure once the payer has already been debited.
    """
    body = _crypto_body_dict(update_type="invoice_settled_v2", payload={"status": "paid"})
    assert CryptoAdapter.describes_paid_money(body) is True


def test_crypto_describes_paid_money_refuses_a_lifecycle_update() -> None:
    """Negative control, and the one that decides whether this is usable.

    ``invoice_expired`` and its siblings arrive for every abandoned
    checkout. An alert that fires on them is an alert the owner mutes
    inside a day.
    """
    body = _crypto_body_dict(update_type="invoice_expired")
    assert CryptoAdapter.describes_paid_money(body) is False


def test_crypto_describes_paid_money_refuses_an_unreadable_body() -> None:
    """A body that is not an object cannot claim a payment happened."""
    assert CryptoAdapter.describes_paid_money(b"not json") is False
    assert CryptoAdapter.describes_paid_money(b"[]") is False
    assert CryptoAdapter.describes_paid_money(b"") is False


def test_crypto_describes_paid_money_refuses_a_zero_amount() -> None:
    """Zero is a readable amount, and it is not money owed to anyone."""
    assert CryptoAdapter.describes_paid_money(_crypto_body("0")) is False


def test_crypto_describes_paid_money_reports_a_sub_coin_invoice() -> None:
    """The refusal ``parse_event`` makes most quietly (``coins <= 0``).

    ``0.0005 USD * 900 = 0.45`` floors to zero coins, so the invoice is
    dropped — but the payer really did send 0.0005 USDT. Small is not
    the same as nothing.
    """
    assert CryptoAdapter.describes_paid_money(_crypto_body("0.0005")) is True


def test_crypto_describes_paid_money_reports_an_amount_it_cannot_read() -> None:
    """An amount we cannot parse is not an amount we may call zero.

    Both unreadable cases resolve the same way — a paid update with no
    invoice under it at all, and one whose amount is not a number.
    Guessing "probably nothing" here is guessing in the direction that
    loses a real payment silently.
    """
    assert CryptoAdapter.describes_paid_money(_crypto_body("сколько-то")) is True
    assert CryptoAdapter.describes_paid_money(b'{"update_type": "invoice_paid"}') is True


# ---------------------------------------------------------------------------
# YooKassa
# ---------------------------------------------------------------------------


def _install_fake_yookassa(
    monkeypatch: pytest.MonkeyPatch,
    *,
    succeeded: bool,
    rub: str = "499.00",
    currency: str = "RUB",
    user_id: str | None = "42",
    coins: str = "100",
) -> None:
    """Stand in for ``Payment.find_one``.

    Since T-020 R11-b the adapter credits off *this* object, not off
    the webhook body, so the fake has to carry the amount and metadata
    the real SDK returns — an ``Amount`` model with ``.value`` /
    ``.currency`` plus a plain-dict ``metadata``. Tests that want the
    body and the authoritative record to disagree simply pass
    different values here than they put in the body.
    """
    fake = types.ModuleType("yookassa")

    class _Configuration:
        account_id: str = ""
        secret_key: str = ""

    amount_obj = types.SimpleNamespace(value=rub, currency=currency)
    meta = {"coins": coins} | ({} if user_id is None else {"user_id": user_id})

    class _Payment:
        status = "succeeded" if succeeded else "pending"
        amount = amount_obj
        metadata = meta

        @classmethod
        def find_one(cls, payment_id: str) -> _Payment:
            return cls()

    fake.Configuration = _Configuration  # type: ignore[attr-defined]
    fake.Payment = _Payment  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yookassa", fake)


def _yk_body() -> bytes:
    return json.dumps(
        {
            "event": "payment.succeeded",
            "object": {
                "id": "PAY-Y",
                "status": "succeeded",
                "amount": {"value": "499.00", "currency": "RUB"},
                "metadata": {"user_id": "42", "coins": "100"},
            },
        }
    ).encode()


def test_yookassa_verify_gates_on_credentials() -> None:
    assert YooKassaAdapter("shop", "secret").verify_signature({}, b"") is True
    assert YooKassaAdapter("", "secret").verify_signature({}, b"") is False
    assert YooKassaAdapter("shop", "").verify_signature({}, b"") is False


def test_yookassa_parse_event_reverify_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_yookassa(monkeypatch, succeeded=True)
    event = YooKassaAdapter("shop", "secret").parse_event(_yk_body())
    assert isinstance(event, ParsedEvent)
    assert event.provider is Provider.YOOKASSA
    assert event.external_id == "PAY-Y"
    assert event.user_id == 42
    # R-FIX-003: coins derived from amount (499 RUB * 10 coins/RUB),
    # not from metadata.coins (which still reads 100 in the fixture
    # for backward compat with the unrelated parse assertions).
    assert event.coins == 4990


def test_yookassa_reverify_miss_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spoofed-webhook defense answers ``None``, never a card.

    #1643. This is the commonest thing an anonymous POST provokes: a
    body claiming success that ЮKassa does not confirm. Nothing was
    paid, so nobody is owed a coin and the owner must not be told
    otherwise.
    """
    _install_fake_yookassa(monkeypatch, succeeded=False)
    assert YooKassaAdapter("shop", "secret").parse_event(_yk_body()) is None


def test_yookassa_parse_event_reverify_mismatch_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_yookassa(monkeypatch, succeeded=False)
    assert YooKassaAdapter("shop", "secret").parse_event(_yk_body()) is None


@pytest.mark.parametrize(
    ("amount_str", "expected_coins"),
    [
        ("0.10", 1),
        ("99.99", 999),
        ("100.00", 1000),
        ("1.23", 12),
    ],
)
def test_yookassa_parse_event_uses_decimal_math(
    monkeypatch: pytest.MonkeyPatch, amount_str: str, expected_coins: int
) -> None:
    """R-FIX-003-fp: small-magnitude amounts must NOT lose a coin to
    IEEE-754 rounding. ``Decimal(amount_str) * Decimal(10)`` is exact;
    ``float(amount_str) * 10`` is environment-dependent for any value
    whose decimal expansion isn't a finite binary fraction (e.g.
    ``0.1``). Pin the four legacy /buy SKUs at the parser surface."""
    _install_fake_yookassa(monkeypatch, succeeded=True, rub=amount_str, coins=str(expected_coins))
    body = json.dumps(
        {
            "event": "payment.succeeded",
            "object": {
                "id": "PAY-Y",
                "status": "succeeded",
                "amount": {"value": amount_str, "currency": "RUB"},
                "metadata": {"user_id": "42", "coins": str(expected_coins)},
            },
        }
    ).encode()
    event = YooKassaAdapter("shop", "secret").parse_event(body)
    assert isinstance(event, ParsedEvent)
    assert event.coins == expected_coins


def _yk_body_amount(amount_str: str) -> bytes:
    # ``coins`` is deliberately wrong. Every YooKassa payment this bot
    # can be notified about was created with both ``user_id`` and
    # ``coins`` in its metadata (bot.py:18435, bot.py:18580 — the port
    # has no YooKassa checkout of its own), and #228 makes the adapter
    # refuse a body missing either before it spends a merchant-API
    # round-trip on it. Carrying a bogus value rather than none keeps
    # these bodies shaped like the real thing *and* keeps proving the
    # R11-b property: the credit comes off the reverified record, so
    # what the body claims here never reaches the wallet.
    return json.dumps(
        {
            "event": "payment.succeeded",
            "object": {
                "id": "PAY-Y",
                "status": "succeeded",
                "amount": {"value": amount_str, "currency": "RUB"},
                "metadata": {"user_id": "42", "coins": "1"},
            },
        }
    ).encode()


@pytest.mark.parametrize(
    ("usd_to_rub", "expected_coins"),
    [
        (90.0, 4990),  # the historic anchor — byte-identical to pre-R11
        (75.0, 5988),  # strong rouble: a rouble buys more coins
        (120.0, 3742),  # weak rouble: a rouble buys fewer
    ],
)
def test_yookassa_prices_roubles_through_the_usd_anchor(
    monkeypatch: pytest.MonkeyPatch, usd_to_rub: float, expected_coins: int
) -> None:
    """T-020 R11: the credited amount tracks the live fix.

    499 RUB is worth a different number of coins depending on what a
    rouble is worth in dollars, because a coin has one price and it is
    denominated in dollars.
    """
    _install_fake_yookassa(monkeypatch, succeeded=True)
    adapter = YooKassaAdapter("shop", "secret", usd_to_rub=usd_to_rub)
    event = adapter.parse_event(_yk_body_amount("499.00"))
    assert isinstance(event, ParsedEvent)
    assert event.coins == expected_coins


def test_yookassa_defaults_to_the_offline_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adapter built without a rate credits exactly what it always
    did — the FX plumbing is additive, never a precondition."""
    _install_fake_yookassa(monkeypatch, succeeded=True)
    default = YooKassaAdapter("shop", "secret").parse_event(_yk_body_amount("499.00"))
    pinned = YooKassaAdapter("shop", "secret", usd_to_rub=FALLBACK_USD_TO_RUB).parse_event(
        _yk_body_amount("499.00")
    )
    assert isinstance(default, ParsedEvent) and isinstance(pinned, ParsedEvent)
    assert default.coins == pinned.coins == 4990


def test_yookassa_weak_rouble_no_longer_undercuts_the_withdraw_desk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hole R11 closes, at the adapter surface.

    At USD/RUB = 120 a payer spends one dollar's worth of roubles. The
    frozen rate credited 1 200 coins for it; /withdraw redeems at
    ``COINS_PER_USD = 900``, so that dollar came back out as $1.33 —
    unbounded, risk-free, and paid by the bot's owner.
    """
    _install_fake_yookassa(monkeypatch, succeeded=True, rub="120.00", coins="1200")
    body = _yk_body_amount("120.00")  # exactly $1 at USD/RUB = 120

    frozen = YooKassaAdapter("shop", "secret").parse_event(body)
    live = YooKassaAdapter("shop", "secret", usd_to_rub=120.0).parse_event(body)

    assert isinstance(frozen, ParsedEvent) and isinstance(live, ParsedEvent)
    assert frozen.coins == 1200  # 33% more than a dollar buys
    assert live.coins == COINS_PER_USD  # a dollar in, a dollar out


@pytest.mark.parametrize("bad_fix", [0.0, -1.0, 1.0, float("nan"), 1e9])
def test_yookassa_clamps_a_garbage_fx_quote(
    monkeypatch: pytest.MonkeyPatch, bad_fix: float
) -> None:
    """The adapter re-checks the rate even though the router already
    did — this number is about to mint coins, so it gets two guards."""
    _install_fake_yookassa(monkeypatch, succeeded=True)
    event = YooKassaAdapter("shop", "secret", usd_to_rub=bad_fix).parse_event(
        _yk_body_amount("499.00")
    )
    assert isinstance(event, ParsedEvent)
    assert event.coins == 4990  # the offline anchor, not a divide-by-zero


# ---------------------------------------------------------------------------
# T-020 R11-b — the body is a notification, not evidence.
# ---------------------------------------------------------------------------


def test_yookassa_ignores_an_inflated_amount_in_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mint-your-own-balance hole.

    YooKassa webhooks are unsigned, so anyone who knows a real payment
    id — its own payer, first of all — can POST the notification
    themselves with the amount rewritten. Reverify proves *a* payment
    succeeded; it says nothing about the numbers in the body. Credit
    must come off the reverified record.
    """
    _install_fake_yookassa(monkeypatch, succeeded=True, rub="100.00")
    event = YooKassaAdapter("shop", "secret").parse_event(_yk_body_amount("1000000.00"))
    assert isinstance(event, ParsedEvent)
    assert event.coins == 1000  # 100 RUB, not ten million


def test_yookassa_ignores_a_redirected_user_id_in_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same hole, other field: the body must not choose the recipient.

    Both metadata fields are present and both are lies — #228 checks
    only that they *exist*, so a well-shaped forgery still gets past
    the cheap gate and has to be defeated where it matters, on the
    reverified record.
    """
    _install_fake_yookassa(monkeypatch, succeeded=True, user_id="42")
    body = json.dumps(
        {
            "event": "payment.succeeded",
            "object": {
                "id": "PAY-Y",
                "status": "succeeded",
                "amount": {"value": "499.00", "currency": "RUB"},
                "metadata": {"user_id": "9999", "coins": "9999"},
            },
        }
    ).encode()
    event = YooKassaAdapter("shop", "secret").parse_event(body)
    assert isinstance(event, ParsedEvent)
    assert event.user_id == 42


def test_yookassa_refuses_a_payment_with_no_reverified_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No authoritative recipient means no credit — not a body fallback.

    #1643: refusing is only half. ЮKassa confirmed this payment, so the
    money is on the merchant account and the payer has nothing — the
    refusal has to be reported as such, not spelled ``None`` like a
    notification that was never money. ``payer`` is empty on purpose
    here: the missing id is the whole finding.
    """
    _install_fake_yookassa(monkeypatch, succeeded=True, user_id=None)
    event = YooKassaAdapter("shop", "secret").parse_event(_yk_body())
    assert isinstance(event, UncreditedPayment)
    assert event.cause is UncreditedCause.NO_USER_ID
    assert event.provider is Provider.YOOKASSA
    assert event.external_id == "PAY-Y"
    assert event.payer == ""
    assert event.amount == "499.00 RUB"


def test_yookassa_refuses_a_non_rouble_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``coins_for_rub`` would price 499 USD as 499 RUB — a 90x discount."""
    _install_fake_yookassa(monkeypatch, succeeded=True, currency="USD")
    event = YooKassaAdapter("shop", "secret").parse_event(_yk_body())
    assert isinstance(event, UncreditedPayment)
    assert event.cause is UncreditedCause.NOT_RUB
    # The recipient IS known on this branch, and printing it is what
    # lets the owner credit by hand without opening the dashboard.
    assert event.payer == "42"
    assert event.amount == "499.00 USD"


@pytest.mark.parametrize("bad_amount", ["NaN", "Infinity", "-Infinity", "0", "-5.00", ""])
def test_yookassa_refuses_a_non_finite_or_empty_amount(
    monkeypatch: pytest.MonkeyPatch, bad_amount: str
) -> None:
    """``Decimal`` accepts NaN/Infinity without raising; NaN then raises
    on comparison and Infinity raises at ``int()``. Both are rejected
    before they reach the arithmetic."""
    _install_fake_yookassa(monkeypatch, succeeded=True, rub=bad_amount)
    event = YooKassaAdapter("shop", "secret").parse_event(_yk_body())
    assert isinstance(event, UncreditedPayment)
    assert event.cause is UncreditedCause.BAD_AMOUNT
    assert event.payer == "42"


def test_yookassa_refuses_unreadable_reverified_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``user_id`` that is not a number — same class, earliest gate.

    #1643. This is the one post-reverify refusal that fires before the
    amount is parsed, which is why the alert reads the sum off the
    payment object separately instead of off a local variable.
    """
    _install_fake_yookassa(monkeypatch, succeeded=True, user_id="not-a-number")
    event = YooKassaAdapter("shop", "secret").parse_event(_yk_body())
    assert isinstance(event, UncreditedPayment)
    assert event.cause is UncreditedCause.BAD_METADATA
    assert event.amount == "499.00 RUB"


def test_yookassa_refuses_an_amount_under_one_coin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-coin top-up: real money in, nothing to credit.

    #1643. This branch used to log nothing at all — the only silent
    money-losing refusal in the method — while the card it now raises
    ends by sending the owner to the journal.
    """
    _install_fake_yookassa(monkeypatch, succeeded=True, rub="0.01")
    event = YooKassaAdapter("shop", "secret").parse_event(_yk_body())
    assert isinstance(event, UncreditedPayment)
    assert event.cause is UncreditedCause.BELOW_ONE_COIN
    assert event.payer == "42"
    assert event.amount == "0.01 RUB"


def test_yookassa_body_only_refusals_stay_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The union's other half, pinned (#1643).

    Every refusal decided from the unsigned body must stay ``None``.
    An ``UncreditedPayment`` here would hand a stranger with a POST the
    ability to ring the owner's phone — the exact objection that kept
    ``describes_paid_money`` off this adapter (#1611).
    """
    _install_fake_yookassa(monkeypatch, succeeded=True)
    adapter = YooKassaAdapter("shop", "secret")
    assert adapter.parse_event(b"not json") is None
    assert adapter.parse_event(json.dumps([1, 2]).encode()) is None
    assert adapter.parse_event(json.dumps({"event": "refund.succeeded"}).encode()) is None
    assert adapter.parse_event(json.dumps({"event": "payment.waiting"}).encode()) is None


def test_yookassa_reads_a_dict_shaped_sdk_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK returns models, but some versions hand back plain dicts.

    Reading only the attribute shape would make a dict-shaped response
    look like "no amount" and silently drop real payments.
    """
    fake = types.ModuleType("yookassa")

    class _Configuration:
        account_id: str = ""
        secret_key: str = ""

    class _Payment:
        status = "succeeded"
        amount = {"value": "499.00", "currency": "RUB"}
        metadata = {"user_id": "42"}

        @classmethod
        def find_one(cls, payment_id: str) -> _Payment:
            return cls()

    fake.Configuration = _Configuration  # type: ignore[attr-defined]
    fake.Payment = _Payment  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yookassa", fake)

    event = YooKassaAdapter("shop", "secret").parse_event(_yk_body())
    assert isinstance(event, ParsedEvent)
    assert event.user_id == 42
    assert event.coins == 4990


def test_yookassa_parse_event_returns_none_on_non_succeeded_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_yookassa(monkeypatch, succeeded=True)
    body = json.dumps(
        {
            "event": "payment.canceled",
            "object": {
                "id": "PAY-Y",
                "status": "canceled",
                "metadata": {"user_id": "42", "coins": "100"},
            },
        }
    ).encode()
    assert YooKassaAdapter("shop", "secret").parse_event(body) is None


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------


def _install_fake_stripe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    valid: bool,
    event_payload: dict[str, Any] | None = None,
) -> None:
    fake = types.ModuleType("stripe")

    class _Webhook:
        @staticmethod
        def construct_event(body: bytes, sig: str, secret: str) -> dict[str, Any]:
            if not valid:
                raise ValueError("invalid signature")
            assert event_payload is not None
            return event_payload

    fake.Webhook = _Webhook  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "stripe", fake)


def _stripe_event() -> dict[str, Any]:
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_X",
                "amount_total": 1999,
                "metadata": {"user_id": "55", "coins": "250"},
            }
        },
    }


def test_stripe_verify_signature_accepts_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_stripe(monkeypatch, valid=True, event_payload=_stripe_event())
    adapter = StripeAdapter("whsec_test")
    assert adapter.verify_signature({"Stripe-Signature": "t=1,v1=a"}, b"{}")


def test_stripe_verify_signature_rejects_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_stripe(monkeypatch, valid=False)
    adapter = StripeAdapter("whsec_test")
    assert not adapter.verify_signature({"Stripe-Signature": "bogus"}, b"{}")


def test_stripe_verify_signature_rejects_missing_secret() -> None:
    assert not StripeAdapter("").verify_signature({"Stripe-Signature": "x"}, b"{}")


def test_stripe_verify_signature_rejects_missing_header() -> None:
    assert not StripeAdapter("whsec_test").verify_signature({}, b"{}")


def test_stripe_parse_event_extracts_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_stripe(monkeypatch, valid=True, event_payload=_stripe_event())
    adapter = StripeAdapter("whsec_test")
    body = b"{}"
    assert adapter.verify_signature({"Stripe-Signature": "t=1,v1=a"}, body)
    event = adapter.parse_event(body)
    assert event is not None
    assert event.provider is Provider.STRIPE
    assert event.external_id == "cs_test_X"
    assert event.user_id == 55
    # R-FIX-003: coins derived from amount_total (1999 cents * 900
    # coins/USD // 100 cents/USD == 17991), not from metadata.coins
    # (which still reads 250 in the fixture).
    assert event.coins == 17991


def test_stripe_parse_event_refuses_a_negative_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1982, the operator-error half.

    There is no in-bot checkout for Stripe — the owner creates the
    session in the dashboard and types ``metadata.user_id`` by hand, so
    pasting a group id is a plausible slip rather than an attack. The
    guard here read ``not (user_id and session_id)``, which a negative
    id satisfies, and the money went to a wallet nobody owns.
    """
    payload = _stripe_event()
    payload["data"]["object"]["metadata"]["user_id"] = "-1001234567890"
    _install_fake_stripe(monkeypatch, valid=True, event_payload=payload)
    adapter = StripeAdapter("whsec_test")
    body = b"{}"
    assert adapter.verify_signature({"Stripe-Signature": "t=1,v1=a"}, body)

    assert adapter.parse_event(body) is None


def test_stripe_parse_event_returns_none_on_non_checkout_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _stripe_event()
    payload["type"] = "payment_intent.created"
    _install_fake_stripe(monkeypatch, valid=True, event_payload=payload)
    adapter = StripeAdapter("whsec_test")
    body = b"{}"
    adapter.verify_signature({"Stripe-Signature": "x"}, body)
    assert adapter.parse_event(body) is None


def test_stripe_parse_event_without_prior_verify_returns_none() -> None:
    """The adapter caches the parsed event on verify_signature. Skipping
    verify and calling parse_event directly must NOT credit — that path
    means the router has a bug, and we fail-closed."""
    adapter = StripeAdapter("whsec_test")
    assert adapter.parse_event(b"{}") is None


def test_stripe_refuses_a_non_usd_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """``coins_for_usd_cents`` is USD in name only — the arithmetic has
    no idea what the minor units are. 1999 RUB minor units priced as
    cents would sell 17 991 coins for about twenty dollars' worth of
    roubles."""
    payload = _stripe_event()
    payload["data"]["object"]["currency"] = "rub"
    _install_fake_stripe(monkeypatch, valid=True, event_payload=payload)
    adapter = StripeAdapter("whsec_test")
    body = b"{}"
    assert adapter.verify_signature({"Stripe-Signature": "x"}, body)
    assert adapter.parse_event(body) is None


def test_stripe_cache_is_keyed_by_content_not_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stranded cache entry must not be reachable by an unrelated body.

    The cache used to key on ``id(body)``, which CPython reuses once
    the original bytes are collected — a later request could then be
    served someone else's event. Two equal-valued but distinct bytes
    objects standing in for the same delivery is the observable half
    of that contract.
    """
    _install_fake_stripe(monkeypatch, valid=True, event_payload=_stripe_event())
    adapter = StripeAdapter("whsec_test")
    verified = b'{"a": 1}'
    assert adapter.verify_signature({"Stripe-Signature": "x"}, verified)

    # Distinct object, same bytes — this IS the same delivery.
    assert adapter.parse_event(b'{"a": ' + b"1}") is not None
    # And the entry is consumed, so a replay finds nothing cached.
    assert adapter.parse_event(verified) is None


def _stripe_delayed(status: str, *, event_type: str) -> dict[str, Any]:
    """A checkout session at one of the stages of a delayed payment."""
    payload = _stripe_event()
    payload["type"] = event_type
    payload["data"]["object"]["payment_status"] = status
    return payload


def _parse(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> object | None:
    _install_fake_stripe(monkeypatch, valid=True, event_payload=payload)
    adapter = StripeAdapter("whsec_test")
    body = b"{}"
    assert adapter.verify_signature({"Stripe-Signature": "x"}, body)
    return adapter.parse_event(body)


def test_stripe_refuses_a_completed_but_unpaid_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Checkout Session completes when the payer commits, not when the
    money lands. Pay by SEPA debit or at a convenience store and Stripe
    sends ``completed`` with ``payment_status: "unpaid"`` days before the
    funds clear — crediting there mints coins against a payment that can
    still fail, and by the time ``async_payment_failed`` arrives they are
    spent."""
    assert (
        _parse(
            monkeypatch,
            _stripe_delayed("unpaid", event_type="checkout.session.completed"),
        )
        is None
    )


def test_stripe_credits_when_the_delayed_payment_finally_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the same rule: refusing the unpaid ``completed``
    is only right if the event that says the money DID arrive is honoured,
    or a bank-transfer payer would pay and never be credited at all."""
    event = _parse(
        monkeypatch,
        _stripe_delayed("paid", event_type="checkout.session.async_payment_succeeded"),
    )
    assert event is not None
    # Same session id as the ``completed`` delivery that preceded it —
    # which is what makes the idempotency key stop a double credit.
    assert event.external_id == "cs_test_X"  # type: ignore[attr-defined]
    assert event.coins == 17991  # type: ignore[attr-defined]


def test_stripe_ignores_the_failed_half_of_a_delayed_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``async_payment_failed`` is the money NOT arriving."""
    assert (
        _parse(
            monkeypatch,
            _stripe_delayed("unpaid", event_type="checkout.session.async_payment_failed"),
        )
        is None
    )


def test_stripe_refuses_a_session_that_required_no_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully discounted session is genuinely completed and genuinely
    free. Nothing settled, so nothing to credit."""
    assert (
        _parse(
            monkeypatch,
            _stripe_delayed("no_payment_required", event_type="checkout.session.completed"),
        )
        is None
    )


def test_stripe_still_credits_a_session_that_omits_payment_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent is treated as paid, deliberately.

    Only Stripe can put a body past the HMAC, and older API versions did
    not always send the field — refusing on absence would turn a
    provider-side omission into a payer who paid and got nothing.
    """
    payload = _stripe_event()
    assert "payment_status" not in payload["data"]["object"]
    assert _parse(monkeypatch, payload) is not None


def test_stripe_refuses_to_credit_a_test_mode_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1609: a test-mode event is signed, but no money moved.

    Stripe signs test webhooks with the test secret, so the HMAC
    passes and nothing upstream of ``parse_event`` tells them apart
    from live ones. Until #1609 the ``livemode`` check lived only in
    ``describes_paid_money``, which the router reaches ONLY after
    ``parse_event`` already returned ``None`` — so on the crediting
    path there was no gate at all and a test endpoint pointed at the
    live URL was a coin faucet.
    """
    payload = _stripe_event()
    payload["livemode"] = False
    assert _parse(monkeypatch, payload) is None


def test_stripe_still_credits_an_event_that_omits_livemode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent means live — the same asymmetry ``describes_paid_money``
    already carries. Only an explicit ``false`` is a claim; refusing on
    absence would turn a provider-side omission into a payer who paid
    and got nothing.
    """
    payload = _stripe_event()
    assert "livemode" not in payload
    assert _parse(monkeypatch, payload) is not None
    payload["livemode"] = True
    assert _parse(monkeypatch, payload) is not None


def test_crypto_refuses_a_non_finite_amount() -> None:
    """``float("inf")`` clears the ``> 0`` guard and then overflows
    inside ``int()``. The body is signed, so this is provider
    malformation — but a 500 would have Crypto Pay retrying forever."""
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    assert adapter.parse_event(_crypto_body("Infinity")) is None
    assert adapter.parse_event(_crypto_body("nan")) is None


# --- #239: the fiat charge travels with the event -----------------


def test_fiat_or_none_parses_a_plain_decimal_string() -> None:
    """The happy path must be exact, not merely close.

    ``Decimal("2.50")`` and ``Decimal("2.5")`` compare equal, so the
    assertion below also pins the *string* form — that is what lands
    in the column, and a reconciliation against RollyPay's dashboard
    is a string comparison long before it is a numeric one.
    """
    parsed = fiat_or_none("2.50")
    assert parsed == Decimal("2.50")
    assert str(parsed) == "2.50"


def test_fiat_or_none_refuses_garbage_and_absence() -> None:
    assert fiat_or_none(None) is None
    assert fiat_or_none("") is None
    assert fiat_or_none("not a number") is None


def test_fiat_or_none_refuses_non_finite_values() -> None:
    """``Decimal("Infinity")`` parses happily — and lies.

    A non-finite value would render as ``Infinity RUB`` in the audit
    column and as ``inf`` in any spreadsheet that reads it back. It is
    not a figure, so it is not recorded; ``None`` says "we do not know"
    and that is the truth.
    """
    assert fiat_or_none("Infinity") is None
    assert fiat_or_none("-Infinity") is None
    assert fiat_or_none("NaN") is None


def test_crypto_parse_event_carries_the_asset_charge() -> None:
    """Crypto Pay is billed in the asset, not in the derived USD.

    ``amount_usd`` is a float and ``2.0 USDT`` at rate ``1.0`` can come
    back as ``2.0000000000000004`` — a figure that is wrong in the only
    place it matters. What the provider actually sent is the asset
    amount and the asset→USD rate, so that is what is stored.
    """
    adapter = CryptoAdapter(_CRYPTO_TOKEN)
    event = adapter.parse_event(_crypto_body("2.0"))
    assert event is not None
    assert event.fiat_amount == Decimal("2.0")
    assert event.fiat_currency == "USDT"
    assert event.fx_rate == Decimal("1.0")


def test_yookassa_parse_event_carries_the_rouble_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_yookassa(monkeypatch, succeeded=True)
    event = YooKassaAdapter("shop", "secret").parse_event(_yk_body())
    assert isinstance(event, ParsedEvent)
    assert event.fiat_amount == Decimal("499.00")
    assert event.fiat_currency == "RUB"
    # The adapter defaults to the documented anchor when no live quote
    # was handed in; pinning it here is what makes the stored rate
    # reproducible from the ticket alone.
    assert event.fx_rate == Decimal(str(FALLBACK_USD_TO_RUB))


def test_stripe_parse_event_carries_the_charge_without_a_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stripe prices in the currency coins are already denominated in.

    There is no conversion, so there is no rate — and ``None`` here is
    a statement ("no conversion happened"), not a missing value.
    """
    _install_fake_stripe(monkeypatch, valid=True, event_payload=_stripe_event())
    adapter = StripeAdapter("whsec_test")
    body = b"{}"
    assert adapter.verify_signature({"Stripe-Signature": "t=1,v1=a"}, body)
    event = adapter.parse_event(body)
    assert event is not None
    assert event.fiat_amount == Decimal("19.99")
    assert event.fiat_currency == "USD"
    assert event.fx_rate is None


# ---------------------------------------------------------------------------
# Stripe: paid, verified, refused a credit (#1527)
# ---------------------------------------------------------------------------


def _stripe_body(**overrides: Any) -> bytes:
    """A signed-shape checkout-session body, as bytes off the wire.

    ``describes_paid_money`` reads the raw body rather than the cached
    event, so these tests hand it JSON rather than the dict the fake SDK
    returns — which is exactly what production does.
    """
    sess: dict[str, Any] = {
        "id": "cs_test_X",
        "object": "checkout.session",
        "amount_total": 1999,
        "currency": "usd",
        "payment_status": "paid",
        "metadata": {"user_id": "55"},
    }
    sess.update(overrides)
    return json.dumps({"type": "checkout.session.completed", "data": {"object": sess}}).encode()


def test_stripe_describes_paid_money_accepts_a_paid_session() -> None:
    """The plain case: the money cleared, whatever the parser then did."""
    assert StripeAdapter.describes_paid_money(_stripe_body()) is True


def test_stripe_describes_paid_money_accepts_the_two_refusals_that_lose_money() -> None:
    """The reachable refusals: a non-USD settlement and a missing payer.

    Both are a real payment on the merchant account with nobody's
    balance moved, which is the entire reason the alert exists.
    """
    assert StripeAdapter.describes_paid_money(_stripe_body(currency="rub")) is True
    assert StripeAdapter.describes_paid_money(_stripe_body(metadata={})) is True


def test_stripe_describes_paid_money_accepts_an_unlisted_session_event() -> None:
    """A session event Stripe adds tomorrow still reports money.

    The credit path is an allowlist and would refuse this; the alert
    must not mirror that allowlist or the refusal would pass in
    silence. The object names itself, which is what carries it here.
    """
    body = json.loads(_stripe_body())
    body["type"] = "checkout.session.completed_v2"
    assert StripeAdapter.describes_paid_money(json.dumps(body).encode()) is True


def test_stripe_describes_paid_money_refuses_a_dispute_closed() -> None:
    """The hazard that rules out a RollyPay-shaped exception list.

    ``charge.dispute.closed`` sits outside ``REVERSAL_EVENTS`` on
    purpose — a dispute can close *won*. A "not a reversal, therefore
    paid money" predicate would fire an uncredited alert on good news.
    """
    body = json.dumps(
        {
            "type": "charge.dispute.closed",
            "data": {"object": {"id": "dp_1", "object": "dispute", "amount": 49900}},
        }
    ).encode()
    assert StripeAdapter.describes_paid_money(body) is False


def test_stripe_describes_paid_money_refuses_ordinary_traffic() -> None:
    """Stripe delivers hundreds of event types to one endpoint."""
    for event_type in ("customer.created", "payment_intent.created", "invoice.finalized"):
        body = json.dumps({"type": event_type, "data": {"object": {"id": "x"}}}).encode()
        assert StripeAdapter.describes_paid_money(body) is False, event_type


def test_stripe_describes_paid_money_refuses_a_session_not_yet_paid() -> None:
    """A delayed-notification method commits days before it clears.

    Nobody is out any money at ``unpaid``; the credit arrives with
    ``async_payment_succeeded``, and an alert here would cry wolf on
    every SEPA debit.
    """
    assert StripeAdapter.describes_paid_money(_stripe_body(payment_status="unpaid")) is False


def test_stripe_describes_paid_money_treats_an_absent_status_as_paid() -> None:
    """Mirrors ``parse_event``: only Stripe can put a body past the HMAC."""
    body = json.loads(_stripe_body())
    del body["data"]["object"]["payment_status"]
    assert StripeAdapter.describes_paid_money(json.dumps(body).encode()) is True


def test_stripe_describes_paid_money_refuses_a_fully_discounted_session() -> None:
    """Zero cleared is not money lost."""
    assert StripeAdapter.describes_paid_money(_stripe_body(amount_total=0)) is False


def test_stripe_describes_paid_money_reports_an_amount_it_cannot_read() -> None:
    """Unreadable is not zero.

    Guessing "probably nothing" guesses in the direction that loses a
    real payment, so an amount that will not parse — and an absent one —
    resolve the other way.
    """
    assert StripeAdapter.describes_paid_money(_stripe_body(amount_total="сколько-то")) is True
    body = json.loads(_stripe_body())
    del body["data"]["object"]["amount_total"]
    assert StripeAdapter.describes_paid_money(json.dumps(body).encode()) is True


def test_stripe_describes_paid_money_refuses_a_test_mode_event() -> None:
    """Signed exactly like a live one, but nobody is out any money.

    The same call RollyPay's sandbox flag gets. Only an explicit
    ``false`` refuses — an absent ``livemode`` is not a claim.
    """
    assert StripeAdapter.describes_paid_money(_stripe_body(livemode=False)) is True
    body = json.loads(_stripe_body())
    body["livemode"] = False
    assert StripeAdapter.describes_paid_money(json.dumps(body).encode()) is False


def test_stripe_describes_paid_money_refuses_an_unreadable_body() -> None:
    assert StripeAdapter.describes_paid_money(b"not json") is False
    assert StripeAdapter.describes_paid_money(b"[]") is False
    assert StripeAdapter.describes_paid_money(b"") is False
