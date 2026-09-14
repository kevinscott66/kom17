"""Pure numeric formatting helpers.

Extracted from legacy ``bot.py`` — three closely-related routines that
together account for ~200 call sites: ``format_number`` (thousands
separator), ``format_amount_compact`` (1.5K / 2.50M short form), and
``format_amount_fine`` (small-fraction TON-style with trailing-zero
trim). Pulling them out together is the right granularity because
callers usually pick one of the three based on the value's magnitude,
and seeing the three side-by-side makes the picking obvious.

Design note — no emojis in this module
--------------------------------------
The legacy versions bake emoji constants (``COM_EMOJI``, ``TON_EMOJI``,
``COM_EMOJI_LARGE``) into the returned string. That couples a pure
number-to-string transform to a project-wide config singleton, and
makes the helpers unusable for ad-hoc admin output, logs, and tests.
The split here returns *just the number string*; callers compose with
whatever currency sign they need at the call site (``f"{format_amount_compact(x)} {COM_EMOJI}"``).
This costs each caller one ``f`` interpolation and buys back
testability and reusability.

Design note — float-safe thousands separator
--------------------------------------------
Legacy used ``f"{num:,}".replace(",", " ")`` which works for floats too
(``f"{1234.5:,}"`` → ``"1,234.5"``, no comma in the decimal portion), so
we keep that idiom. The thin-ASCII space is the RU convention; if the
project ever wants U+00A0 (NBSP) it's a one-character change here.
"""

from __future__ import annotations

from typing import Final

_THRESHOLD_K: Final[int] = 1_000
_THRESHOLD_M: Final[int] = 1_000_000

# Balance-tier emoji ladder, from legacy ``format_balance_emoji``.
# Each entry is ``(min_inclusive_amount, emoji)``, sorted descending so
# the first match wins. The thresholds are user-facing — changing them
# changes the visual feel of every wallet display in the bot, so they
# live as a named constant rather than buried magic numbers.
_BALANCE_TIERS: Final[tuple[tuple[int, str], ...]] = (
    (10_000, "💎"),
    (5_000, "💰"),
    (1_000, "🪙"),
    (0, "👛"),
)

#: CPython refuses to parse an integer literal longer than 4300 digits
#: (the int/str conversion limit), so ``int("1" * 5000)`` raises
#: ``ValueError`` exactly the way ``int("²")`` does. Telegram caps a
#: text message at 4096 characters, so no incoming message can carry a
#: longer run today — the bound costs nothing and keeps the helper
#: honest for callers reading from anywhere but a message.
_MAX_INT_DIGITS: Final[int] = 4300

#: SQLite stores ``INTEGER`` as a signed 64-bit value, so aiosqlite
#: raises ``OverflowError: Python int too large to convert to SQLite
#: INTEGER`` for a bind parameter outside it. That escapes a handler as
#: an unhandled exception — ``/profile 99999999999999999999`` and the
#: numeric-id form of every moderation command crashed rather than
#: answering "пользователь не найден". Telegram ids, coin amounts, XP
#: and durations all live far inside this bound, so a token past it is
#: never a value this bot can mean; refusing it here turns the crash
#: into whatever the call site already says about a bad number.
MAX_DB_INT: Final[int] = 2**63 - 1


def is_digit_run(token: str) -> bool:
    """True when ``token`` is a plain ASCII digit run, of any magnitude.

    The shape half of :func:`is_int_token`, split out because one caller
    needs it without the value bounds: ``utils/http_body`` reads a
    ``Content-Length`` off the wire, and for that gate a declaration too
    large to represent is the strongest possible "too large", not a
    malformed header. Everything else wants :func:`is_int_token`, which
    also promises the value is one this process can carry.

    It is also the single place the repo is allowed to call bare
    ``str.isdigit()`` — 128 code points answer it ``True`` and then
    raise inside ``int()`` (#102), which is why
    ``tests/regression/test_unicode_digit_parsing.py`` bans the call
    everywhere else.
    """
    return token.isascii() and token.isdigit()


