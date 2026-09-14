"""The one place the COM↔USD top-up rate is written down (T-019, R5).

Before this module the buy rate lived as four independent ``900``
literals — ``crypto.py:_COINS_PER_USD``, ``stripe.py:_USD_TO_COINS``,
``crypto_invoices.py:COINS_PER_USD`` and the ``WITHDRAW_COINS_PER_USDT``
default in :mod:`~telegram_invite_bot.config.settings`. Four literals
for one economic quantity is a silent-divergence trap: change the price
on the /topup screen and the webhook keeps crediting the old number, so
the coins a user is *promised* stop matching the coins they *get*. The
economy audit (``docs/ECONOMY_RATE_AUDIT.md`` §1) flagged it as the
structural risk behind every rate recommendation in that document.

Everything USD-denominated now reads :data:`COINS_PER_USD` from here.
The module deliberately imports nothing from the project so that
``config.settings`` can derive the withdraw-side default from it
without an import cycle.

T-020 (R11) folded the **rouble** top-up leg in here too. It used to
be a second frozen literal (``yookassa.py:_RUB_TO_COINS = 10``) living
one directory away, and a frozen rouble price against a floating fix is
not merely inaccurate — it is arbitrageable. The withdraw desk pays out
at :data:`COINS_PER_USD`; whenever USD/RUB rises above
:data:`FALLBACK_USD_TO_RUB`, ten coins per rouble is *cheaper* than
nine hundred coins per dollar, so a buyer could top up in roubles and
cash straight back out in USDT at a profit, unboundedly, out of the
owner's wallet. :func:`coins_for_rub` derives the rouble price from the
dollar anchor instead, so the two legs cannot drift apart. See
``docs/ECONOMY_RATE_AUDIT.md`` §8.6.

Deliberately NOT centralised here: the withdraw rate itself — it stays
an env-tunable (``WITHDRAW_COINS_PER_USDT``) precisely so an operator
can open a buy/sell spread (audit R6) without touching code.
"""

from __future__ import annotations

import math
from decimal import Decimal

#: Coins credited per 1 USD on the top-up side.
#:
#: Mirrors the legacy ``get_exchange_rate("USD", "COINS") or 900.0``
#: fallback (``bot.py:18302``). Changing this changes what buyers pay
#: for coins across every provider at once — which is the point.
COINS_PER_USD: int = 900

#: USD/RUB used when the FX upstream can't be reached.
#:
#: Matches the legacy ``rates.get("RUB", 90)`` default. Together with
#: :data:`COINS_PER_USD` it reproduces the historic ``1 COM = 0.1 RUB``
#: peg exactly — which is the point: the offline path should land on the
#: number the bot has always shown, not on a third independent guess.
FALLBACK_USD_TO_RUB: float = 90.0

#: Assets whose unit is pegged to the US dollar to within rounding.
#:
#: #1233: a missing asset→USD rate used to be read as parity for
#: every asset. For a dollar stablecoin that is true; for anything
#: else it silently discards the entire conversion — 0.05 BTC
#: credited as five cents, or the same error running the other way
#: and emptying the owner's wallet. Parity now survives only for the
#: assets that actually hold it, and every other asset refuses the
#: credit rather than guessing. Same posture as
#: :func:`sane_usd_to_rub` on the rouble leg: a quote that will PRICE
#: money is checked, not assumed.
USD_PEGGED_ASSETS: frozenset[str] = frozenset({"USDT", "USDC", "BUSD", "USDD"})

#: Fiat currency every crypto top-up invoice is denominated in.
#:
#: #1232: a Crypto Pay invoice minted with a bare ``asset`` is billed
#: in units of THAT asset, so a button reading "$5" produced an
#: invoice for five bitcoin. Invoices are now ``currency_type="fiat"``
#: in this currency, with the chosen asset narrowed to the settlement
#: option — which is the only form that can honour a USD price tag.
#: The credit path branches on the same value.
INVOICE_FIAT: str = "USD"

#: Largest single amount any pricing helper below will convert, read in
#: whatever unit the caller quotes it in.
#:
#: Not an opinion about how much anyone might spend — a billion of any
#: of these units is orders of magnitude past the largest pack on the
#: /topup screen, and the bound exists to stop arithmetic rather than
#: commerce. Every helper here multiplies before it floors, and the
#: operand arrives as a provider-supplied string, so the multiply is
#: reachable with a number picked to break it rather than to buy
#: anything. ``Decimal("1E+999999999")`` is finite and positive and
#: raises ``decimal.Overflow`` inside :func:`coins_for_rub`;
#: ``float("1e308")`` passes ``math.isfinite`` and becomes ``inf``
#: inside :func:`coins_for_usd`, where ``math.floor`` refuses it; and an
#: amount merely absurd rather than infinite prices cleanly into a
#: hundred-thousand-digit ``int`` that ``EconomyRepo.credit`` then
#: declines to bind — correctly, but the decline is logged, and
#: formatting a number past CPython's 4300-digit conversion limit into
#: that log line raises in turn, this time inside the open economy
#: transaction.
#:
#: Same posture as :func:`sane_usd_to_rub` one field over: a number that
#: is about to PRICE money is checked, not assumed.
MAX_PRICEABLE_AMOUNT: Decimal = Decimal(10**9)


