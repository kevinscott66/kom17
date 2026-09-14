"""Pure-math helpers for economy arithmetic.

Stage 8 (EconomyRepo write methods — debit, credit, transfer, daily
claim) is the next big chunk of the strangler migration. Before that
DB-touching code lands we want its pure-math invariants extracted and
tested standalone, for two reasons:

1. The legacy implementations bury arithmetic rules inside helpers that
   also do DB writes, transaction logging, cache invalidation, and WAL
   checkpointing (``set_balance``, ``add_coins`` in ``bot.py``). Pulling
   the math out makes both halves independently reviewable: this module
   covers "what should the new balance be?" with zero I/O; Stage 8
   covers "how do we persist it atomically?" with zero math.

2. These functions are pinpoint-tested with parametrized cases and used
   from multiple call sites (commission has 5 call sites in legacy; the
   debit/credit invariants will have dozens once Stage 8 ports). Having
   them in one place with one set of tests means a tax-rate tweak or
   floor adjustment is a one-line change with full coverage.

What's here
-----------
* :func:`commission_amount` — ``max(1, amount * percent / 100)`` with
  zero-guard. Mirrors legacy ``commission_amount`` at ``bot.py:9782``.
* :func:`validate_credit_amount` — predicate for "is this a legal
  credit?" (positive, non-overflow). Mirrors the ``amount <= 0`` guard
  in legacy ``add_coins``.
* :func:`validate_balance_target` — predicate for "is this a legal
  set-balance target?" (non-negative). Mirrors the ``new_balance < 0``
  guard in legacy ``set_balance``.

These are deliberately predicates returning ``bool`` rather than
"raise on invalid" — every legacy call site short-circuits with a
``logger.warning`` + ``return False``, and a typed exception hierarchy
just to satisfy a stricter style would force every handler to wrap a
``try/except`` for no behavioural change. Stage 8 may upgrade the
boolean to a Result-type if it turns out we want richer reason codes
for transaction logging.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# Upper bound for any legal balance / amount value. Picked to match
# :data:`telegram_invite_bot.utils.numbers.format_amount_compact`'s
# display range — anything above this won't render meaningfully in the
# wallet UI anyway, and the cap defends against integer overflow in
# downstream consumers that may cast to float. The legacy code had no
# such cap and could in principle accept ``2**63 - 1`` from an admin
# command; we tighten that here.
_MAX_AMOUNT: int = 10**15


def commission_amount(amount: int, percent: int) -> int:
    """Return the commission cut on ``amount`` at ``percent`` percent.

    Legacy contract (``bot.py:9782``):

    * Non-positive ``amount`` or ``percent`` → 0 (no commission to
      charge).
    * Otherwise ``max(1, amount * percent / 100)`` — the ``max(1, ...)``
      floor matters for tiny transfers: a 1-coin transfer at 5%
      computes to ``0.05`` which rounds to ``0``; legacy charges
      1 coin instead so commission is always at least visible.

    Rounding policy: **floor** (truncate toward zero), implemented
    with pure-int floor-division ``(amount * percent) // 100``
    (M-E-4 in audits/01_economy.md). Legacy uses ``int(amount *
    percent / 100)``; Python's ``/`` is float division, which loses
    precision for ``amount`` near ``_MAX_AMOUNT`` (``10**15``) — at
    the high end the float product silently shifts the lower ~10
    digits. The integer form is exact at all representable inputs
    and produces the same answer as the float form for every legal
    sub-cap value, so this is a precision fix only — no user sees a
    different commission on a realistic transfer.

    #1274: this function has **no production call sites**. Every
    shipped money path calls the float-form twin
    :func:`~telegram_invite_bot.services.referral_commission_service.purchase_commission_amount`
    instead: from the group-buy branch of ``handlers/shop.py``,
    twice inside :meth:`GroupDonationService.route` (the owner slice
    and the developer fee), and once each in
    :meth:`ReferralCommissionService.apply_purchase_commission` and
    :meth:`ReferralCommissionService.apply_developer_commission`.
    Named rather than numbered (#1476): every one of the four line
    anchors this list used to carry had drifted onto prose, which
    reads as "the list is stale, there are no call sites" — the
    opposite of the point being made.
    That twin is deliberately verbatim-legacy and its docstring says
    so; the divergence between the two forms needs
    ``amount * percent > 2**53`` to produce a different number, which
    this economy cannot reach. So the two are NOT in conflict about
    what ships — this copy is the stricter form kept for callers that
    do not owe legacy parity, and it currently has none.

    Floor (not ceil / banker's round) is the documented legacy
    posture and the safer policy for the service: a fractional cent
    of rounding stays with the user, not the operator. The
    ``max(1, ...)`` ladder still applies, so a 1-coin transfer at
    5% commission still charges 1 coin rather than 0.
    """
    if amount <= 0 or percent <= 0:
        return 0
    return max(1, (amount * percent) // 100)


def validate_credit_amount(amount: int) -> bool:
    """Return ``True`` iff ``amount`` is a legal credit value.

    Mirrors legacy ``add_coins``'s opening guard: must be strictly
    positive (a "credit" of 0 or negative is the caller's bug, not a
    no-op to swallow silently). The upper bound prevents overflow
    when the value is added to an existing balance.

    Stage 8 will use this in :class:`EconomyRepo.credit` to reject
    bad inputs before opening a transaction.
    """
    return 0 < amount <= _MAX_AMOUNT


def format_fiat_amount(minor_units: int | None, currency: str | None) -> str:
    """Render a minor-unit fiat amount as a human-readable string.

    M-E-6 migrated ``WithdrawalRequest.amount_fiat`` from Float
    (major units, e.g. ``10.50``) to Integer (minor units, e.g.
    ``1050`` cents/kopecks). This helper centralises the conversion
    back to display form so every renderer agrees on the format:
    ``"<major>.<two-decimals> <currency>"`` where the currency suffix
    is appended only when known. The two-decimal posture matches
    every fiat currency the legacy form ever wrote (USD, EUR, RUB —
    all centesimal). Currencies with different minor units (JPY, KWD)
    aren't represented in the legacy data set and adding per-currency
    exponent tables would be premature; revisit at the write-side
    port if the form ever grows new currencies.

    ``minor_units`` of ``None`` is the legacy null case ("withdrawal
    row was filed without a fiat amount, only crypto"); we render
    ``"—"`` to match the existing card UIs that already use that
    placeholder for empty cells. ``currency`` is similarly optional
    — operator-typed strings get HTML-escaped at the render site,
    not here.
    """
    if minor_units is None:
        return "—"
    # Negative amounts have no business meaning for withdrawals
    # (they'd represent a credit, not a request) but we render the
    # raw value rather than swallow the sign — an operator seeing
    # ``-10.50`` knows the row is corrupted at a glance.
    major, minor = divmod(abs(minor_units), 100)
    sign = "-" if minor_units < 0 else ""
    body = f"{sign}{major}.{minor:02d}"
    if currency:
        return f"{body} {currency}"
    return body


#: Widest fraction any asset the payout path can emit actually uses.
#: USDT/TON quote 6-9 decimals and Crypto Pay returns them as JSON
#: floats, so 8 is enough to render every real value exactly while
#: staying inside the range where a float still round-trips.
_CRYPTO_DECIMALS = 8


def format_crypto_amount(amount: float | None, asset: str | None) -> str:
    """Render a crypto payout amount as a human-readable string.

    The sibling of :func:`format_fiat_amount` for the other column.
    ``WithdrawalRequest.amount_crypto`` is a Float in the asset's own
    major units (``4.5`` USDT, not minor units), which is what the
    provider quotes and what the operator has to type into the payout
    form — so there is no scaling here, only formatting.

    Two properties are load-bearing and neither is what ``str(float)``
    gives you:

    * **No scientific notation.** ``str(1e-05)`` is ``"1e-05"``, and an
      operator copying that into a wallet field is a wrong transfer.
      Fixed-point formatting keeps every value in the shape a wallet
      accepts.
    * **No fake precision.** Plain ``f"{x:.8f}"`` renders ``4.5`` as
      ``"4.50000000"``, which reads like a measured-to-8-places figure
      rather than the round number it is, so trailing zeros are
      stripped. A whole number keeps no decimal point at all (``"5"``,
      not ``"5."``).

    ``None`` renders ``"—"``, matching :func:`format_fiat_amount` and
    the empty-cell placeholder the admin cards already use. ``asset``
    is optional and appended only when known; operator-visible strings
    get HTML-escaped at the render site, not here.
    """
    if amount is None:
        return "—"
    # ``:f`` rather than ``:g``: ``:g`` falls back to exponent form
    # outside its precision window, which is exactly the case a payout
    # amount must never render in.
    body = f"{amount:.{_CRYPTO_DECIMALS}f}"
    if "." in body:
        body = body.rstrip("0").rstrip(".")
    # An amount that rounds to nothing at 8 decimals is not "0" — that
    # would read as "nothing to pay". Say it is smaller than the
    # display can express instead.
    if body in {"0", "-0"} and amount != 0:
        body = "~0"
    if asset:
        return f"{body} {asset}"
    return body


#: A ``pending`` withdrawal older than this is treated as *stale*.
#:
#: Payouts are manual (see ``handlers/admin/withdrawals`` — the operator
#: pays out externally and then presses ✅), so "pending" is a queue the
#: owner has to work by hand. A queue nobody is told about is a queue
#: that silently stops being worked: prod carried one ``pending`` row for
#: five months without a single signal anywhere. One working day is the
#: honest boundary — a request filed in the evening should not raise an
#: alarm the same night, but one nobody touched by the next evening is
#: no longer "in progress", it is forgotten.
#:
#: Shared by the ``/admin_withdrawals`` card (the ⚠️ marker) and the
#: hourly sweeper (the owner alert) so the two surfaces can never
#: disagree about what "stale" means.
STALE_WITHDRAWAL_AGE: timedelta = timedelta(hours=24)


def parse_db_timestamp(value: str | None) -> datetime | None:
    """Parse a stored naive-UTC timestamp string, or ``None``.

    ``withdrawal_requests.created_at`` is TEXT, written as
    ``"YYYY-MM-DD HH:MM:SS"`` (``WithdrawService._now_iso``). It is a
    *legacy-shared* column though, so the pipeline cannot assume every
    row was written by the new code — imports and hand-edited rows may
    carry a ``T`` separator, sub-second digits, an offset, or plain
    garbage. ``fromisoformat`` accepts the first three; the fourth
    returns ``None`` rather than raising, because the only two callers
    are a diagnostic card and a courtesy alert, and neither may blow up
    over one malformed cell.

    An offset-carrying value is normalised to the naive-UTC frame the
    rest of the pipeline compares in (:func:`utils.time.db_now`), so a
    mixed table still yields comparable ages instead of a silent
    24-hour-ish skew.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def format_age(delta: timedelta) -> str:
    """Render an age as a single coarse unit: ``"7m"`` / ``"3h"`` / ``"12d"``.

    One unit, never two — the operator card puts this at the end of an
    already-dense row, and the decision it feeds ("is this old?") needs
    a magnitude, not a duration. Truncating rather than rounding keeps
    the label monotone with the threshold: something shown as ``23h`` is
    genuinely still under the 24-hour boundary.

    A negative delta (a row stamped in the future — clock skew, or a
    hand-written timestamp) clamps to ``"0m"``. Rendering ``"-3h"``
    would read as a bug in the card rather than in the row.
    """
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def validate_balance_target(new_balance: int) -> bool:
    """Return ``True`` iff ``new_balance`` is a legal set-balance target.

    Mirrors legacy ``set_balance``'s opening guard: non-negative
    (balances can't go below zero in this economy) and within the
    representable range.

    Note: this does NOT check whether the user has been excluded
    (``is_excluded_bot_user`` in legacy). That's a *policy* check
    against a different table; this module only owns the *arithmetic*
    side of the invariants.
    """
    return 0 <= new_balance <= _MAX_AMOUNT
