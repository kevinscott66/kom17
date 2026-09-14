"""COM → fiat/crypto rate service (A-09).

The bot's INTERNAL game currency is "COM" (🪙). This service converts
COM into fiat / crypto units. It is NOT a real-world FX tool — one leg is
anchored on the rate coins actually cash out at, and every other fiat is
derived from a single free upstream (``exchangerate-api.com``). Crypto
rates (BTC / ETH / TON) are never fetched; they come from a hardcoded
fallback table.

Ported from legacy ``CurrencyManager`` (bot.py:3271-3402) + the
``AVAILABLE_CURRENCIES`` / ``COM_TO_CURRENCY`` constants (bot.py:3225-3268).

Mirrors the Stage-5 :class:`WeatherService` shape: a per-instance TTL
cache + an optionally-injected ``httpx.AsyncClient``, so one instance
constructed at ``build_main_router`` time gives every user warm rates.
The client seam is test-only in practice — nothing passes one in
production (#423), so a cache miss opens its own connection.
The legacy cache was buggy/unused; here it actually caches for 1h and
refreshes lazily on expiry (M-P-9 pattern).

Behaviour:

* On a successful fetch: ``base["USD"] = 1 / coins_per_usdt`` and
  ``base["RUB"] = base["USD"] * usd_to_rub`` (the upstream's RUB fix, 90
  if missing); each supported fiat present in the payload gets
  ``base[cur] = base["USD"] * rates[cur]``.
* Crypto (BTC / ETH / TON) always come from the hardcoded fallback.
* On ANY failure (non-200 / exception): fall back to the hardcoded
  ``COM_TO_CURRENCY`` table with the USD/RUB legs re-anchored. The user
  never sees an error.

T-019 (R4) inverted the anchor. Legacy pinned ``1 COM = 0.1 RUB`` and
computed USD from it — a peg that was only ever the *derived* value at
900 coins/USDT and USD/RUB = 90. Whenever the fix moved, the bot quoted a
coin price that the /withdraw desk would not honour, in both directions.
USD is the leg with a real price (``WITHDRAW_COINS_PER_USDT`` is what a
coin actually leaves the ecosystem at), so USD is now the anchor and RUB
is derived. At the historic 900 / 90 the whole table is numerically
unchanged; away from it, it is finally correct. See
``docs/ECONOMY_RATE_AUDIT.md`` §7.1 R4.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime

import httpx
from loguru import logger

from telegram_invite_bot.services.payments.rates import (
    COINS_PER_USD,
    FALLBACK_USD_TO_RUB,
)
from telegram_invite_bot.utils.http_read import send_capped
from telegram_invite_bot.utils.numbers import format_amount_compact, format_amount_fine

log = logger.bind(component="services.currency")

_EXCHANGE_URL_V4 = "https://api.exchangerate-api.com/v4/latest/USD"
_EXCHANGE_URL_V6 = "https://v6.exchangerate-api.com/v6/{key}/latest/USD"

# Default USD→RUB used when the upstream omits the RUB rate.
_DEFAULT_USD_TO_RUB = FALLBACK_USD_TO_RUB

# How long a FALLBACK table may sit in the cache (#1615).
#
# A real table earns ``cache_ttl_seconds`` — an hour by default —
# because published rates do not move faster than that. The offline
# table has no such claim behind it: it is what we quote when we could
# not ask. Cached for the same hour, one second of network trouble
# landing on a cold cache priced every exchange, market and withdraw
# for the next hour off a frozen anchor, with only a warning in the
# log to say so. A minute is short enough that a blip costs a minute,
# and long enough that a hard-down upstream is still not fetched once
# per request.
_FALLBACK_CACHE_TTL_SECONDS = 60.0

# Crypto codes never come from the (fiat-only) upstream — always served
# from the hardcoded fallback below.
_CRYPTO = frozenset({"BTC", "ETH", "TON"})

# ----------------------------------------------------------------------
# Constants copied VERBATIM from legacy bot.py:3225-3268.
#
# Note: legacy also lists BRL; it is intentionally omitted here because
# the A-09 supported set (this port's ``AVAILABLE_CURRENCIES`` keys) does
# not include BRL.
# ----------------------------------------------------------------------

_COM_EMOJI = "🪙"
_TON_EMOJI = "💎"

# Magnitude steps for the K/M abbreviation (legacy ``format_amount``).
_THRESHOLD_K = 1_000.0
_THRESHOLD_M = 1_000_000.0

# ``name`` is the RU display name, ``name_en`` the EN one. Both are
# present so a currency rendered to an English user never leaks Cyrillic
# (the ru/en interface-convergence rule) — see :func:`currency_name`.
#
# ``symbol`` + ``format`` are the RR-6 #67 restoration: legacy rendered a
# converted amount through a per-currency template so ``$1.50K`` and
# ``1.50K ₽`` each read the way a native does. The first port dropped
# both fields and printed a bare number, which is why every amount looked
# like the same currency. ``format`` is a project-owned constant — it is
# never built from user input, so ``str.format`` here can't be turned
# into a format-string attack — and no symbol contains ``<``/``&``, so
# the result is safe to drop into an HTML card unescaped.
AVAILABLE_CURRENCIES: dict[str, dict[str, object]] = {
    "COM": {
        # The internal coin. Its CODE stays ``COM`` — it is the key of
        # this table, the value stored in ``economy.users.display_currency``
        # (never a column called ``currency``, and never in ``users.db``)
        # and the one every rate map is written against — while the
        # TICKER the user reads is ``DLAB``. Renaming the code would need
        # a data migration for something nobody outside the source ever
        # sees.
        "name": "DLAB",
        "name_en": "DLAB",
        "ticker": "DLAB",
        "emoji": _COM_EMOJI,
        "symbol": _COM_EMOJI,
        "decimals": 0,
        "format": "{amount} {symbol}",
    },
    "RUB": {
        "name": "Российский рубль",
        "name_en": "Russian Ruble",
        "emoji": "🇷🇺",
        "symbol": "₽",
        "decimals": 0,
        "format": "{amount} {symbol}",
    },
    "USD": {
        "name": "Доллар США",
        "name_en": "US Dollar",
        "emoji": "🇺🇸",
        "symbol": "$",
        "decimals": 2,
        "format": "{symbol}{amount}",
    },
    "EUR": {
        "name": "Евро",
        "name_en": "Euro",
        "emoji": "🇪🇺",
        "symbol": "€",
        "decimals": 2,
        "format": "{amount}{symbol}",
    },
    "BTC": {
        "name": "Bitcoin",
        "name_en": "Bitcoin",
        "emoji": "₿",
        "symbol": "₿",
        "decimals": 8,
        "format": "{amount} {symbol}",
    },
    "ETH": {
        "name": "Ethereum",
        "name_en": "Ethereum",
        "emoji": "Ξ",
        "symbol": "Ξ",
        "decimals": 6,
        "format": "{amount} {symbol}",
    },
    "TON": {
        "name": "Toncoin",
        "name_en": "Toncoin",
        "emoji": _TON_EMOJI,
        "symbol": _TON_EMOJI,
        "decimals": 4,
        "format": "{amount} {symbol}",
    },
    "CNY": {
        "name": "Китайский юань",
        "name_en": "Chinese Yuan",
        "emoji": "🇨🇳",
        "symbol": "¥",
        "decimals": 2,
        "format": "{amount}{symbol}",
    },
    "KZT": {
        "name": "Казахстанский тенге",
        "name_en": "Kazakhstani Tenge",
        "emoji": "🇰🇿",
        "symbol": "₸",
        "decimals": 0,
        "format": "{amount}{symbol}",
    },
    "BYN": {
        "name": "Белорусский рубль",
        "name_en": "Belarusian Ruble",
        "emoji": "🇧🇾",
        "symbol": "Br",
        "decimals": 2,
        "format": "{amount}{symbol}",
    },
    "UAH": {
        "name": "Украинская гривна",
        "name_en": "Ukrainian Hryvnia",
        "emoji": "🇺🇦",
        "symbol": "₴",
        "decimals": 2,
        "format": "{amount}{symbol}",
    },
    "GBP": {
        "name": "Фунт стерлингов",
        "name_en": "Pound Sterling",
        "emoji": "🇬🇧",
        "symbol": "£",
        "decimals": 2,
        "format": "{symbol}{amount}",
    },
    "JPY": {
        "name": "Японская иена",
        "name_en": "Japanese Yen",
        "emoji": "🇯🇵",
        "symbol": "¥",
        "decimals": 0,
        "format": "{amount}{symbol}",
    },
    "CHF": {
        "name": "Швейцарский франк",
        "name_en": "Swiss Franc",
        "emoji": "🇨🇭",
        "symbol": "Fr",
        "decimals": 2,
        "format": "{amount}{symbol}",
    },
    "TRY": {
        "name": "Турецкая лира",
        "name_en": "Turkish Lira",
        "emoji": "🇹🇷",
        "symbol": "₺",
        "decimals": 2,
        "format": "{amount}{symbol}",
    },
    "INR": {
        "name": "Индийская рупия",
        "name_en": "Indian Rupee",
        "emoji": "🇮🇳",
        "symbol": "₹",
        "decimals": 2,
        "format": "{amount}{symbol}",
    },
}

#: Fallback display currency — legacy's ``(currency or "RUB").upper()``.
DEFAULT_CURRENCY = "RUB"

#: What an English user gets before they have ever picked a currency.
DEFAULT_CURRENCY_EN = "USD"


def display_code(code: str) -> str:
    """The ticker to SHOW for ``code`` — its own code unless overridden.

    Only the internal coin overrides it: stored and computed as ``COM``
    everywhere in the source and the database, read as ``DLAB`` by the
    user. Every other currency is its own ISO/crypto ticker, so this is
    the identity for them.
    """
    meta = AVAILABLE_CURRENCIES.get(code)
    if meta is None:
        return code
    return str(meta.get("ticker") or code)


def currency_name(code: str, lang: str) -> str:
    """Localized display name for a currency code (EN never leaks Cyrillic).

    Falls back to the raw code for an unknown id. ``lang == "en"`` picks
    ``name_en``; anything else picks the RU ``name``.
    """
    meta = AVAILABLE_CURRENCIES.get(code)
    if meta is None:
        return code
    key = "name_en" if lang == "en" else "name"
    return str(meta.get(key) or meta.get("name") or code)


def currency_label(code: str, lang: str) -> str:
    """``🇺🇸 US Dollar (USD)`` — the one-line "which currency" phrase.

    Used by every card that has to name the active currency, so the
    emoji/name/code triple can't drift between the picker, the
    confirmation and ``/rate``.
    """
    meta = AVAILABLE_CURRENCIES.get(code)
    if meta is None:
        return code
    return f"{meta['emoji']} {currency_name(code, lang)} ({display_code(code)})"


def effective_currency(stored: str | None, lang: str) -> str:
    """The currency to render amounts in, given the saved preference.

    Ports legacy ``CurrencyManager.get_user_currency`` (bot.py:3369)
    **verbatim**, quirk included: for an English user a stored ``RUB`` is
    read as ``USD``. That is not an oversight here — ``display_currency``
    has a server-side default of ``'RUB'``, so "row says RUB" and "user
    never chose" are the same bytes, and legacy resolved the ambiguity in
    favour of showing an English speaker dollars.

    The quirk is kept because the ambiguity it resolves is still in the
    data. The second reader it used to be synchronised with is gone
    (T-011), but every row legacy wrote is still here, and in those rows
    ``'RUB'`` remains indistinguishable from "never chose" — dropping the
    rule would silently re-interpret an existing user's stored value
    rather than fix anything. The cards work around it honestly instead:
    when the stored code and the effective one differ, the picker says so
    out loud (``h_cur_en_default_note``) rather than pretending the tap
    did nothing.

    An unknown / empty / malformed stored value degrades to the locale
    default rather than raising — the column is writable by the legacy
    process and by hand, so this function treats it as untrusted.
    """
    code = (stored or "").strip().upper()
    if code not in AVAILABLE_CURRENCIES:
        code = ""
    if lang == "en":
        return code if code and code != DEFAULT_CURRENCY else DEFAULT_CURRENCY_EN
    return code or DEFAULT_CURRENCY


def _format_converted(value: float, decimals: int) -> str:
    """Legacy's converted-amount ladder: ``2.50M`` / ``1.50K`` / ``10.5``.

    Restores the K/M abbreviation the first port dropped (RR-6 #67). The
    sub-1000 branch keeps the per-currency decimal count and trims
    trailing zeros, so fiat stays tidy (``12.50`` → ``12.5``) while
    crypto keeps its precision.
    """
    if value >= _THRESHOLD_M:
        return f"{value / _THRESHOLD_M:.2f}M"
    if value >= _THRESHOLD_K:
        return f"{value / _THRESHOLD_K:.2f}K"
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_display_amount(
    com_amount: float,
    code: str,
    rate: float,
    *,
    include_com: bool = True,
) -> str:
    """``1.00K 🪙 (~$1.11)`` — a COM amount rendered in a display currency.

    Ports legacy ``CurrencyManager.format_amount`` (bot.py:3340). The
    ``rate`` is passed in rather than fetched so this stays a pure
    function: the caller already awaited :meth:`CurrencyService.get_rate`
    for its own card, and a formatter that can do I/O is a formatter that
    can time out mid-sentence.

    ``include_com=False`` yields just the converted half — the shape the
    picker's "how it looks" examples want, where the COM column is the
    bullet label and repeating it would be noise.

    Deliberate divergence: legacy switched the COM emoji to 👑 above a
    million (``format_com_amount``, bot.py:3193). Here 🪙 is used at every
    magnitude, because 👑 is the VIP marker throughout the new pipeline
    and a big balance is not a VIP badge.
    """
    code = (code or DEFAULT_CURRENCY).upper()
    com_part = f"{format_amount_compact(com_amount)} {_COM_EMOJI}"
    if code == "COM":
        return com_part

    meta = AVAILABLE_CURRENCIES.get(code, AVAILABLE_CURRENCIES[DEFAULT_CURRENCY])
    raw_decimals = meta.get("decimals")
    decimals = raw_decimals if isinstance(raw_decimals, int) else 2
    converted = com_amount * rate
    # TON borrows the fine-grained ladder: a Toncoin amount is routinely
    # small enough that the fiat rounding would print a flat ``0``.
    amount_text = (
        format_amount_fine(converted) if code == "TON" else _format_converted(converted, decimals)
    )
    rendered = str(meta["format"]).format(amount=amount_text, symbol=meta["symbol"])
    return f"{com_part} (~{rendered})" if include_com else rendered


# КУРСЫ ВАЛЮТ — the offline fallback table, used only when the FX API is
# unreachable. Values are the legacy verbatim ones, i.e. the table as it
# stood at 900 coins/USDT and USD/RUB = 90. The live path (T-019 R4)
# recomputes USD and RUB from the real payout rate instead of reading
# them here; crypto has no upstream and always falls back to these.
COM_TO_CURRENCY: dict[str, float] = {
    "COM": 1.0,
    # Фиат
    "RUB": 0.1,
    "USD": 0.001111,
    "EUR": 0.001053,
    "GBP": 0.00087,
    "CNY": 0.008,
    "JPY": 0.1667,
    "KZT": 0.526,
    "BYN": 0.00345,
    "UAH": 0.0435,
    "TRY": 0.03125,
    "INR": 0.0952,
    "CHF": 0.00098,
    # Крипто
    "BTC": 0.0000000185,
    "ETH": 0.000000556,
    "TON": 0.0001667,
}

# Fiat currencies the upstream can provide a rate for (everything in the
# supported set that isn't COM/RUB/crypto). COM is the base unit and
# crypto is fiat-less, so both are excluded. RUB stays out too, but for a
# different reason since T-019 (R4): it is no longer *pinned*, it is
# computed alongside USD from the same anchor, so re-deriving it in the
# loop would just recompute the identical number.
_DERIVABLE_FIAT = frozenset(
    code for code in AVAILABLE_CURRENCIES if code not in {"COM", "RUB"} and code not in _CRYPTO
)


def _usable_rate(value: object) -> float | None:
    """Coerce one upstream rate leg, rejecting anything unusable.

    An ``isinstance(value, int | float)`` check on its own lets three
    kinds of junk into the table, none of which raise where they enter:

    * ``True`` — ``bool`` *is* an ``int``, so a JSON ``true`` prices the
      leg at exactly 1.0;
    * ``NaN`` / ``Infinity`` — :func:`json.loads` accepts both literals,
      and every downstream guard is written as ``x <= 0`` or ``x > 0``,
      which ``NaN`` slips through in *both* directions;
    * ``0`` and negatives — some upstreams answer an unsupported code
      with a zero rather than omitting it.

    They surface far from here, as a ``0.00 €`` quote on ``/rate``, a
    ``nan`` rouble line on the profile card, or — for ``inf`` — an
    ``OverflowError`` out of that card's ``round(balance * rate)``.
    Consumers each re-guard this by hand today; making the table itself
    finite-and-positive is one check instead of five.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    rate = float(value)
    if not math.isfinite(rate) or rate <= 0.0:
        return None
    return rate


class CurrencyService:
    """COM→currency rates with a 1h TTL cache (A-09).

    Designed to be shared across requests: the computed ``base_rates``
    table is cached per-instance for ``cache_ttl_seconds`` (default 1h)
    and refreshed lazily on the next call after expiry. A singleton
    constructed at lifespan/router-build time gives every user the same
    warm table — a fresh instance per call would defeat the cache.

    One exception to that lifetime: a table that came from the offline
    fallback is cached for :data:`_FALLBACK_CACHE_TTL_SECONDS` instead,
    so a momentary upstream failure costs a minute of frozen pricing
    rather than an hour of it (#1615).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        cache_ttl_seconds: float = 3600.0,
        coins_per_usdt: float = float(COINS_PER_USD),
    ) -> None:
        self._api_key = api_key or None
        self._client = client
        self._timeout = timeout
        self._cache_ttl = cache_ttl_seconds
        self._coins_per_usdt = coins_per_usdt if coins_per_usdt > 0.0 else float(COINS_PER_USD)
        # (timestamp, base_rates, ttl) — None until the first lazy
        # fill. The TTL travels WITH the entry because it is not a
        # property of the service: a fallback table lives a minute,
        # a real one lives the configured hour (#1615).
        self._cache: tuple[float, dict[str, float], float] | None = None
        # Serialises *misses* only; see :meth:`_base_rates`.
        self._fill_lock = asyncio.Lock()

    @property
    def _url(self) -> str:
        """Keyed v6 endpoint when an API key is configured, else v4."""
        if self._api_key:
            return _EXCHANGE_URL_V6.format(key=self._api_key)
        return _EXCHANGE_URL_V4

    def _now(self) -> float:
        """Wall-clock seconds — overridable in tests for TTL control."""
        return datetime.now(UTC).timestamp()

    async def get_rate(self, code: str) -> float:
        """Return ``1 COM → code`` as a float.

        The value comes from the cached/freshly-computed ``base_rates``
        table, then the hardcoded ``COM_TO_CURRENCY`` fallback, then
        ``1.0`` as a last resort — mirroring legacy ``get_rate``.

        T-019 (R4): RUB used to short-circuit to a flat ``0.1`` here.
        That was the *derived* value at USD/RUB = 90, not an independent
        truth, so the moment the fix moved the bot quoted a coin price
        that no longer matched what a coin actually withdraws for. RUB
        now goes through the same table as every other fiat.
        """
        code = code.upper()
        rates = await self._base_rates()
        if code in rates:
            return rates[code]
        return COM_TO_CURRENCY.get(code, 1.0)

    async def usd_to_rub(self) -> float:
        """The raw USD/RUB fix behind the COM table (T-020 R11).

        The service computes this internally to derive ``base["RUB"]``
        but only ever exposed the COM-denominated result. R11 needs the
        fix itself, to price a rouble top-up off the same dollar anchor
        the withdraw desk uses.

        Recovered from the *public, already-cached* table rather than
        by a second fetch: ``base["RUB"] / base["USD"]`` cancels the
        ``1 / coins_per_usdt`` both legs share and leaves the fix. That
        keeps one cache and one upstream call per hour, and — more to
        the point — makes it impossible for the rouble a payment is
        priced at to disagree with the rouble ``/rate`` quoted a second
        earlier.

        Falls back to :data:`FALLBACK_USD_TO_RUB` if either leg is
        missing or non-positive; the caller is crediting real money and
        must never divide by a zero that came off the wire.
        """
        rates = await self._base_rates()
        usd = rates.get("USD", 0.0)
        rub = rates.get("RUB", 0.0)
        if usd <= 0.0 or rub <= 0.0:
            return FALLBACK_USD_TO_RUB
        return rub / usd

    def _cached(self) -> dict[str, float] | None:
        """The table if it is present and still inside ITS OWN TTL.

        The stored TTL, not ``self._cache_ttl`` — the two differ
        exactly when the entry is a fallback table (#1615).
        """
        if self._cache is None:
            return None
        ts, rates, ttl = self._cache
        if self._now() - ts > ttl:
            return None
        return rates

    async def _base_rates(self) -> dict[str, float]:
        """Return the cached base-rate table, refreshing on expiry.

        The refresh is guarded so that an expiry does not turn into one
        upstream fetch *per waiting caller*. This service is a singleton
        shared by every handler and both fiat webhooks, and the fetch is
        an await point — so without the lock, every request that arrived
        while the table was stale would see the same miss and start its
        own request. That costs a metered API quota that is billed per
        call, and makes each caller pay the full fetch latency instead
        of the first one paying it for all of them.

        The hit path deliberately stays outside the lock: the common
        case is a warm table, and it must not queue behind anything.
        Inside the lock the cache is re-read, because by the time a
        waiter is admitted the coroutine ahead of it has usually already
        filled it — that second read is what turns N fetches into one.
        """
        rates = self._cached()
        if rates is not None:
            return rates
        async with self._fill_lock:
            # Someone may have filled it while we waited for the lock.
            rates = self._cached()
            if rates is not None:
                return rates
            rates, is_fallback = await self._compute_base_rates()
            # ``min`` and not a bare constant: a deployment that
            # configured a TTL shorter than a minute asked for a
            # fresher table than we would otherwise give it, and the
            # degraded path must not LENGTHEN a cache lifetime.
            ttl = (
                min(self._cache_ttl, _FALLBACK_CACHE_TTL_SECONDS)
                if is_fallback
                else self._cache_ttl
            )
            self._cache = (self._now(), rates, ttl)
            return rates

    def _fallback_table(self) -> dict[str, float]:
        """The offline table, with the USD/RUB legs re-anchored.

        ``COM_TO_CURRENCY`` froze USD and RUB at 900 coins/USDT and
        USD/RUB = 90. An operator who opens a spread (audit R6) would
        otherwise see the degraded path quietly quote the *old* price —
        the one case where being wrong is least visible, because nothing
        errored. Every other code keeps its hardcoded value: without the
        upstream there is nothing better to derive them from.
        """
        base = dict(COM_TO_CURRENCY)
        base["USD"] = 1.0 / self._coins_per_usdt
        base["RUB"] = base["USD"] * _DEFAULT_USD_TO_RUB
        return base

    async def _compute_base_rates(self) -> tuple[dict[str, float], bool]:
        """Fetch upstream + derive the table; fall back on any failure.

        The user NEVER sees an error — a non-200 or an exception just
        yields the hardcoded ``COM_TO_CURRENCY`` table.

        Returns ``(table, is_fallback)``. The flag exists because the
        caller caches the answer and the two kinds of answer do not
        deserve the same lifetime (#1615); it is returned rather than
        stashed on ``self`` so that the fact travels with the value it
        describes and cannot be read a fill later.
        """
        payload = await self._fetch()
        if payload is None:
            log.warning("currency upstream unavailable — using hardcoded fallback")
            return self._fallback_table(), True

        # v6 keyed endpoint flags failures in-band with ``result: error``
        # and carries rates under ``conversion_rates``; v4 keyless uses
        # ``rates`` and no result flag. Normalise both here.
        if payload.get("result") == "error":
            log.warning("currency upstream returned error result — using fallback")
            return self._fallback_table(), True
        rates_raw = payload.get("conversion_rates")
        if not isinstance(rates_raw, dict):
            rates_raw = payload.get("rates")
        if not isinstance(rates_raw, dict):
            log.warning("currency upstream returned no rates — using fallback")
            return self._fallback_table(), True

        usd_to_rub = _usable_rate(rates_raw.get("RUB"))
        if usd_to_rub is None:
            usd_to_rub = _DEFAULT_USD_TO_RUB

        base: dict[str, float] = dict(COM_TO_CURRENCY)
        base["COM"] = 1.0
        # T-019 (R4): anchor the whole table on USD, because USD is the
        # only leg with a *real* price — ``coins_per_usdt`` is the rate
        # coins actually leave the ecosystem at on /withdraw. The old
        # code anchored on a pinned RUB=0.1 and derived USD from it,
        # which silently mispriced every currency whenever the USD/RUB
        # fix moved away from 90. Numerically identical at the historic
        # 900 coins/USDT and USD/RUB = 90; correct everywhere else.
        usd_rate = 1.0 / self._coins_per_usdt
        base["USD"] = usd_rate
        base["RUB"] = usd_rate * usd_to_rub
        for cur in _DERIVABLE_FIAT:
            if cur == "USD":
                continue
            cur_rate = _usable_rate(rates_raw.get(cur))
            if cur_rate is not None:
                base[cur] = usd_rate * cur_rate
        # Crypto stays on the hardcoded fallback (already in ``base`` via
        # the COM_TO_CURRENCY seed).
        return base, False

    async def _fetch(self) -> dict[str, object] | None:
        """GET the upstream rates; return parsed JSON or ``None`` on failure.

        Isolated so tests can monkeypatch this single seam (mirrors the
        weather port's MockTransport approach but simpler for a one-call
        service).
        """
        if self._client is not None:
            return await self._fetch_with(self._client)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._fetch_with(client)

    async def _fetch_with(self, client: httpx.AsyncClient) -> dict[str, object] | None:
        try:
            # #1936: a WALL-CLOCK cap, not just the per-phase httpx one.
            # ``httpx.Timeout(2.5)`` bounds connect, write, pool and each
            # individual read — it does not bound their sum. An upstream
            # that answers the handshake and then trickles one byte every
            # two seconds resets the read timer on every chunk
            # ``send_capped`` pulls out of ``aiter_bytes()``, so the fetch
            # runs for as long as the peer cares to keep dripping.
            #
            # That breaks the ordering #1614 rests on. ``fx.py`` caps this
            # client at ``FX_UPSTREAM_TIMEOUT_SECONDS`` (2.5) precisely so
            # it gives up BEFORE the money path's
            # ``FX_TIMEOUT_SECONDS`` (3.0) ``wait_for`` — because our
            # giving up caches the offline table and the caller's giving
            # up caches nothing. Against a drip the phase caps never fire,
            # the 3.0 s ceiling wins, and every later call repeats the
            # full three-second wait with no cache entry to expire: the
            # exact "offline anchor forever" state that constant exists to
            # prevent. Capping the whole call restores the intended order.
            #
            # It also bounds how long ``_base_rates`` holds ``_fill_lock``,
            # which is taken across this await.
            #
            # Not raised through: a timeout here is the same event as any
            # other upstream failure — no rates — and the caller already
            # degrades to the fallback table on ``None``. An outer
            # cancellation is NOT swallowed: ``wait_for`` re-raises a
            # cancellation it did not itself request as ``CancelledError``,
            # which is a ``BaseException`` and passes this ``except`` by.
            response = await asyncio.wait_for(send_capped(client, "GET", self._url), self._timeout)
        except TimeoutError as exc:
            log.warning("currency fetch exceeded {t}s: {e}", t=self._timeout, e=exc)
            return None
        except httpx.HTTPError as exc:
            log.warning("currency HTTP error: {e}", e=exc)
            return None
        if response.status_code != 200:
            log.warning("currency HTTP {s}", s=response.status_code)
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None
