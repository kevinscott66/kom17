"""The single-source COM↔USD rate (T-019, R5).

``docs/ECONOMY_RATE_AUDIT.md`` §1 found the buy rate written down four
separate times. The danger was never the value — it was that a future
price change could land on three of the four sites and leave the
/topup button promising coins the webhook would not credit. These
tests pin the collapse: every consumer must resolve to the *same*
number, and the conversion helpers must keep the legacy floor policy.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from telegram_invite_bot.config.settings import WithdrawConfig
from telegram_invite_bot.services.payments.crypto_invoices import (
    COINS_PER_USD as INVOICE_RATE,
)
from telegram_invite_bot.services.payments.rates import (
    COINS_PER_USD,
    FALLBACK_USD_TO_RUB,
    MAX_PRICEABLE_AMOUNT,
    coins_for_rub,
    coins_for_usd,
    coins_for_usd_cents,
    is_priceable,
    rub_per_coin,
    sane_usd_to_rub,
)


def test_every_usd_denominated_site_reads_the_same_rate() -> None:
    """/topup button, both webhook adapters and the withdraw default
    all resolve to one number — the divergence trap is closed."""
    assert INVOICE_RATE == COINS_PER_USD
    # Withdraw only *defaults* to parity; an operator opening a spread
    # (audit R6) sets WITHDRAW_COINS_PER_USDT and this drifts on purpose.
    assert WithdrawConfig().coins_per_usdt == float(COINS_PER_USD)


@pytest.mark.parametrize(
    ("amount_usd", "expected"),
    [
        (5.0, 4_500),
        (0.5, 450),  # integer-valued product: floor and ceil agree
        (0.5005, 450),  # 450.45 → floor keeps the change with the service
        (0.0005, 0),  # sub-coin top-up credits nothing (M-E-1)
    ],
)
def test_coins_for_usd_floors(amount_usd: float, expected: int) -> None:
    assert coins_for_usd(amount_usd) == expected


@pytest.mark.parametrize(
    ("cents", "expected"),
    [(500, 4_500), (50, 450), (1, 9), (0, 0)],
)
def test_coins_for_usd_cents_stays_in_integer_math(cents: int, expected: int) -> None:
    """Stripe quotes minor units; the cents→coins hop must never round
    through a float."""
    assert coins_for_usd_cents(cents) == expected


def test_rub_per_coin_reproduces_the_old_pinned_peg_at_its_own_fx() -> None:
    """The retired ``0.1`` literal was right only at USD/RUB = 90 —
    which is exactly what the derivation returns there."""
    assert rub_per_coin(90.0, float(COINS_PER_USD)) == pytest.approx(0.1)


def test_rub_per_coin_tracks_fx_instead_of_freezing() -> None:
    """A 10% move in the fix moves the displayed coin price with it."""
    assert rub_per_coin(99.0, float(COINS_PER_USD)) == pytest.approx(0.11)
    assert rub_per_coin(81.0, float(COINS_PER_USD)) == pytest.approx(0.09)


@pytest.mark.parametrize(
    ("usd_to_rub", "coins_per_usdt"),
    [(0.0, 900.0), (-1.0, 900.0), (90.0, 0.0), (90.0, -1.0)],
)
def test_rub_per_coin_refuses_nonsense_inputs(usd_to_rub: float, coins_per_usdt: float) -> None:
    """A bad FX quote yields 0.0 so the caller falls back rather than
    rendering a negative or infinite coin price to a user."""
    assert rub_per_coin(usd_to_rub, coins_per_usdt) == 0.0


# ---------------------------------------------------------------------------
# R11 — the rouble leg, priced through the dollar anchor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount_rub",
    ["0.10", "1.23", "99.99", "100.00", "499.00", "10000.00"],
)
def test_coins_for_rub_reproduces_the_legacy_rate_at_the_legacy_fix(
    amount_rub: str,
) -> None:
    """At USD/RUB = 90 the derivation IS the retired ``* 10`` literal.

    This is the whole safety argument for R11: the offline path — and
    every existing invoice, test fixture and operator expectation —
    lands on exactly the number the bot has always credited. Only when
    the fix moves away from 90 does anything change.
    """
    amount = Decimal(amount_rub)
    assert coins_for_rub(amount, FALLBACK_USD_TO_RUB) == int(amount * 10)


def test_coins_for_rub_closes_the_withdraw_desk_arbitrage() -> None:
    """One dollar's worth of roubles must buy one dollar's worth of coins.

    The frozen ``10 coins/RUB`` was the derived price at USD/RUB = 90.
    At a weaker rouble it undercut the dollar providers on the same
    coin — and the /withdraw desk still redeems at ``COINS_PER_USD``,
    so the gap was risk-free money taken straight out of the owner's
    wallet, with nothing capping how much a payer may deposit.
    """
    for fix in (75.0, 90.0, 120.0, 150.0):
        one_dollar_of_roubles = Decimal(str(fix))
        # Priced live: a dollar in, a dollar's worth of coins out.
        assert coins_for_rub(one_dollar_of_roubles, fix) == COINS_PER_USD
        # Priced frozen: the payer walks away with more coins than the
        # desk sells a dollar for, and cashes the difference out.
        frozen = int(one_dollar_of_roubles * 10)
        assert (frozen > COINS_PER_USD) is (fix > FALLBACK_USD_TO_RUB)


@pytest.mark.parametrize(
    ("amount_rub", "expected"),
    [
        ("0.001", 0),  # sub-coin top-up credits nothing (M-E-1 parity)
        ("0.00", 0),
        ("-5.00", 0),  # a negative amount can never mint
    ],
)
def test_coins_for_rub_floors_and_refuses_non_positive(amount_rub: str, expected: int) -> None:
    assert coins_for_rub(Decimal(amount_rub), FALLBACK_USD_TO_RUB) == expected


def test_coins_for_rub_keeps_the_change_rather_than_rounding_up() -> None:
    """Floor, not round — same posture as :func:`coins_for_usd`."""
    # 33.33 RUB at USD/RUB = 90 → 333.3 coins.
    assert coins_for_rub(Decimal("33.33"), 90.0) == 333


@pytest.mark.parametrize(
    "quote",
    [None, 0.0, -1.0, 1.0, 29.9, 300.1, 1e9, float("nan"), float("inf")],
)
def test_sane_usd_to_rub_refuses_an_implausible_quote(quote: float | None) -> None:
    """A broken FX upstream must not be allowed to price a mint.

    ``0`` would divide-by-zero, ``1.0`` would credit 900 coins per
    rouble, and NaN fails every comparison — so the guard checks
    finiteness before the band.
    """
    assert sane_usd_to_rub(quote) == FALLBACK_USD_TO_RUB


@pytest.mark.parametrize("quote", [30.0, 75.0, 90.0, 120.0, 300.0])
def test_sane_usd_to_rub_passes_a_plausible_quote_through(quote: float) -> None:
    """The band is a broken-upstream detector, not a view on the rouble."""
    assert sane_usd_to_rub(quote) == quote


def test_coins_for_rub_falls_back_on_a_garbage_fix() -> None:
    """The clamp is applied inside the pricing helper, not only at the
    edge — so a caller that forgets to sanitise still cannot mint."""
    assert coins_for_rub(Decimal("100.00"), 0.0) == 1000
    assert coins_for_rub(Decimal("100.00"), float("nan")) == 1000


# --------------------------------------------------------------------------
# #1698 — the pricing helpers are total.
#
# Every one of the amounts below either raised or returned a non-zero coin
# count before the bound existed, which is what makes these assertions
# worth writing: ``coins_for_usd(1e308)`` and ``coins_for_usd(inf)`` raised
# OverflowError inside ``math.floor``; ``coins_for_usd(nan)`` raised
# ValueError; ``coins_for_rub(Decimal("1E+999999999"))`` raised
# decimal.Overflow and ``Decimal("NaN")`` raised InvalidOperation on the
# ``<= 0`` test that was supposed to catch it; and the merely-absurd cases
# priced into integers long enough that formatting one into a log line
# raises in its own right.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("0"),
        Decimal("100.50"),
        Decimal(10**9),
        0,
        1,
        10**9,
        0.0,
        12.5,
        1e9,
    ],
    ids=[
        "dec-zero",
        "dec-typical",
        "dec-at-the-bound",
        "int-zero",
        "int-one",
        "int-at-the-bound",
        "float-zero",
        "float-typical",
        "float-at-the-bound",
    ],
)
def test_is_priceable_accepts_anything_anyone_could_actually_pay(
    amount: Decimal | float | int,
) -> None:
    """The bound is a limit on arithmetic, not on commerce.

    It is inclusive, and the largest pack on /topup is six orders of
    magnitude below it in every unit.
    """
    assert is_priceable(amount) is True


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1E+999999999"),
        Decimal(10**9 + 1),
        10**100000,
        10**9 + 1,
        float("nan"),
        float("inf"),
        1e308,
    ],
    ids=[
        "dec-nan",
        "dec-inf",
        "dec-neg-inf",
        "dec-astronomical",
        "dec-just-over",
        "int-astronomical",
        "int-just-over",
        "float-nan",
        "float-inf",
        "float-astronomical",
    ],
)
def test_is_priceable_refuses_what_the_arithmetic_cannot_carry(
    amount: Decimal | float | int,
) -> None:
    """Answering ``False`` is itself the assertion.

    Each of these makes some *other* obvious way of asking the question
    raise instead of answering: ``math.isfinite`` on the astronomical
    ``int``, a bare comparison on either NaN.
    """
    assert is_priceable(amount) is False


def test_max_priceable_amount_is_a_decimal_so_the_comparison_stays_exact() -> None:
    """A float bound would round; an int bound would drag a huge operand
    through ``float()`` on the way to the comparison."""
    assert isinstance(MAX_PRICEABLE_AMOUNT, Decimal)
    assert Decimal(10**9) == MAX_PRICEABLE_AMOUNT


@pytest.mark.parametrize(
    "amount_usd",
    [float("inf"), float("-inf"), float("nan"), 1e308, 1e15],
    ids=["inf", "-inf", "nan", "astronomical", "merely-absurd"],
)
def test_coins_for_usd_prices_an_unusable_amount_at_zero(amount_usd: float) -> None:
    """Zero, not an exception — every caller already reads a non-positive
    result as "do not credit", and the alternative is a 500 from inside a
    webhook the provider will then retry forever."""
    assert coins_for_usd(amount_usd) == 0


@pytest.mark.parametrize(
    "amount_cents",
    [10**5000, 10**12],
    ids=["astronomical", "merely-absurd"],
)
def test_coins_for_usd_cents_prices_an_unusable_amount_at_zero(
    amount_cents: int,
) -> None:
    """The integer path has no infinity to trip over, so the danger is
    purely magnitude: the product used to be bindable-in-principle and
    unprintable-in-practice."""
    assert coins_for_usd_cents(amount_cents) == 0


@pytest.mark.parametrize(
    "amount_rub",
    ["NaN", "Infinity", "-Infinity", "1E+999999999", "10000000000"],
    ids=["nan", "inf", "-inf", "astronomical", "merely-absurd"],
)
def test_coins_for_rub_prices_an_unusable_amount_at_zero(amount_rub: str) -> None:
    """``is_priceable`` runs before the ``<= 0`` test, because on NaN the
    ``<= 0`` test is the thing that raises."""
    assert coins_for_rub(Decimal(amount_rub), FALLBACK_USD_TO_RUB) == 0


def test_the_bound_does_not_disturb_ordinary_pricing() -> None:
    """A regression fence around the three helpers: the amounts a real
    top-up carries price exactly as they did before #1698."""
    assert coins_for_usd(10.0) == 9000
    assert coins_for_usd_cents(1000) == 9000
    assert coins_for_rub(Decimal("900.00"), 90.0) == 9000
