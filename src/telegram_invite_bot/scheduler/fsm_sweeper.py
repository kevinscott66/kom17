"""Periodic expiry of stale aiogram FSM sessions (Stage 35).

Motivation
----------
The /cpc handler (Stage 34) pins the challenger's FSM into
``awaiting_acceptance`` the moment the challenge card is sent, and
into ``awaiting_moves`` once accepted. Neither state has a built-in
TTL — aiogram's :class:`MemoryStorage` is a flat dict, untouched
unless someone reads/writes the key. Legacy used two thread-pool
cleanup passes (``rock_paper_scissors.py:231,254``) keyed on
``ACCEPT_TIMEOUT_SEC=60`` / ``CHOOSE_TIMEOUT_SEC=30`` wall-clock
deadlines stored on the session row. Without an equivalent sweeper in
the new pipeline, a challenge that never gets answered leaves the
challenger feeling "stuck busy" forever (every subsequent /cpc is
rejected with ``already_in_game``).

The sweeper here is the FIRST background pipeline in the strangler
refactor. Its shape is deliberately conservative so future periodic
tasks (/tip auto-deletion, /daily reminder, P2P watchdog) can copy it:

1. **Pure ``sweep_once`` core.** No sleep, no event loop assumptions.
   Inject a ``clock`` callable so tests pin the wall clock. Returns a
   :class:`SweepReport` so callers (and observability) can answer
   "how many sessions got expired this pass" without re-deriving it.

2. **Async ``run`` loop on top.** Owns the cancellation semantics:
   one ``CancelledError`` and the loop exits cleanly. Exception
   handling around ``sweep_once`` is defensive — a single bad rule
   shouldn't take the whole sweeper down (logged + continue).

3. **Per-state rules.** :class:`TimeoutRule` carries the wall-clock
   budget and the ``on_expire`` callback. The handler module that
   owns the state also owns the callback (see
   ``handlers/rps_timeouts.py``) — keeps the i18n + side-effect logic
   next to the rest of the flow, not buried in this generic module.

Storage scanning — coupled to :class:`MemoryStorage` internals
---------------------------------------------------------------
Aiogram's :class:`BaseStorage` does NOT expose a public "iterate all
keys" API. Three plausible implementations:

* **(chosen) Read ``MemoryStorage.storage`` directly.** That attribute
  is a ``defaultdict[StorageKey, MemoryStorageRecord]`` and aiogram's
  own source uses it as the canonical state container — not marked
  private (no underscore), stable across the 3.x branch we pin. The
  cost is a hard coupling: if we ever swap to ``RedisStorage`` or a
  hypothetical Stage 15 ``SqliteStorage``, this scan needs a new
  branch. Documented at the call site below.
* **Side-index via middleware.** Maintain our own
  ``dict[StorageKey, datetime]`` in the sweeper, updated by an
  ``outer_middleware`` that observes every state mutation. Cleaner
  abstraction-wise, but doubles the write path for every FSM update
  AND requires a process-lifetime singleton — a heavier infra
  investment for the same outcome at today's tiny FSM volume.
* **Read deadline from FSM data itself and scan whatever storage we
  have, refusing at construction the ones we cannot walk.** This is
  what Stage 35 lands. The bullet used to promise a no-op fallback on
  an unknown backend; the code never did that, and #1449 settled the
  question the other way, because a silent no-op sweeper is precisely
  the failure this module cannot afford. The deadline is stamped into
  FSM data as ``state_entered_at`` (ISO string) by each handler when
  it sets a state, so the sweeper's invariant is "every rule-covered
  state carries a parsable timestamp"; a state without one is treated
  as fresh (logged at WARNING, sweep skips it).

Why not :class:`asyncio.TaskGroup` or apscheduler? The sweeper is one
long-running task; a TaskGroup is overkill, and apscheduler adds a
heavyweight dep + persistence story we don't need for in-memory FSM
sessions. The plain ``asyncio.create_task`` + cancel-on-shutdown
shape in :class:`Application.start_background` is the minimum that
works.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from loguru import logger

if TYPE_CHECKING:
    from aiogram import Bot

log = logger.bind(component="scheduler.fsm_sweeper")


# Field name carried in FSM data dict on every state-bearing record.
# Stage 35 wires the rps handler to stamp it on every ``set_state``;
# future stages with their own FSM flows MUST do the same or their
# states will never expire. Centralised here so the contract is in one
# place rather than spread across handler modules.
STATE_ENTERED_AT_FIELD = "state_entered_at"


# #1489: how long the sweeper is willing to WAIT for a rule's guard.
#
# The loop below is strictly sequential, and the guard is a real lock
# that live handlers hold. Without a ceiling, one key whose lock is
# held by something stuck stops the expiry of every OTHER key and every
# other rule for as long as that lasts — with no exception raised, so
# the task stays formally alive, ``_report_background_death`` never
# fires and the only symptom is that nothing ever times out again.
#
# 5 s is deliberately generous against the thing actually holding the
# lock (a handler doing one or two Telegram round trips) and
# deliberately mean against anything slower. Losing the acquisition
# costs nothing: no side effect has happened yet, and the next pass
# retries the same key one ``interval_seconds`` later. A handler that
# still holds the match lock after five seconds is by definition
# mid-flight, which is precisely the case where the expiry SHOULD be
# abandoned rather than won.
#
# The ceiling covers ACQUISITION ONLY, not ``on_expire`` running under
# the lock. That asymmetry is deliberate. Abandoning an acquisition has
# no side effects; cancelling a callback part-way through cannot be
# undone, and — per the idempotency contract on :class:`TimeoutRule` —
# the retry would re-send whatever it already sent. The callback is
# also not the unbounded half: every registered ``on_expire`` only
# talks to Telegram, and aiogram's own session timeout bounds that. The
# lock is the only wait here with no ceiling of its own, so it is the
# only one that gets given one.
_GUARD_ACQUIRE_TIMEOUT_SECONDS = 5.0


# #1767: how many keys one pass may actually expire, and how long it
# waits between the sends.
#
# Every expiry is one outbound Telegram message, and the loop below
# used to fire them back to back on a 30-second cadence with nothing
# yielding to the rate limiter in between. The bot's own send rate is
# capped around 30/s globally; a pass that has to expire two hundred
# stale seats would blow straight through that, and the answer would
# be a 429 the sweeper had no handling for at all.
#
# 20 keys at 50 ms of spacing is ~1 second of sending inside a
# 30-second period — roughly 0.66 expiries per second sustained, an
# order of magnitude under the limit, while still draining a backlog
# of a thousand keys inside a minute of wall clock. Deferring the rest
# costs nothing: a deferred key is left completely untouched, so the
# next pass finds the same record and the user's notice is late by one
# interval rather than lost.
_EXPIRE_BUDGET_PER_PASS = 20
_EXPIRE_PACE_SECONDS = 0.05

# Ceiling on the backoff the sweeper will honour from a 429 before it
# goes back to its normal cadence. Telegram's own retry_after for a
# bot this size is seconds, not minutes; a wildly larger value is
# either a mistake or a punishment we cannot usefully sit out inside
# one task, and blocking the whole sweeper on it would stop every
# OTHER rule from expiring too.
_MAX_FLOOD_BACKOFF_SECONDS = 300.0


# #1908: the errors that mean "this chat is gone", not "try again".
#
# ``on_expire`` is a notice, and every registered one sends it to the
# key's chat. When Telegram answers that the chat has migrated to a
# supergroup, that the bot was kicked, or that the chat does not exist,
# the answer is a property of the destination: the next pass gets it
# again, and the pass after that, for as long as the process lives. The
# generic branch below would keep the record so a "transient" failure
# could be retried — but nothing about these three is transient, so the
# record is expired anyway, minus the notice nobody can receive.
#
# ``TelegramMigrateToChat`` is the case that made this necessary and
# the one easiest to miss: it is a SIBLING of ``TelegramBadRequest``,
# not a subclass, so the ``suppress(TelegramBadRequest,
# TelegramForbiddenError)`` every callback already wraps its send in
# does not cover it. A /duel or /cpc seat held in a group that migrates
# to a supergroup therefore raises out of the callback forever.
#
# ``TelegramBadRequest`` is deliberately NOT here. That one is about
# our own payload — a malformed notice is a bug we can deploy a fix
# for — so it keeps the retry-then-quarantine path, where the record
# survives long enough for that deploy to happen.
_UNREACHABLE_CHAT_ERRORS = (
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
)


# #1908: how many consecutive failed attempts one key gets before the
# sweeper stops spending budget on it.
#
# The budget above is charged when the send is ATTEMPTED, which is the
# only correct place — a failed send costs Telegram the same request a
# successful one does. The consequence is that a callback that always
# raises burns a slot on every pass, forever: twenty such keys ahead of
# the healthy ones in scan order spend the whole budget before the
# first live seat is reached, and the sweeper stops expiring anything
# at all while every counter except ``deferred`` still looks normal.
#
# Three attempts is ~90 seconds at the default interval — long enough
# for a genuinely transient failure (a locked database, one bad
# connection) to clear, short enough that a permanent one stops
# competing with live seats before the next pass. The quarantine is
# per-process and per-key and is dropped the moment the key succeeds,
# races, or leaves storage; a deploy that fixes the callback clears it
# by restarting.
_MAX_EXPIRE_FAILURES = 3


def _can_scan(storage: BaseStorage) -> bool:
    """True when :meth:`FsmTimeoutSweeper._iter_storage_keys` can walk it.

    The two supported backends, in the order the reader tries them.
    Split out of that method (#1449) so ``__init__`` can ask the same
    question at BOOT without restating the list — the two answers must
    agree, or the reader's refusal becomes reachable again and the
    loop goes back to swallowing it every thirty seconds.
    """
    return getattr(storage, "iter_keys", None) is not None or isinstance(storage, MemoryStorage)


def _cannot_scan_msg(storage: BaseStorage) -> str:
    """The refusal both :func:`_can_scan` callers raise, worded once."""
    return (
        f"FsmTimeoutSweeper does not know how to iterate "
        f"{type(storage).__name__}; add an iter_keys() "
        f"method on the storage or extend this dispatcher."
    )


def memory_storage_keys(storage: MemoryStorage) -> list[StorageKey]:
    """Snapshot of the keys :class:`MemoryStorage` really holds.

    Not ``storage.storage.keys()`` (#1516). That dict is a
    ``defaultdict`` and aiogram's :class:`FSMContextMiddleware` calls
    ``get_state`` on EVERY update (``aiogram/fsm/middleware.py:42``),
    which materialises an empty record for every ``(chat_id,
    user_id)`` pair that has ever touched the bot and never removes
    one. Both scanners over this storage — :meth:`FsmTimeoutSweeper.
    _iter_storage_keys` and ``scheduler.fsm_busy._iter_keys`` — then
    allocate a key per lifetime user and await a ``get_state`` per
    key, once every sweep interval and, worse, inline on the /cpc and
    /duel accept path under a held match lock. The cost tracked total
    audience rather than live sessions.

    The predicate is deliberately the exact negation of
    ``SQLiteStorage``'s ``_DELETE_IF_EMPTY`` (#195): a record counts as
    present when it has a state OR a non-empty payload. So the two
    backends now answer the same question the same way, including the
    deliberate corner where a handler wrote data without a state — a
    wiring bug both backends keep visible rather than swallow.

    Reading ``record.state`` and ``record.data`` off the dataclass
    directly, instead of through ``await storage.get_state(key)``,
    is what makes this cheap: no coroutine per key, and no
    ``defaultdict`` insert from the scan itself.

    The list IS a snapshot; it protects the caller from the dict
    mutating mid-iteration while a handler runs concurrently.
    """
    return [
        key
        for key, record in list(storage.storage.items())
        if record.state is not None or record.data
    ]


def prune_empty_memory_records(storage: MemoryStorage) -> int:
    """Delete the empty records and return how many went. #1940.

    :func:`memory_storage_keys` stopped the scanners from *paying* for
    the records aiogram's :class:`FSMContextMiddleware` materialises on
    every update (#1516), but nothing ever removed them, and nothing
    ever will: ``MemoryStorage.close`` is a no-op and the dict is a
    ``defaultdict`` whose growth is driven by total audience. A process
    that has seen a hundred thousand ``(chat, user)`` pairs holds a
    hundred thousand ``MemoryStorageRecord``s that mean "no session",
    for the life of the process. Filtering them out of the scan made
    the leak cheap to carry; it did not stop it.

    Deleting them is invisible by construction, and for the same reason
    :meth:`FsmTimeoutSweeper._clear_key` already pops the keys it
    expires: every ``MemoryStorage`` accessor indexes ``self.storage``
    afresh (``aiogram/fsm/storage/memory.py``), so nothing holds a
    reference to the record we drop, and the next read recreates
    exactly the empty one we removed.

    The whole function is synchronous on purpose. No ``await`` means no
    handler runs between the snapshot and the deletes, so a record
    cannot become non-empty in the gap — the emptiness we tested is
    still true when we act on it.

    The predicate is the same one :func:`memory_storage_keys` uses,
    negated, so a record this drops is exactly a record that scan
    considers absent. Anything with a state OR a payload stays, up to
    and including the deliberate corner where a handler wrote data
    without a state.
    """
    empty = [
        key for key, record in storage.storage.items() if record.state is None and not record.data
    ]
    for key in empty:
        del storage.storage[key]
    return len(empty)


@dataclass(frozen=True, slots=True)
class TimeoutRule:
    """Per-state expiry policy.

    M-G-5: ``timeout_seconds`` is the SOLE per-state timeout knob.
    Earlier shapes carried separate ``accept_timeout_seconds`` /
    ``moves_timeout_seconds`` fields with hardcoded state-name
    literals inside ``get_timeout_for_state`` — that coupling silently
    broke if anyone renamed ``RpsStates`` or copied the rule shape to
    a sibling flow. Every state now registers its OWN rule object via
    the ``dict[State, TimeoutRule]`` passed to
    :class:`FsmTimeoutSweeper`; the per-state lookup is the registry,
    not a literal match inside the rule.

    ``on_expire`` is the side-effect the sweeper invokes once when the
    deadline trips. Implementations receive ``(bot, key, data)`` so they
    can notify both seats / drop inline keyboards / log; the sweeper
    itself clears the FSM AFTER the callback returns so a crashing
    callback leaves state intact and the next sweep retries.

    #1908 puts two bounds on that retry. A raise saying the chat is
    unreachable — kicked, migrated, gone — clears the record anyway,
    because "retry until the notice lands" cannot terminate when the
    destination is what is broken. And any other repeated failure is
    counted: after ``max_expire_failures`` consecutive ones the
    sweeper stops attempting the key at all, so one broken callback
    cannot spend the pass's whole send budget on itself.

    #1515: ``on_expire`` MUST be idempotent. "Once" above is the
    intent, not a guarantee this class can make — the only thing
    standing between one call and two is the clear that follows it,
    and the clear is a storage write that can fail. When it does, the
    record keeps the same state and the same (by then older) stamp, so
    the next pass re-derives the same expiry, passes both race
    re-reads and calls the callback a second time. Every registered
    callback today only sends a message, so the visible cost is a
    duplicate notice; the first one that moves coins in this shape
    would pay out twice, and this sentence is what stands between the
    two.

    ``guard`` (#126) is an optional per-key async context manager the
    sweeper enters around the whole expire-and-clear region. A flow
    whose handlers already serialise on something — /cpc and /duel
    hold a per-match :class:`asyncio.Lock` — hands that same lock over
    here, so an accept click and a deadline can no longer both believe
    they own the match: whichever takes the lock first wins outright,
    and the loser sees the state the winner left. Rules that need no
    serialisation leave it ``None`` and the sweeper runs bare, exactly
    as before.

    The trade is that the lock is held across the callback's Telegram
    I/O. That is the point — a click arriving mid-expiry should wait
    and then be told the truth ("match not found"), not act on a
    snapshot the sweeper is in the middle of invalidating.

    #1489: the sweeper will not wait forever to take it. Acquisition
    is bounded by ``_GUARD_ACQUIRE_TIMEOUT_SECONDS``; a guard it cannot
    take in that time leaves the key untouched and counted in
    :attr:`SweepReport.skipped`. Once taken, the hold across the
    callback is NOT bounded — see the constant for why the two halves
    are treated differently.

    #1487: ``timeout_seconds`` is a FLOOR, not a schedule. The sweeper
    polls, so a deadline that trips one millisecond after a pass is not
    noticed until the next one: the observed lifetime of a state is
    ``[timeout_seconds, timeout_seconds + interval_seconds]``. The
    module docstring's "wall-clock budget" means the budget after which
    the state BECOMES eligible, never the instant it is cleared. Before
    #1487 the upper bound was worse still — the loop slept the full
    interval AFTER each pass, so the period was ``interval + pass
    duration`` rather than ``interval``; :meth:`FsmTimeoutSweeper.run`
    now subtracts the pass it just ran.

    There is deliberately no "and now drop your own bookkeeping" hook
    to go with it. #126 briefly had one, because the /cpc and /duel
    match-lock registries needed their slot popped at exactly the right
    instant — after the clear, still inside the guard — and neither
    ``on_expire`` nor the guard's ``__aexit__`` is that instant. The
    answer turned out to be a registry that never needs telling
    (:class:`~telegram_invite_bot.utils.keyed_locks.KeyedLocks`,
    refcounted), so the hook had no callers left and the sweeper does
    not have to know that flows keep bookkeeping at all.
    """

    on_expire: Callable[[Bot, StorageKey, dict[str, Any]], Awaitable[None]]
    timeout_seconds: int
    guard: Callable[[Bot, StorageKey], AbstractAsyncContextManager[Any]] | None = None

    def __post_init__(self) -> None:
        """Reject non-positive timeouts at construction time."""
        if self.timeout_seconds <= 0:
            raise ValueError("TimeoutRule.timeout_seconds must be a positive integer")


@dataclass(frozen=True, slots=True)
class SweepReport:
    """Single-pass result, for observability + tests.

    ``scanned`` is the number of state-bearing keys the sweeper looked
    at; ``expired`` is the subset that tripped a rule and got cleared.
    ``errors`` counts keys the pass could not finish. Until #1515 that
    meant only an ``on_expire`` callback that raised; it now also
    covers a storage read, a guard entry and the clear itself, all of
    which used to escape the loop entirely instead of being counted.
    Surfaced separately so a healthy sweep with one bad seat is
    distinguishable from a totally broken pass.

    ``skipped`` counts keys the pass declined to even try, because the
    rule's guard was still held when
    ``_GUARD_ACQUIRE_TIMEOUT_SECONDS`` ran out (#1489). Unlike
    ``errors`` this is not a malfunction — somebody else legitimately
    owns that match right now — but a number that stays high across
    passes says a lock is being held far longer than a Telegram round
    trip, which used to be invisible because the sweeper simply waited.

    ``raced`` counts keys whose state *or deadline* changed between the
    scan and the moment the sweeper took the rule's guard — a player
    got there first, so the expiry was abandoned untouched. In a
    healthy process this is 0; a non-zero number is the honest signal
    that a click and a deadline arrived at the same instant, and it is
    the metric to watch if timeout budgets ever get tightened. Defaults
    to 0 so existing call sites and their equality assertions keep
    reading naturally.

    #258 widened this from "state changed" to "state or deadline
    changed", which makes the counter noisier: a legitimate mid-match
    restamp now shows up here. That is correct — those *were* races the
    sweeper lost, and before the fix they were being silently
    mis-resolved as expiries.

    #837 added a SECOND place that increments it: the same comparison
    runs again after ``on_expire`` returns, before the clear. A key
    counted there is not quite "abandoned untouched" — the callback
    already ran, so the user got the "timed out" notice and then kept
    their session. That is the lesser of the two wrongs (the
    alternative is deleting a flow the user just answered), but it
    means a non-zero ``raced`` can now include keys that were
    half-expired rather than not expired at all.

    ``deferred`` counts keys that were due to expire and that the pass
    deliberately did not touch (#1767): either the per-pass expiry
    budget was already spent, or Telegram answered a 429 and the pass
    stopped sending. A deferred key is untouched — no callback, no
    clear — so the next pass finds exactly the record this one saw. A
    number that never returns to 0 means the backlog is growing faster
    than the budget drains it.

    ``poisoned`` counts due keys the pass refused to attempt because
    their ``on_expire`` had already failed ``_MAX_EXPIRE_FAILURES``
    times in a row (#1908). Unlike ``deferred`` these are not coming
    back next pass: the sweeper has given up on them for the life of
    the process, so they no longer compete for the budget with keys it
    can still serve. The number is the honest count of sessions that
    are stuck busy with nobody to tell — it should be 0, and a
    non-zero one is a callback bug to deploy a fix for, not a backlog
    to wait out.

    ``retry_after`` is the backoff Telegram asked for, in seconds, and
    is non-zero only on a pass that a 429 cut short. :meth:`run` sleeps
    it off before the next tick; ``sweep_once`` itself never sleeps it,
    so a test can assert on the number without waiting for it.

    ``pruned`` counts the empty :class:`MemoryStorage` records the pass
    reclaimed (#1940) — see :func:`prune_empty_memory_records`. It is
    always 0 on a persistent backend, which has no such records, and on
    a healthy memory process it is roughly "new users since the last
    pass". A number that stays large every pass is not a malfunction:
    it is the growth rate the sweeper is now absorbing, and the honest
    argument for moving that deployment to ``FSM_BACKEND=sqlite``.
    """

    scanned: int
    expired: int
    errors: int
    raced: int = 0
    skipped: int = 0
    deferred: int = 0
    poisoned: int = 0
    retry_after: float = 0.0
    pruned: int = 0


class FsmTimeoutSweeper:
    """Background task that expires stale FSM sessions."""

    def __init__(
        self,
        storage: BaseStorage,
        bot: Bot,
        *,
        rules: dict[State, TimeoutRule],
        interval_seconds: float = 30.0,
        guard_timeout_seconds: float = _GUARD_ACQUIRE_TIMEOUT_SECONDS,
        expire_budget: int = _EXPIRE_BUDGET_PER_PASS,
        expire_pace_seconds: float = _EXPIRE_PACE_SECONDS,
        max_expire_failures: int = _MAX_EXPIRE_FAILURES,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        # Rules are indexed by State (not the ``.state`` string) so a
        # typo in handler wiring fails at import time, not silently at
        # sweep time. Translate once here to avoid the lookup per key
        # in the hot path.
        self._rules_by_state_name: dict[str, TimeoutRule] = {
            state.state: rule for state, rule in rules.items() if state.state is not None
        }
        if len(self._rules_by_state_name) != len(rules):
            # Defensive — a State() with no .state attribute set means
            # someone forgot the StatesGroup wiring. Loud at boot.
            raise ValueError("every TimeoutRule key must carry a .state string")
        if not _can_scan(storage):
            # #1449: refuse an unwalkable backend HERE. The reader's
            # own refusal is raised by the FIRST statement of
            # ``sweep_once``, so it lands in ``run``'s defensive
            # ``except Exception`` and is retried on the next tick,
            # and every tick after that, indefinitely. The task stays
            # alive and the health check stays green while not one of
            # the registered states ever expires — held /duel and
            # /cpc stakes are never returned, and every busy-gated
            # flow stays cross-blocked — with a full traceback in the
            # journal twice a minute as the only signal that anything
            # is wrong. A storage the sweeper cannot walk is a
            # CONFIGURATION error, not a transient one, and the
            # honest place to answer one is boot: this propagates out
            # of ``Application.start_background``, which builds the
            # sweeper before it spawns the task, so the process never
            # comes up half-working. Same posture as the rules check
            # just above.
            raise TypeError(_cannot_scan_msg(storage))
        self._storage = storage
        self._bot = bot
        self._interval_seconds = interval_seconds
        # #1489: ceiling on taking a rule's guard, not on holding it.
        self._guard_timeout_seconds = guard_timeout_seconds
        # #1767: ceiling and spacing on the pass's OUTBOUND fan-out.
        self._expire_budget = expire_budget
        self._expire_pace_seconds = expire_pace_seconds
        # #1908: consecutive ``on_expire`` failures per key, so a
        # callback that can never succeed stops competing for the
        # budget. Pruned every pass to the keys storage still holds.
        self._max_expire_failures = max_expire_failures
        self._expire_failures: dict[StorageKey, int] = {}
        self._clock = clock

    async def run(self) -> None:
        """Loop forever until cancelled. Production entry point.

        Wraps :meth:`sweep_once` in a defensive try/except so a
        transient sweep failure doesn't kill the task — a dead sweeper
        is the second-worst outcome (after a money-leak), and the
        observability story is just "WARNING in logs".

        #1487: the sleep is measured against the monotonic clock, not
        added to it. The loop used to sleep the whole interval AFTER
        the pass, so the real period was ``interval + pass duration``
        and every pass pushed the next one further out — a drift that
        compounds, and one the docstrings never admitted to. Subtracting
        the pass makes the PERIOD the interval. It does not, and cannot,
        make the interval an expiry precision: see the paragraph on
        :class:`TimeoutRule` for what ``timeout_seconds`` actually
        promises.

        A pass longer than the interval yields ``max(0.0, ...)`` — a
        zero sleep rather than a negative one, i.e. run again at once
        rather than try to claw back time that is gone. There is no
        catch-up burst: consecutive missed slots are not queued.
        """
        log.info(
            "sweeper started: interval={interval}s rules={rules}",
            interval=self._interval_seconds,
            rules=list(self._rules_by_state_name.keys()),
        )
        try:
            while True:
                started = time.monotonic()
                try:
                    report = await self.sweep_once()
                    if (
                        report.expired
                        or report.errors
                        or report.raced
                        or report.skipped
                        or report.deferred
                        or report.poisoned
                        # #1940: ``pruned`` is deliberately NOT in this
                        # condition. On a ``MemoryStorage`` deployment it
                        # is non-zero on almost every pass — that is what
                        # "one record per passer-by" means — so triggering
                        # on it would turn a log that speaks only when
                        # something happened into a line every thirty
                        # seconds. It is reported below, where the pass
                        # already had something to say.
                    ):
                        log.bind(
                            scanned=report.scanned,
                            expired=report.expired,
                            errors=report.errors,
                            raced=report.raced,
                            skipped=report.skipped,
                            deferred=report.deferred,
                            poisoned=report.poisoned,
                            pruned=report.pruned,
                            retry_after=report.retry_after,
                        ).info("sweep pass")
                    if report.retry_after > 0.0:
                        # #1767: Telegram told us to back off. Wait it
                        # out HERE, inside the ``elapsed`` measurement
                        # below, so the backoff is charged against the
                        # interval instead of stacking on top of it —
                        # and so the next pass cannot start sending
                        # again while the flood wait is still running.
                        log.bind(retry_after=report.retry_after).warning(
                            "backing off after a flood wait",
                        )
                        await asyncio.sleep(report.retry_after)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — defensive top-level
                    log.exception("sweep pass failed; continuing")
                # #1487: charge the pass against the interval instead of
                # stacking on top of it. The failure branch above is
                # inside the same measurement on purpose — an exception
                # is not a reason to lengthen the period.
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, self._interval_seconds - elapsed))
        except asyncio.CancelledError:
            log.info("sweeper cancelled; exiting cleanly")
            raise

    async def sweep_once(self) -> SweepReport:
        """One pass over the storage. Deterministic given ``self._clock``.

        #1767: the pass is bounded on two axes. It attempts at most
        ``expire_budget`` sends and reports the rest as ``deferred``
        (they keep their records and come back next pass), and it waits
        ``expire_pace_seconds`` between sends so a hundred simultaneous
        deadlines do not become a hundred-message burst. A
        :class:`TelegramRetryAfter` aborts the pass outright and is
        handed back in ``retry_after`` for :meth:`run` to sleep off.

        #1908: the budget is charged per ATTEMPT, so a callback that
        always raises used to spend a slot on every pass forever. Two
        things now stop that. A failure that says the chat is
        unreachable clears the record anyway — there is nobody left to
        notify — and any other failure is charged a strike, after
        ``max_expire_failures`` of which the key is quarantined for the
        life of the process and reported as ``poisoned`` instead of
        competing for the budget with keys the sweeper can still serve.
        """
        # #1940: reclaim BEFORE the snapshot, so the walk below sees an
        # already-clean dict. Nothing else in the process ever removes
        # these records, and this pass is the only place that can be
        # sure no handler is mid-flight while it looks.
        pruned = 0
        if isinstance(self._storage, MemoryStorage):
            pruned = prune_empty_memory_records(self._storage)
        prefetched = await self._prefetch_records()
        keys = list(prefetched) if prefetched is not None else await self._iter_storage_keys()
        now = self._clock()
        scanned = 0
        expired = 0
        errors = 0
        raced = 0
        skipped = 0
        deferred = 0
        poisoned = 0
        retry_after = 0.0
        # Sends ATTEMPTED, which is what the rate limiter counts — not
        # ``expired``, which only counts the ones that also cleared.
        attempted = 0
        aborted_at: int | None = None
        for index, key in enumerate(keys):
            # #1515: one bad key must not end the pass. Only the
            # ``on_expire`` call used to sit inside a ``try``; the two
            # storage reads, the guard entry, the two race re-reads and
            # -- worst of all -- the clear below ran bare, so a single
            # ``database is locked`` from ``SQLiteStorage`` threw out of
            # the ``for`` and abandoned every key the pass had not
            # reached yet. The task itself survived (``run`` catches one
            # level up), which is exactly why it was invisible.
            #
            # A failed clear is the dangerous half: the record keeps the
            # same state and the same, now older, stamp, so the next
            # pass re-derives the same expiry, passes both race
            # re-reads and calls ``on_expire`` AGAIN. See the
            # idempotency contract on :class:`TimeoutRule`.
            try:
                if prefetched is None:
                    state_name = await self._storage.get_state(key)
                else:
                    state_name = prefetched[key][0]
                if state_name is None:
                    continue
                rule = self._rules_by_state_name.get(state_name)
                if rule is None:
                    continue
                scanned += 1
                if prefetched is None:
                    data = await self._storage.get_data(key)
                else:
                    data = prefetched[key][1]
                entered_at = self._parse_entered_at(data)
                if entered_at is None:
                    # Defensive: a state without the stamp is an invariant
                    # violation by the handler that set it. Log + skip
                    # rather than expire-immediately (that would punish
                    # users for our own wiring bug).
                    log.bind(state=state_name, key=str(key)).warning(
                        "FSM state without state_entered_at; skipping",
                    )
                    continue
                age = (now - entered_at).total_seconds()
                if age < rule.timeout_seconds:
                    continue
                # #1908: the key is due and the sweeper has given up on
                # it. Checked BEFORE the budget, which is the whole
                # point: a quarantined key that still consumed a slot
                # would go on starving the healthy ones exactly as it
                # did before the quarantine existed.
                if self._expire_failures.get(key, 0) >= self._max_expire_failures:
                    poisoned += 1
                    continue
                # #1767: the key is due, but the pass may have already
                # sent as much as it is allowed to. Defer BEFORE the
                # guard, so a deferred key costs neither a lock nor a
                # storage read and the next pass sees the untouched
                # record it left behind.
                if attempted >= self._expire_budget:
                    deferred += 1
                    continue
                # Space the sends. The wait goes here, before the send
                # rather than after it, so the pass never pays for a
                # gap after its last message.
                if attempted and self._expire_pace_seconds > 0:
                    await asyncio.sleep(self._expire_pace_seconds)
                # #126: everything from here down runs under the rule's
                # guard (a no-op for rules that declare none). For /cpc and
                # /duel that guard is the same per-match lock the accept /
                # decline / move handlers take, so a click can no longer
                # slip between the callback and the clear below.
                async with self._guarded(rule, key) as acquired:
                    if not acquired:
                        # #1489: somebody else holds the match right
                        # now and would not let go in time. Nothing has
                        # happened yet, so leaving is free and the next
                        # pass retries the same key.
                        log.bind(
                            state=state_name,
                            key=str(key),
                            waited=self._guard_timeout_seconds,
                        ).warning("guard still held; skipping this key")
                        skipped += 1
                        continue
                    # Re-read under the guard. We may have queued behind an
                    # accept that landed a millisecond before the deadline;
                    # if it did, the state we scanned is no longer the state
                    # in storage, and expiring it would delete a match a
                    # player just started — both seats left holding move
                    # keyboards that answer "match not found" forever.
                    #
                    # #258: the state NAME is not the whole deadline. A
                    # handler may refresh ``state_entered_at`` while staying
                    # in the same state — the ``/duel`` roll handler does
                    # exactly that between best-of-N rounds, and
                    # ``handlers.support.cmd_support`` on a re-prompt.
                    # Comparing only the name expired a match whose clock
                    # had just been reset one await ago, destroying the
                    # running score. Worse, the guard *widens* the window:
                    # having decided to expire, the sweeper blocks on the
                    # very lock the roll handler holds across two Telegram
                    # round trips. So re-read the stamp here too, under
                    # the guard, and re-test it against the same timeout.
                    current = await self._storage.get_state(key)
                    fresh = await self._storage.get_data(key)
                    deadline_holds = self._deadline_holds(fresh, rule, now)
                    if current != state_name or deadline_holds:
                        log.bind(
                            expired_state=state_name,
                            current=current,
                            key=str(key),
                            deadline_holds=deadline_holds,
                        ).warning(
                            "state changed under expiry; leaving it alone",
                        )
                        raced += 1
                        # #1908: whatever is in this key now is not what
                        # the earlier attempts failed on, so it does not
                        # inherit their strikes.
                        self._expire_failures.pop(key, None)
                        continue
                    # #1908: charged HERE, before the call and whatever
                    # it answers, because the budget bounds requests to
                    # Telegram and a failed request costs the same one
                    # as a successful one. That is also why a callback
                    # that always raises needs the quarantine above:
                    # counting only successes would let a hundred failing
                    # sends fly per pass instead.
                    attempted += 1
                    try:
                        # Hand the callback the POST-lock snapshot: the
                        # pre-lock ``data`` can be a whole round out of date,
                        # and ``_expire_duel`` renders its card from this
                        # dict.
                        await rule.on_expire(self._bot, key, fresh)
                    except TelegramRetryAfter as exc:
                        # #1767: a flood wait is about the BOT, not this
                        # key, so retrying the rest of the pass would
                        # only collect more 429s. Stop sending, leave
                        # every remaining record untouched, and hand
                        # ``run`` the backoff Telegram asked for.
                        #
                        # This must be caught ABOVE the generic handler
                        # below: ``TelegramRetryAfter`` is an ordinary
                        # ``Exception``, and the generic branch's
                        # "leave the state intact and let the next pass
                        # retry" is exactly the behaviour that turns one
                        # flood wait into a fan-out re-sent every
                        # ``interval_seconds`` for as long as it lasts.
                        retry_after = min(
                            max(float(exc.retry_after), 0.0), _MAX_FLOOD_BACKOFF_SECONDS
                        )
                        log.bind(
                            state=state_name,
                            key=str(key),
                            retry_after=retry_after,
                        ).warning("flood wait from Telegram; aborting the pass")
                        errors += 1
                        aborted_at = index
                        break
                    except _UNREACHABLE_CHAT_ERRORS as exc:
                        # #1908: the notice cannot be delivered — not
                        # now and not on any later pass, because the
                        # chat itself is gone (migrated, kicked, or
                        # never there). Falling through to the clear
                        # below is the whole branch: the record has no
                        # value left once nobody can be told about it,
                        # and keeping it means one seat that stays busy
                        # forever plus one budget slot burnt every
                        # thirty seconds for as long as the process
                        # lives. Counted in ``errors`` as well as in
                        # ``expired`` so the two together read as "we
                        # cleared it, and the user never heard".
                        log.bind(
                            state=state_name,
                            key=str(key),
                            error=type(exc).__name__,
                        ).warning("expiry notice undeliverable; clearing anyway")
                        errors += 1
                    except Exception:  # noqa: BLE001 — must not kill the loop
                        log.exception(
                            "on_expire callback raised; FSM left intact (state={state}, key={key})",
                            state=state_name,
                            key=str(key),
                        )
                        errors += 1
                        # Don't clear the state — let the next pass retry.
                        # Stops a perma-broken callback from quietly wiping
                        # match data nobody can recover.
                        #
                        # #1908: bounded, though. The retry above is for
                        # a failure that can plausibly stop happening; a
                        # callback that raises on every pass is charged
                        # a strike here and, once it has
                        # ``_max_expire_failures`` of them, stops being
                        # attempted at all so the keys behind it in scan
                        # order get their turn.
                        strikes = self._expire_failures.get(key, 0) + 1
                        self._expire_failures[key] = strikes
                        if strikes >= self._max_expire_failures:
                            log.bind(
                                state=state_name,
                                key=str(key),
                                strikes=strikes,
                            ).error("on_expire keeps failing; giving up on this key")
                        continue
                    # #837: check ONE more time, now that the callback has
                    # returned. The guard is optional and most registered
                    # rules leave it ``None`` (``app.py`` wires guards only
                    # for the two /cpc and the two /duel states; the other
                    # nineteen run bare). For those, nothing serialised the
                    # Telegram round trip ``on_expire`` just made, and the
                    # user is perfectly able to answer the very prompt it
                    # sent — the two writes below would then delete the
                    # state that answer created. That is the same failure
                    # the re-read above prevents, one step later in the
                    # sequence, and the guarded rules are the only ones it
                    # was ever prevented for.
                    #
                    # Comparing the same way is sound because no registered
                    # callback touches FSM storage: not one of the
                    # registered ``on_expire`` functions calls
                    # ``set_state`` / ``set_data`` / ``update_data``, so
                    # anything that moved here was a handler, not us. A
                    # callback that starts mutating state would read as a
                    # race against itself and its own expiry would stop
                    # clearing — noisy, not dangerous, and the ``raced``
                    # counter says so.
                    #
                    # This narrows the window rather than closing it: from
                    # "one Telegram round trip" down to "two storage reads".
                    # Closing it outright needs a compare-and-swap that
                    # :class:`BaseStorage` does not offer, and a
                    # SQLite-only one would leave ``MemoryStorage`` — which
                    # every test and any ``FSM_BACKEND=memory`` deploy runs
                    # on — with no protection at all.
                    after_state = await self._storage.get_state(key)
                    after_data = await self._storage.get_data(key)
                    if after_state != state_name or self._deadline_holds(after_data, rule, now):
                        log.bind(
                            expired_state=state_name,
                            current=after_state,
                            key=str(key),
                        ).warning(
                            "state changed while on_expire ran; leaving it alone",
                        )
                        raced += 1
                        self._expire_failures.pop(key, None)
                        continue
                    # Clear AFTER the callback succeeds. Order matters: a
                    # crashing callback must leave the state intact for the
                    # retry above. The callback itself already holds ``fresh``
                    # by value, so the copy it quotes is never the empty dict.
                    await self._clear_key(key)
                    expired += 1
                    # #1908: the strikes are CONSECUTIVE ones. A key that
                    # got through drops whatever it had collected, so a
                    # flaky callback is never quarantined for failures it
                    # already recovered from.
                    self._expire_failures.pop(key, None)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — one key must not end the pass
                log.exception(
                    "sweep step failed; skipping this key (key={key})",
                    key=str(key),
                )
                errors += 1
        if aborted_at is not None:
            # Everything after the aborted key was left unexamined, so
            # report it as deferred rather than pretending the pass
            # covered the whole storage.
            deferred += len(keys) - aborted_at - 1
        # #1908: the quarantine is bookkeeping about keys, so it must not
        # outlive them. A record that was cleared, expired elsewhere or
        # simply never came back would otherwise keep its strikes in this
        # dict for the life of the process, and the dict would grow one
        # entry per broken session forever.
        if self._expire_failures:
            live = set(keys)
            self._expire_failures = {
                key: strikes for key, strikes in self._expire_failures.items() if key in live
            }
        return SweepReport(
            scanned=scanned,
            expired=expired,
            errors=errors,
            raced=raced,
            skipped=skipped,
            deferred=deferred,
            poisoned=poisoned,
            retry_after=retry_after,
            pruned=pruned,
        )

    async def _clear_key(self, key: StorageKey) -> None:
        """Wipe one record, preferring a backend's own one-shot clear.

        The portable way to empty a key is ``set_state(None)`` then
        ``set_data({})`` — what :meth:`FSMContext.clear` does
        (``aiogram/fsm/context.py:42-44``) — and it is what
        :class:`MemoryStorage` gets, because nothing in
        :class:`BaseStorage` promises anything better.

        #1516: those two writes leave the record BEHIND, holding
        ``state=None`` and an empty dict. That is precisely the row
        :class:`~telegram_invite_bot.fsm.sqlite_storage.SQLiteStorage`
        deletes (#195, ``_DELETE_IF_EMPTY``), so keeping it made the
        two backends disagree about whether a finished flow still
        exists. Drop it after the portable pair, so
        :func:`memory_storage_keys` and the sibling scan in
        ``scheduler.fsm_busy`` see the same key set either way.

        A disk-backed store can do it in one statement, and should:
        two commits are two fsyncs, and a crash landing between them
        leaves a stateless row still holding its payload. This sweeper
        is precisely the code that would then skip that row forever,
        since :meth:`sweep_once` treats ``state is None`` as "nothing
        to do". So dispatch on the capability, the same ``getattr``
        shape :meth:`_iter_storage_keys` uses rather than an
        ``isinstance`` on a backend this module should not have to
        import.
        """
        clear = getattr(self._storage, "clear", None)
        if callable(clear):
            await clear(key)
            return
        await self._storage.set_state(key, None)
        await self._storage.set_data(key, {})
        if isinstance(self._storage, MemoryStorage):
            # Safe against a concurrent handler: ``.storage`` is a
            # ``defaultdict``, so the next read recreates exactly the
            # empty record the two writes above would have left.
            self._storage.storage.pop(key, None)

    @contextlib.asynccontextmanager
    async def _guarded(self, rule: TimeoutRule, key: StorageKey) -> AsyncIterator[bool]:
        """Take the rule's guard under a deadline; yield whether we hold it (#1489).

        Yields ``True`` inside the guard and ``False`` — outside it, and
        having entered nothing — when the acquisition ran out of time.
        The caller must check, because a ``False`` body that proceeded
        would be doing exactly what the guard exists to prevent.

        The ceiling wraps ``__aenter__`` and nothing else, which is why
        this is spelled out by hand rather than as an
        ``async with asyncio.timeout(...): async with cm:``. That shape
        puts the BODY under the same deadline, so the callback would be
        cancelled mid-Telegram-call — a side effect that cannot be taken
        back, against an acquisition that can be abandoned for free.

        On the success path the entered manager is handed to an
        :class:`~contextlib.AsyncExitStack` rather than exited by hand,
        so an exception raised in the body still reaches ``__aexit__``
        with the real ``(type, value, tb)`` triple.
        """
        cm = self._guard_for(rule, key)
        try:
            async with asyncio.timeout(self._guard_timeout_seconds):
                await cm.__aenter__()
        except TimeoutError:
            yield False
            return
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_exit(cm)
            yield True

    def _guard_for(self, rule: TimeoutRule, key: StorageKey) -> AbstractAsyncContextManager[Any]:
        """The rule's per-key guard, or a no-op for rules without one."""
        if rule.guard is None:
            return contextlib.nullcontext()
        return rule.guard(self._bot, key)

    async def _prefetch_records(self) -> dict[StorageKey, tuple[str, dict[str, Any]]] | None:
        """Every rule-covered record in ONE storage round trip, or ``None``.

        #1451. Candidate selection used to cost ``1 + N`` reads
        per pass (``1 + 2N`` when most keys were in a covered
        state): one :meth:`_iter_storage_keys`, then a
        ``get_state`` for every key in the store and a
        ``get_data`` for every key that survived it. Under
        ``FSM_BACKEND=sqlite`` all of those serialise on the one
        connection :class:`fsm.sqlite_storage.SQLiteStorage`
        holds for the whole process, which is also the connection
        every routed update's own ``get_state`` uses -- so the
        pass competed directly with user-facing latency, and its
        cost scaled with rows the sweeper could never act on.

        A storage that offers ``iter_records(states)`` answers the
        whole question in one statement. ``None`` means it does
        not, and the caller falls back to the key-by-key walk --
        which is what :class:`MemoryStorage` gets, where every
        one of those reads is a dict lookup and the round-trip
        count is not a cost at all.

        Duck-typed on the method rather than an ``isinstance`` for
        the same reason :meth:`_iter_storage_keys` is: a future
        Redis or Postgres backend that adopts the convention gets
        the fast path without touching this module. It is a pure
        OPTIMISATION and deliberately NOT part of
        :func:`_can_scan`: a backend without it still sweeps
        correctly, so making it a boot requirement would reject
        working configurations.

        What this must never absorb is the re-read under the
        rule's guard (#258) or the one after ``on_expire``
        returns (#837). Both exist precisely because the
        snapshot goes stale, and both stay per-key reads at the
        moment of the decision; only the filtering reads move
        here.
        """
        iter_records = getattr(self._storage, "iter_records", None)
        if iter_records is None:
            return None
        records = await iter_records(tuple(self._rules_by_state_name))
        return {key: (state, data) for key, state, data in records}

    async def _iter_storage_keys(self) -> list[StorageKey]:
        """Return a snapshot of every :class:`StorageKey` known to the
        backing store.

        Two backends ship today:

        * :class:`MemoryStorage` — we reach into ``.storage`` directly
          because aiogram doesn't expose a public iter API (see the
          module docstring's "Storage scanning" section), through
          :func:`memory_storage_keys`, which snapshots the dict and
          drops the empty records aiogram's own middleware leaves
          behind for every user the bot has ever seen (#1516).
        * :class:`SQLiteStorage` (T-012) — exposes an async
          ``iter_keys()`` that returns a materialised list (one short
          read query, lock released before we start scanning state).
          Detected by duck-typing on the attribute rather than an
          ``isinstance`` so a future backend (Redis, Postgres) that
          adopts the same convention works without touching this
          method.

        Async because the SQLite branch needs to ``await`` the query;
        the memory branch returns a fully-built list and just gets
        wrapped in the same coroutine for shape symmetry.
        """
        iter_keys = getattr(self._storage, "iter_keys", None)
        if iter_keys is not None:
            # Duck-typed: any storage that ships ``async def
            # iter_keys() -> Sequence[StorageKey]`` works. The
            # ``list`` wrap accepts whatever sequence the storage
            # returns (tuple, list, deque, ...).
            return list(await iter_keys())
        if isinstance(self._storage, MemoryStorage):
            return memory_storage_keys(self._storage)
        # Unreachable through the constructor since #1449, which asks
        # :func:`_can_scan` the same question at boot. Kept because
        # the day the two disagree is the day ``run`` goes back to
        # swallowing this raise every thirty seconds forever, and a
        # test pins that it still fires on a storage swapped in after
        # construction.
        raise TypeError(_cannot_scan_msg(self._storage))

    def _deadline_holds(self, data: dict[str, Any], rule: TimeoutRule, now: datetime) -> bool:
        """True when ``data``'s stamp does NOT authorise expiring the key.

        Shared by the two race checks in :meth:`sweep_once` — the one
        taken after the guard (#258) and the one taken after the
        callback (#837) — so the two can never drift apart.

        Two ways a record defends itself. Either its stamp puts the
        deadline back in the future (a handler restamped: the /duel roll
        handler does that between rounds, ``handlers.support.cmd_support``
        on a re-prompt), or there is no parsable stamp at all.

        #1488: that second answer used to be ``False``, i.e. "go ahead
        and expire", which contradicted the scan twenty lines up, where
        a missing stamp means "fresh session, leave alone". The
        contradiction was reachable and it deleted live flows. Eleven
        handlers — ``checks.py``, ``rps.py``, ``groupadmin.py`` (twice),
        ``shop.py``, ``p2p_trade.py`` (twice), ``broadcast.py``,
        ``support.py``, ``withdraw.py``, ``p2p.py`` — call ``set_state``
        FIRST and stamp with a second write right after. A user who
        re-enters the SAME state through :meth:`FSMContext.clear` (which
        empties the data dict) is, for the width of one await, sitting
        on a record whose state matches what the sweeper scanned and
        whose stamp is gone. The old answer expired that brand-new flow
        and handed ``on_expire`` an empty dict to render from.

        The two readings are now one, and it is the scan's: no stamp
        means hands off. The cost of choosing that side is that a record
        which somehow loses its stamp permanently never expires — but
        the scan already refuses those, loudly, every single pass, so
        this only makes the sweeper self-consistent about a key it was
        never going to reap anyway.

        ``now`` is the sweep's frozen clock, not a fresh reading. A
        handler that restamped after the pass began therefore lands in
        the future and the subtraction goes negative, which is exactly
        the answer we want.
        """
        entered_at = self._parse_entered_at(data)
        if entered_at is None:
            return True
        return (now - entered_at).total_seconds() < rule.timeout_seconds

    @staticmethod
    def _parse_entered_at(data: dict[str, Any]) -> datetime | None:
        raw = data.get(STATE_ENTERED_AT_FIELD)
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        # The handlers stamp tz-aware UTC; defensively normalise so a
        # tz-naive value (e.g. from a stale record migrated in
        # mid-deploy) doesn't crash arithmetic against ``self._clock``.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed


def utc_now_iso() -> str:
    """Helper used by handlers when stamping :attr:`STATE_ENTERED_AT_FIELD`.

    Centralised so a future move to a monotonic clock / mockable time
    source happens in one place. Today it's just ``datetime.now(UTC)``
    rendered to ISO 8601, which is what :meth:`FsmTimeoutSweeper.
    _parse_entered_at` knows how to round-trip.
    """
    return datetime.now(UTC).isoformat()


__all__ = [
    "STATE_ENTERED_AT_FIELD",
    "FsmTimeoutSweeper",
    "SweepReport",
    "TimeoutRule",
    "memory_storage_keys",
    "prune_empty_memory_records",
    "utc_now_iso",
]
