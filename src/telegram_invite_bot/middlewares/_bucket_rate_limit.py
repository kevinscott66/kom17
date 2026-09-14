"""Shared token-bucket rate-limit base for middleware classes.

Both :class:`TransferRateLimitMiddleware` (per-user transfer gate,
Stage 19) and :class:`RateLimitMiddleware` (per-user gaming gate,
Stage 31) implement the *same* token-bucket logic: lookup bucket,
advance time, decide admit/reject, write back, on reject answer the
user with a bilingual "wait N seconds" copy. Until this module
existed the two classes were 95% copy-paste — see commit history
on ``transfer_rate_limit.py`` and ``rate_limit.py``.

The base class owns:

* the per-user bucket dict + instance-wide :class:`asyncio.Lock`
* the clock indirection (``_now``) — tests monkeypatch it
* the admit/reject decision via :func:`utils.rate_limit.refill_and_consume`
* the reject path: language pick, friendly answer, structured log

Subclasses override only:

* :attr:`_REJECT_RU` / :attr:`_REJECT_EN` — bilingual template strings
* :attr:`_LOG_MESSAGE` — log tag for observability filtering
* (optionally) ``_DEFAULT_CAPACITY`` / ``_DEFAULT_REFILL_PER_SECOND``
  via constructor defaults
* (optionally) :meth:`BucketRateLimitMiddleware._is_free` — a
  per-call escape hatch for variants of the command that cost the
  bot nothing (#1537)

Why class-level template-method (not constructor-injected callables)
-------------------------------------------------------------------
The reject copy is part of each command's UX contract, not a
runtime knob. Pinning it as class attributes keeps each concrete
middleware self-describing (you can read the file and see what a
rejected /send vs. a rejected /cpc says) and lets the unit test
diff against the literal string. A constructor-injected ``template``
would move the contract into the wiring code, which is what we
already had with the copy-paste version.

Why a single instance-wide lock, not per-user locks
---------------------------------------------------
The critical section is three nanoseconds of synchronous Python
under the GIL — no I/O. A per-user lock table would add a dict of
locks plus an eviction story for it. The single lock keeps the
common case uncontended (one update per user per second) and is
still needed because aiogram's middleware chain is genuinely
concurrent across updates.

Why the bucket table is LRU-capped
----------------------------------
Same reason :class:`~telegram_invite_bot.middlewares.throttling.ThrottlingMiddleware`
caps its own: each concrete middleware is built once at router-wiring
time and lives for the whole process, so an uncapped dict keeps one
entry per user_id that ever touched the command — forever. Six
instances are wired today, and two of them are shared, so the
instance count is NOT the command count (#1526). Four stand alone:
/voice, /joke, /send and the gaming gate. One is shared across the
four geocoder surfaces — /weather, /forecast, /city and /time (see
:class:`~telegram_invite_bot.middlewares.ai_rate_limit.WeatherRateLimitMiddleware`).
The last, the /ai instance, is also the gate on /quote: deliberate,
because /quote spends the owner's own paid DeepSeek account rather
than a stranger's free API, and argued out in
:class:`~telegram_invite_bot.middlewares.ai_rate_limit.ContentRateLimitMiddleware`.
Both sharings are wired in
:func:`routers.main_router.build_main_router`. A bot in public
groups sees a churn of unique IDs that never come back; the cap turns
that unbounded growth into a fixed ceiling.

Evicting a bucket resets its owner to a full one, so the victim
choice matters: the LRU order is touched on EVERY decision, including
rejected ones, which keeps an active flooder at the recent end of the
table and out of eviction range. The user we do drop is by
construction the least recently seen — idle, and therefore already
refilled to capacity in all but pathological configurations, so the
reset gives them nothing they would not have had anyway.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar

from aiogram import BaseMiddleware
from loguru import logger

from telegram_invite_bot.utils.language import pick_by_language
from telegram_invite_bot.utils.rate_limit import BucketState, refill_and_consume

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject

log = logger.bind(component="middlewares.bucket_rate_limit")


class BucketRateLimitMiddleware(BaseMiddleware):
    """Abstract token-bucket rate limiter; subclass and set the copy.

    Concrete middlewares specialise the user-facing copy and the log
    tag; the bucket arithmetic is identical for every gate.
    """

    # Per-subclass overridable copy. Defined as ClassVar so mypy
    # recognises subclass shadowing as legitimate (not "redefining a
    # field"). Both languages MUST contain ``{seconds}`` — the format
    # call is the only render point and a missing placeholder would
    # surface at runtime not import-time.
    _REJECT_RU: ClassVar[str] = "⏳ Слишком часто. Подожди {seconds} сек."
    _REJECT_EN: ClassVar[str] = "⏳ Too fast. Wait {seconds}s."

    # Log tag for observability filtering. Subclasses override so an
    # operator can grep "transfer rate-limit reject" vs.
    # "gaming rate-limit reject" without parsing class names.
    _LOG_MESSAGE: ClassVar[str] = "rate-limit reject"

    # Fallback "wait" seconds returned when ``refill_per_second == 0``
    # (a misconfigured router with refill disabled). Subclasses can
    # override to match their domain's expected cool-down magnitude.
    _ZERO_REFILL_FALLBACK_SECONDS: ClassVar[int] = 60

    # Ceiling on the bucket table (see module docstring). 10_000 users
    # of per-command history is far more than any of these gates needs
    # to stay accurate — a bucket refills fully within a minute or two
    # at every configured rate, so a user who fell out of the last
    # 10_000 distinct IDs is not one the gate would still be holding
    # tokens against.
    _MAX_TRACKED_USERS: ClassVar[int] = 10_000

    def __init__(
        self,
        capacity: float,
        refill_per_second: float,
        is_exempt: Callable[[int], bool] | None = None,
    ) -> None:
        self._capacity = float(capacity)
        self._refill_per_second = refill_per_second
        # Optional per-domain bypass. Legacy's transfer gate let
        # developers through before it touched the tracker at all
        # (bot.py:18832), and a gate that throttles the people
        # testing it is a gate that gets disabled in a hurry.
        # Living on the base class rather than in one subclass means
        # any future gate opts in by passing a predicate instead of
        # re-implementing ``__call__``. ``None`` — the default every
        # gate except /send takes — means nobody is exempt.
        self._is_exempt = is_exempt
        # Per-instance OrderedDict — each subclass instance has its own
        # bucket map (no cross-router bleed), ordered so eviction can
        # pick the least-recently-seen user in O(1).
        self._buckets: OrderedDict[int, BucketState] = OrderedDict()
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        """Indirection over :func:`time.monotonic` for tests."""
        return monotonic()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if self._is_free(event, data):
            # #1537: a variant of the command that costs nothing must
            # not spend the budget that protects the variant that does.
            # Checked before the bucket is even looked up, so a free
            # call creates no row and cannot make an existing row
            # look "recently seen" to the evictor either.
            return await handler(event, data)

        user = data.get("event_from_user") or getattr(event, "from_user", None)
        if user is None or getattr(user, "id", None) is None:
            # Anonymous updates bypass — see subclass docstrings for
            # the per-domain rationale.
            return await handler(event, data)

        user_id = int(user.id)
        if self._is_exempt is not None and self._is_exempt(user_id):
            # No token consumed and no bucket row created — legacy
            # returned before both ``check`` and ``add``
            # (bot.py:18832-18833), so an exempt user can never
            # arrive at a partially-drained bucket later.
            return await handler(event, data)

        async with self._lock:
            now = self._now()
            state = self._buckets.get(user_id, BucketState(tokens=self._capacity, updated_at=now))
            next_state, admitted = refill_and_consume(
                state,
                now=now,
                capacity=self._capacity,
                refill_per_second=self._refill_per_second,
            )
            self._buckets[user_id] = next_state
            # Touch on every decision, admitted or not — a rejected
            # flooder must stay at the recent end or they would evict
            # themselves and come back to a full bucket.
            self._buckets.move_to_end(user_id)
            self._evict_if_needed()

        if admitted:
            return await handler(event, data)

        # Reject path: bilingual cool-down message + structured log.
        if self._refill_per_second > 0:
            deficit = max(0.0, 1.0 - next_state.tokens)
            seconds = max(1, math.ceil(deficit / self._refill_per_second))
        else:  # pragma: no cover — defensive; floors don't permit this today
            seconds = self._ZERO_REFILL_FALLBACK_SECONDS

        # ``LanguageMiddleware`` is an OUTER middleware on the root
        # router (mounted in ``build_main_router``) and every one of
        # these gates is an INNER middleware on a child router, so
        # ``data["lang"]`` is already stamped by the time we get here —
        # and it is the only source that honours a stored ``/lang``
        # choice. The Telegram ``language_code`` is the fallback for
        # callers outside that chain (unit tests, and any future
        # dispatcher-level use, where this comment's earlier claim about
        # ordering would hold again).
        # Answering an English-speaking user's ``/lang ru`` bot in
        # English exactly when they are already annoyed is the whole
        # reason LanguageMiddleware exists.
        stamped = data.get("lang")
        if isinstance(stamped, str) and stamped:
            lang = "en" if stamped.lower()[:2] == "en" else "ru"
        else:
            lang_code = (getattr(user, "language_code", None) or "ru").lower()[:2]
            lang = "en" if lang_code == "en" else "ru"
        template = pick_by_language(lang, ru=self._REJECT_RU, en=self._REJECT_EN)
        text = template.format(seconds=seconds)

        answer = getattr(event, "answer", None)
        if callable(answer):
            await answer(text)
        log.bind(uid=user_id, seconds=seconds, tokens=next_state.tokens).info(self._LOG_MESSAGE)
        return None

    def _is_free(self, event: TelegramObject, data: dict[str, Any]) -> bool:
        """Does this particular call cost a token? Default: yes.

        The gates protect an expensive downstream — a paid upstream
        call, a wallet transaction, a game round. Some commands have
        a cheap variant that reaches the same handler chain and
        therefore the same gate: ``/voice`` with no arguments only
        prints a usage hint, and legacy answered it out of the same
        registration. Spending a token on it lets five typos lock the
        real command out for a minute.

        The hook receives the middleware ``data`` mapping, which by
        this point already carries whatever the matched handler's
        filters produced — aiogram's ``TelegramEventObserver.trigger``
        runs ``handler.check()`` and merges its result into the kwargs
        BEFORE wrapping the call in the inner middlewares — so a
        subclass can read ``data["command"]`` and decide on the parsed
        :class:`~aiogram.filters.command.CommandObject` rather than
        re-parsing the raw text.

        Note this is NOT :attr:`_is_exempt`: that one is about WHO is
        calling (developers), this one about WHAT is being called.
        """
        return False

    def _evict_if_needed(self) -> None:
        """Drop least-recently-seen buckets down to the cap.

        Called under :attr:`_lock`, so the table is not mutated
        concurrently. ``popitem(last=False)`` is the oldest end of the
        ``move_to_end`` order — see the module docstring for why that
        victim is the safe one.
        """
        while len(self._buckets) > self._MAX_TRACKED_USERS:
            self._buckets.popitem(last=False)