def is_priceable(amount: Decimal | float | int) -> bool:
    """True when ``amount`` is finite and small enough to price.

    Three arms for what reads like one question, because neither half
    can be folded into the other. Finiteness cannot be tested by the
    comparison itself — ``Decimal("NaN")`` and ``float("nan")`` both
    raise ``InvalidOperation`` against a ``Decimal`` bound rather than
    answering ``False`` — and a large ``int`` cannot be tested by
    ``math.isfinite``, which converts to ``float`` first and raises
    ``OverflowError``. The ``Decimal`` comparison is exact at any
    magnitude, so it is the one operation here that is safe to reach
    with a hostile number.
    """
    if isinstance(amount, Decimal):
        return amount.is_finite() and amount <= MAX_PRICEABLE_AMOUNT
    if isinstance(amount, int):
        return amount <= MAX_PRICEABLE_AMOUNT
    return math.isfinite(amount) and amount <= MAX_PRICEABLE_AMOUNT


def coins_for_usd(amount_usd: float) -> int:
    """Coins owed for ``amount_usd``, floored.

    Floor, not round: a fractional coin of change stays with the
    service rather than being credited as a phantom coin. Matches the
    legacy ``int(amount_usd * rate)`` on positive floats. See M-E-1 in
    ``audits/01_economy.md``.

    Total over every ``float``: an amount :func:`is_priceable` refuses
    prices at zero rather than raising, and every caller already reads
    a non-positive result as "do not credit".
    """
    if not is_priceable(amount_usd):
        return 0
    return int(math.floor(amount_usd * COINS_PER_USD))


def coins_for_usd_cents(amount_cents: int) -> int:
    """Coins owed for ``amount_cents``, in integer math throughout.

    Providers that quote minor units (Stripe's ``amount_total``) go
    through here so the cents→USD hop never touches a float.

    :data:`MAX_PRICEABLE_AMOUNT` is read in the unit the caller quotes,
    so here it caps one session at a billion *cents* — ten million
    dollars, still far past the largest pack.
    """
    if not is_priceable(amount_cents):
        return 0
    return (amount_cents * COINS_PER_USD) // 100


#: Plausibility band for a USD/RUB quote that is about to PRICE money.
#:
#: The FX upstream is a free third-party endpoint; a schema change, a
#: partial payload or an outage page can hand back ``0``, ``1.0`` or a
#: nonsense magnitude. Everywhere else that would render a silly number
#: on a card — here it would MINT coins, so an implausible quote is
#: refused outright in favour of :data:`FALLBACK_USD_TO_RUB`. The band
#: is deliberately wide: it is a broken-upstream detector, not an
#: opinion about where the rouble should trade.
USD_RUB_MIN: float = 30.0
USD_RUB_MAX: float = 300.0


def sane_usd_to_rub(quote: float | None) -> float:
    """``quote`` if it is a plausible USD/RUB fix, else the fallback.

    NaN and infinity are rejected too — ``float("nan")`` fails every
    comparison, so a bare range check would let it through and then
    ``Decimal(str(nan))`` would blow up inside :func:`coins_for_rub`.
    """
    if quote is None or not math.isfinite(quote):
        return FALLBACK_USD_TO_RUB
    if not (USD_RUB_MIN <= quote <= USD_RUB_MAX):
        return FALLBACK_USD_TO_RUB
    return quote


def coins_for_rub(amount_rub: Decimal, usd_to_rub: float) -> int:
    """Coins owed for a rouble top-up, floored, anchored on USD.

    The rouble is priced *through* the dollar rather than beside it:
    ``coins = amount_rub / usd_to_rub * COINS_PER_USD``. At the historic
    ``usd_to_rub = 90`` that is exactly the legacy ``amount_rub * 10``
    (``bot.py:3179``), so the offline path is numerically unchanged;
    away from 90 it is finally the same price the dollar providers
    charge, which is what stops the rouble leg from becoming a cheap
    door into a desk that pays out in dollars (R11).

    Kept in :class:`~decimal.Decimal` end to end. YooKassa quotes
    amounts as ``"99.99"`` strings and the multiply happens *before*
    the divide, so the only rounding is the final floor — a fractional
    coin of change stays with the service rather than being credited as
    a phantom coin, same posture as :func:`coins_for_usd`.

    Total over every ``Decimal``: the :func:`is_priceable` test runs
    first because it is also the finiteness test, and
    ``Decimal("NaN") <= 0`` raises rather than answering.
    """
    if not is_priceable(amount_rub) or amount_rub <= 0:
        return 0
    rate = Decimal(str(sane_usd_to_rub(usd_to_rub)))
    return int(amount_rub * Decimal(COINS_PER_USD) / rate)


def rub_per_coin(usd_to_rub: float, coins_per_usdt: float) -> float:
    """RUB a single coin is worth, derived rather than pinned.

    The old display peg was a hard-coded ``0.1`` — internally consistent
    only while USD/RUB happened to sit at 90 (``900 coins = 1 USD =
    90 RUB``). It silently misprices the moment the fix moves, so the
    profile card and the currency converter now compute it from the
    live rate and the rate coins actually leave the ecosystem at.

    Guards against a zero/negative FX quote by returning ``0.0``; the
    caller is expected to fall back to its own default rather than
    render a nonsense price.
    """
    if usd_to_rub <= 0.0 or coins_per_usdt <= 0.0:
        return 0.0
    return usd_to_rub / coins_per_usdt