def is_int_token(token: str) -> bool:
    """True when ``token`` is a plain ASCII digit run that ``int()`` takes.

    ``str.isdigit()`` is the obvious gate to put in front of an
    ``int()`` and it is wrong: 128 code points answer ``True`` to it and
    then raise ``ValueError`` inside ``int()`` — superscripts (``"²"``),
    subscripts (``"₃"``), circled digits (``"①"``), Ethiopic numerals…
    A user typing ``/roll ²`` did not get "это не число", they crashed
    the handler. So every gate in front of an ``int()`` goes through
    here instead.

    >>> is_int_token("42"), is_int_token("²"), is_int_token("")
    (True, False, False)

    Note that ``int()`` is *wider* than this, not narrower: it also
    accepts Arabic-Indic digits (``int("٣") == 3``), surrounding
    whitespace and a leading sign. Rejecting those is deliberate —
    callers parse ids, amounts and counts, where ``"٣"`` silently
    reading as ``3`` is more surprising than a usage hint, and the
    sign, where it is meaningful, is stripped by the caller before the
    check so the two stay visible at the call site.

    The two length/magnitude bounds are not redundant.
    ``_MAX_INT_DIGITS`` runs first because ``int()`` itself refuses a
    longer literal, and this predicate must not raise the very error it
    exists to prevent. ``MAX_DB_INT`` then rejects what SQLite cannot
    store. The negative extreme is off by one — ``-2**63`` is a valid
    SQLite value that this gate calls invalid — which is deliberate:
    two symmetric bounds are worth more here than one exact one, and
    nothing in this bot means minus nine quintillion.

    >>> is_int_token("9" * 25), is_int_token(str(2**63 - 1))
    (False, True)
    """
    if not is_digit_run(token) or len(token) > _MAX_INT_DIGITS:
        return False
    return int(token) <= MAX_DB_INT


def parse_int_token(token: str, *, signed: bool = False) -> int | None:
    """``int(token)`` when the token is a plain ASCII integer, else ``None``.

    The value-returning counterpart to :func:`is_int_token`, and the way
    to close the second half of this bug family: a gate and the ``int()``
    behind it that disagree about the exact string. ``token.lstrip("-")``
    in the gate followed by ``int(token)`` looks symmetric and is not —
    ``lstrip`` removes EVERY leading ``-``, so ``"--5"`` passed the gate
    and raised ``ValueError`` inside the ``int()``. Here there is one
    string and one grammar: at most one leading sign (only when
    ``signed``), then an ASCII digit run.

    >>> parse_int_token("42"), parse_int_token("-5", signed=True)
    (42, -5)
    >>> parse_int_token("--5", signed=True), parse_int_token("-5")
    (None, None)
    """
    body = token
    negative = False
    if signed and body[:1] in {"+", "-"}:
        negative = body[0] == "-"
        body = body[1:]
    if not is_int_token(body):
        return None
    value = int(body)
    return -value if negative else value


def page_offset(index: int, page_size: int) -> int:
    """SQL ``OFFSET`` for the zero-based page ``index``, clamped to a
    value SQLite can bind.

    #1984. :data:`MAX_DB_INT` bounds what a callback field may CARRY —
    that is all ``core/callback_fields.DbInt`` claims, and it says so —
    but it says nothing about what a handler then COMPUTES from it.
    ``index * page_size`` is that computation, and at the ceiling it
    lands one multiplication outside the 64-bit range: the bound #1978
    installed to remove ``OverflowError: Python int too large to
    convert to SQLite INTEGER`` was followed by the same error a few
    lines further down the same handler.

    Clamping rather than refusing is the honest choice. Every page past
    the end already has a defined meaning at these call sites — no rows,
    snap back to the first page — and ``MAX_DB_INT // page_size`` pages
    is past the end of any table this bot will ever hold. So the clamp
    changes the answer for no page a user can legitimately be on, and
    turns a traceback into the empty result the caller already handles.

    A negative index is the first page, not an offset counted from the
    far end; a non-positive ``page_size`` is read as ``1`` so the helper
    cannot divide by zero on the way to protecting a query.

    >>> page_offset(3, 10), page_offset(-1, 10)
    (30, 0)
    >>> page_offset(MAX_DB_INT, 10) <= MAX_DB_INT
    True
    """
    size = max(page_size, 1)
    return min(max(index, 0), MAX_DB_INT // size) * size


def format_number(num: int | float) -> str:
    """Format an integer (or finite float) with space thousands separators.

    >>> format_number(1234567)
    '1 234 567'
    >>> format_number(1234.5)
    '1 234.5'

    No rounding is performed — pass an already-rounded value if you need
    a specific precision. Negative numbers keep their sign at the front.
    """
    return f"{num:,}".replace(",", " ")


def format_amount_compact(amount: float) -> str:
    """Short magnitude form: ``500``, ``1.50K``, ``2.50M``.

    Mirrors legacy ``format_com_amount`` minus the emoji. The
    sub-1000 case is rendered as an integer because COM amounts are
    integer-valued at that scale in this app; fractional values
    smaller than 1 lose precision intentionally. Use
    :func:`format_amount_fine` for the TON-style tiny-fraction case.
    """
    if amount >= _THRESHOLD_M:
        return f"{amount / _THRESHOLD_M:.2f}M"
    if amount >= _THRESHOLD_K:
        return f"{amount / _THRESHOLD_K:.2f}K"
    return f"{int(amount)}"


def format_amount_fine(amount: float) -> str:
    """Magnitude form that preserves small fractions, for crypto-style values.

    Mirrors legacy ``format_ton_amount`` minus the emoji. The decimal
    tail is trimmed of trailing zeros so ``0.10000000`` displays as
    ``0.1``, which is what users expect from a wallet balance. The
    explicit precision tiers (2 / 6 / 8 decimals) match the legacy
    contract and keep wallet-history rendering pixel-stable.
    """
    if amount >= _THRESHOLD_M:
        return f"{amount / _THRESHOLD_M:.2f}M"
    if amount >= _THRESHOLD_K:
        return f"{amount / _THRESHOLD_K:.2f}K"
    if amount >= 1:
        return f"{amount:.2f}"
    if amount >= 0.000_001:
        # Trim trailing zeros, then a stranded ``.`` if the value
        # happened to be an exact integer-in-disguise (e.g. 0.5
        # formatted as ``0.500000`` trims to ``0.5``, not ``0.5.``).
        return f"{amount:.6f}".rstrip("0").rstrip(".")
    return f"{amount:.8f}".rstrip("0").rstrip(".")


def format_xp_short(xp: int) -> str:
    """Signed short XP for catalog rows: ``3000`` → ``+3k``, ``750`` → ``+750``.

    Ports legacy ``_format_rel_xp_short`` (bot.py:22271). The ``k`` step
    is FLOOR division on purpose — the catalog is a scan-and-compare
    list, so ``+3k`` beside ``+750`` reads as a magnitude, not a precise
    figure; :func:`format_xp_spark` is the one-decimal variant used in
    the confirmation card where a single number is the whole point.
    """
    if xp >= _THRESHOLD_M:
        return f"+{_trim_zeros(f'{xp / _THRESHOLD_M:.1f}')}M"
    if xp >= _THRESHOLD_K:
        return f"+{xp // _THRESHOLD_K}k"
    return f"+{xp}"


def format_xp_spark(xp: int, lang: str) -> str:
    """One-decimal short XP for the confirmation card: ``2500`` → ``2,5k``/``2.5k``.

    Ports legacy ``_format_rel_xp_spark`` (bot.py:22294), decimal comma
    included: Russian writes ``2,5k`` and English ``2.5k``, and this is
    the one number the "your bond jumped by …" line is built around, so
    the locale-correct separator is worth the branch. Non-positive
    values render as a bare ``0`` (legacy behaviour) rather than
    ``+0k``.
    """
    if xp <= 0:
        return "0"
    if xp < _THRESHOLD_K:
        return str(xp)
    s = _trim_zeros(f"{xp / _THRESHOLD_K:.1f}", decimal="," if lang == "ru" else ".")
    return f"{s}k"


def _trim_zeros(text: str, *, decimal: str = ".") -> str:
    """Drop a ``.0`` tail (and re-point the separator) — ``2.0`` → ``2``."""
    return text.replace(".", decimal).rstrip("0").rstrip(decimal)


def balance_tier_emoji(amount: int) -> str:
    """Pick the wallet emoji that matches ``amount``'s magnitude tier.

    Mirrors legacy ``format_balance_emoji``'s ladder: 💎 ≥10K, 💰 ≥5K,
    🪙 ≥1K, otherwise 👛. Extracted as a standalone helper because
    every economy handler (profile, balance, daily, leaderboards)
    needs the same ladder and inlining the chain of ``if amount >=``
    in each handler creates four copies that drift independently.

    Negative balances fall through to 👛 — that matches legacy's
    "no special case" behavior. Whether negative balances should
    even exist is a Stage 8 (write-helpers) question; for now we
    just render them, not validate them.
    """
    for threshold, emoji in _BALANCE_TIERS:
        if amount >= threshold:
            return emoji
    # The (0, "👛") entry always matches non-negative; this final
    # return covers the negative case explicitly so the function is
    # total over ``int`` rather than depending on the threshold list.
    return "👛"
