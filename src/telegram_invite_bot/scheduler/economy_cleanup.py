"""Periodic economy housekeeping sweep (L-26 + L-95).

Reaping jobs on one hourly loop — expired inventory, stale game-play
stamps, stale pending P2P trades, expired checks, and the VIP
expiry-warning DM (see ``sweep_once``). The original two:

1. **Expired inventory** (L-26 headline). Legacy ran an hourly task
   (``cleanup_expired_inventory``, ``bot.py:13973``) that MARKED
   expired ``economy.inventory`` rows ``used=1`` rather than deleting
   them; we hard-delete instead — a deliberate divergence documented on
   :meth:`InventoryRepo.delete_expired`. The new ``/inventory`` reader
   already *hides* expired rows (``InventoryRepo.list_for_user`` filters
   ``expires > now``), so the sweep is pure table hygiene — without it
   dead entitlements accumulate forever and bloat the table the
   ``/inventory`` and ``/use`` queries scan.

2. **Stale game-play stamps** (companion to L-25). The persistent
   anti-abuse counter (``economy.game_plays``) only ever reads a rolling
   24h window, so rows older than that can never affect a future cap
   decision. Pruning them on the same sweep keeps ``game_plays`` bounded
   by ``active_players × MAX_PER_DAY`` instead of growing without limit.
   A generous 48h cutoff (2× the widest window) leaves a safety margin
   against clock skew while still bounding the table.

3. **Stale-withdrawal alert** (#169, #281). Payouts are manual — the
   operator pays out externally and then presses ✅ on
   ``/admin_withdrawals`` — so ``pending`` is a queue worked by hand,
   and nothing anywhere used to notice when that hand stopped.
   Production carried one ``pending`` request for five months in total
   silence. The sweep now DMs the owner the first time a request
   crosses
   :data:`~telegram_invite_bot.utils.economy.STALE_WITHDRAWAL_AGE`.
   This is the push half of a signal whose pull half (age + ⚠️ on the
   card) lives in ``handlers/admin/withdrawals``; both read the same
   threshold constant so they can never disagree.

Shape mirrors :mod:`telegram_invite_bot.scheduler.fsm_sweeper` — the
strangler's established background-task pattern:

* **Pure ``sweep_once`` core.** Opens one ``economy.db`` session, runs
  both deletes inside it, commits once. Injectable ``clock`` so tests pin
  the boundary. Returns an :class:`EconomyCleanupReport` so observability
  can answer "how many rows did we reap" without re-deriving it.
* **Async ``run`` loop on top.** Owns cancellation: one
  ``CancelledError`` exits cleanly. A transient sweep failure is logged
  and the loop continues — a dead cleaner is a slow leak, not a money
  bug, so it must never take the task down.

Registration (interval + callback) lives in ``app.py``, where the
background tasks are started. This module owns only the task
*function*; it does NOT touch ``app.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramRetryAfter
from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.language import best_effort_language_for_user
from telegram_invite_bot.repositories.checks_repo import ChecksRepo
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.game_limits_repo import GameLimitsRepo
from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
from telegram_invite_bot.repositories.p2p_repo import P2pRepo
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from telegram_invite_bot.repositories.users_repo import UsersRepo
from telegram_invite_bot.repositories.vip_repo import VipExpiryCandidate, VipRepo
from telegram_invite_bot.repositories.withdrawals_repo import (
    StalePending,
    WithdrawalsRepo,
)
from telegram_invite_bot.services.p2p_service import ExpiredTrade, P2pService
from telegram_invite_bot.services.p2p_service import SweepReport as P2pSweepReport
from telegram_invite_bot.services.pvp_service import (
    DEFAULT_OFFER_TTL_MINUTES,
    ExpiredOffer,
    PvpService,
    PvpSweepReport,
)
from telegram_invite_bot.utils.aiogram import BENIGN_EDIT_REJECTS
from telegram_invite_bot.utils.economy import (
    STALE_WITHDRAWAL_AGE,
    format_age,
    parse_db_timestamp,
)
from telegram_invite_bot.utils.time import db_now

if TYPE_CHECKING:
    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="scheduler.economy_cleanup")

# Hourly, matching legacy's ``cleanup_expired_inventory`` cadence.
DEFAULT_INTERVAL_SECONDS = 3600.0

# #268: how often the MONEY half of the pass runs, independent of the
# hygiene interval above. Legacy swept expired PvP offers once a minute
# (``bot.py:14828-14834``); it had NO periodic P2P sweep at all — stale
# pending trades sat there until someone touched them. The port folded
# both into the hourly inventory sweep, which is where the two hours of
# holdup in prod offer #80 came from. Two further deltas, both
# deliberate: legacy's PvP worker only ran ``if PVP_GAMES_ONLY``
# (``bot.py:14837``), so on a normal deployment nothing expired offers
# at all, and its cutoff carried a ``max(60, …)`` floor
# (``bot.py:14801``) that the port has no need for — the interval below
# is the floor here. The loop also sleeps a full interval BEFORE its
# first pass, so every restart pushes the next refund another hour out —
# and while prod runs for days at a time when left alone, a working day
# of deploys recycles it dozens of times, which is exactly when somebody
# is most likely to be waiting on a stake. The money step is two indexed
# SELECTs over tiny tables; a minute of it costs nothing next to holding
# somebody's stake hostage.
#
# #281: the stale-withdrawal alert now rides this tick too, for the same
# reason and at the same price — see :meth:`EconomyCleanup.sweep_alerts`.
_MONEY_TICK_SECONDS = 60.0

# Game-play stamps older than this are pruned. 48h = 2× the widest cap
# window (the rolling day) so a row can never be deleted while it might
# still count toward a cap, even under modest clock skew.
_GAME_PLAYS_RETENTION = timedelta(hours=48)

# L-95: DM a user whose global VIP expires within this window. Legacy
# used a 24h window (``bot.py:6968``: ``left_sec > 86400`` bails) but
# only fired the check on profile views; the cutover brief widens the
# sweep-driven notice to ~3 days so a user who never opens their profile
# still gets warned with time to renew. Once-per-grant dedup lives in
# ``users.vip_notified_till`` (see VipRepo.list_expiring_global).
VIP_NOTICE_WINDOW = timedelta(days=3)

# The VIP notice is a fan-out DM loop, so it obeys the same two rules the
# broadcast handler learned (see ``handlers/broadcast.py``): pace the
# sends, and honour a flood wait instead of burning the recipient.
#
# ``0.05`` s ≈ 20 msg/s, inside Telegram's ~30 msg/s global bot limit.
# Without it a batch of expiring grants goes out as fast as the socket
# allows and earns a 429 that costs the whole hour's notices.
_SEND_PAUSE_SECONDS = 0.05
# Upper bound on a honoured flood wait. Seconds are normal; anything past
# this is a penalty not worth blocking an hourly sweep on — we cap the
# sleep, log it, and let the candidate roll over to the next pass.
_MAX_RETRY_AFTER_SECONDS = 60

# #1617: how many VIP expiry notices one hygiene pass may send.
#
# The candidate query had no ceiling, and this loop spends a DM, a
# ``_SEND_PAUSE_SECONDS`` pause and its own economy session per
# candidate — while ``run`` awaits the whole hygiene pass inside the
# same single loop that drives ``sweep_money``. So the length of this
# fan-out is exactly how long P2P and PvP stakes stay held past their
# TTL, and nothing bounded it.
#
# 100 is a budget, not a rate limit: the window is three days wide
# against an hourly pass, so a grant has ~72 chances to be notified,
# and the query hands back the nearest deadlines first. The bound it
# buys is ~5 s of pacing plus 100 sends — small against the 60-second
# money tick, where the unbounded version was minutes.
#
# The same shape as ``left_bonds_cleanup._PROBE_BUDGET_PER_PASS``, and
# for the same reason: a background job that fans out to Telegram must
# have a ceiling that does not depend on how big the table got.
_VIP_NOTICE_BUDGET_PER_PASS = 100

# #1486: wall-clock ceiling on the VIP fan-out, in seconds.
#
# ``_VIP_NOTICE_BUDGET_PER_PASS`` bounds the fan-out in CANDIDATES; this
# bounds it in TIME, which is the quantity the ticket actually cares
# about. A budget of 100 candidates is only cheap while every send is
# cheap: one honoured flood wait costs up to
# ``_MAX_RETRY_AFTER_SECONDS`` all by itself, so the worst case under
# the count budget alone is ~100 minutes during which ``run`` cannot
# reach ``sweep_money`` — P2P expiry and PvP stake refunds, i.e. other
# people's coins held past their TTL. That is the same failure #268 was
# written to end, re-entering through the notice loop.
#
# 30 s is half of ``_MONEY_TICK_SECONDS``. The deadline gates ENTERING a
# candidate, not a wait already in flight, so the true worst case is this
# budget plus one capped flood wait (``_MAX_RETRY_AFTER_SECONDS``) —
# ~90 s, against an unbounded ~100 minutes before. Declining the flood
# wait instead would be worse on both counts: it would make the cap dead
# code (60 > 30) and hand the next pass the same penalty to re-earn.
#
# DELIBERATE DEVIATION from #1486's prescribed remedy ("вынести рассылку
# в отдельный таск"). A second task would have to be owned across
# shutdown and engine disposal, and — because the report is built from
# what the fan-out RETURNED — ``vip_notices_sent`` would degrade from
# "delivered" to "queued", silently rewriting the meaning of sixteen
# pinned assertions in
# ``tests/integration/scheduler/test_economy_cleanup_expiry.py``. The
# deadline buys the same property (the money tick never waits on
# Telegram I/O for long) with no new task and no change to what the
# counter means. Nothing is lost by stopping early: the durable
# ``users.vip_notified_till`` mark means an un-notified candidate is
# simply picked up by the next pass, and the window is three days wide
# against an hourly pass.
_VIP_FANOUT_BUDGET_SECONDS = 30.0

# #1766: wall-clock ceiling on the expired-PvP card fix-up, in seconds.
#
# The same reasoning as ``_VIP_FANOUT_BUDGET_SECONDS``, on a tighter
# budget because this fan-out sits on the SIXTY-second money tick rather
# than the hourly hygiene one, and because it is pure cosmetics: the
# stake is already back in the creator's wallet before the first edit is
# attempted. A card that does not get closed this minute is closed the
# minute its offer would have been swept anyway — except it is not, since
# ``expire_guard`` retires an offer exactly once. That is the deliberate
# trade: a missed edit leaves a dead button that answers
# ``h_pvp_not_found``, which is precisely today's behaviour for all
# thirty of them, and never a coin out of place.
#
# ``_EXPIRY_SCAN_LIMIT`` (200) bounds the fan-out in CARDS; at
# ``_SEND_PAUSE_SECONDS`` that is ~10 s of pacing before a single edit's
# latency is counted, which is already a sixth of the tick. Five seconds
# is the ceiling that keeps the money tick a money tick.
_PVP_CARD_BUDGET_SECONDS = 5.0

# Last-resort language for an expiry card. ``pvp_offers`` stores none,
# and this is only reached when ``users.db`` cannot be read at all — the
# same degraded wiring ``_resolve_languages`` already tolerates. Spelled
# out rather than imported because the i18n default is private, and it
# is also the default ``best_effort_language_for_user`` itself applies.
_DEFAULT_CARD_LANGUAGE = "ru"

# #1768: wall-clock ceiling on the expired-P2P-trade DM pass, in seconds.
#
# Same shape and same tick as ``_PVP_CARD_BUDGET_SECONDS``, and the same
# number for the same reason: this fan-out also rides the SIXTY-second
# money tick, and every second it spends is a second somebody else's
# escrow stays held past its TTL. It is NOT cosmetic the way the PvP
# card pass is — this DM is the only signal a buyer who already sent the
# fiat ever gets — but the trade is durably ``cancelled_timeout`` before
# the first send is attempted, so a dropped DM costs information, never
# coins, and the money half must not be the thing that waits.
_P2P_DM_BUDGET_SECONDS = 5.0


# #169: how many overdue requests the owner alert names individually.
# The rest are covered by the total, which is queried separately — the
# DM must stay well inside Telegram's 4096-char limit no matter how deep
# the queue gets, because an alert that 400s is an alert that never
# arrives.
_STALE_WITHDRAWAL_SAMPLE = 5


def _naive_local_now() -> datetime:
    """Naive local-time ``now`` — matches the stored-``expires`` convention.

    ``InventoryRepo.list_for_user`` / ``delete_expired`` and the legacy
    writer all use naive local ``datetime.now()`` for the ``expires``
    column; the sweep MUST compare in the same frame or it would reap
    rows off by the UTC offset.
    """
    return datetime.now()  # noqa: DTZ005 — intentional naive local time


@dataclass(frozen=True, slots=True)
class _HygieneResult:
    """What the hygiene third of a pass managed to do.

    All zeros with an empty candidate tuple is the honest report of a
    hygiene step that raised (#1706) — the pass still happened, that
    third of it simply produced nothing. Private because the public
    shape of a pass is and stays :class:`EconomyCleanupReport`.
    """

    inventory_deleted: int = 0
    privileges_deleted: int = 0
    game_plays_deleted: int = 0
    checks_deactivated: int = 0
    vip_candidates: tuple[VipExpiryCandidate, ...] = ()


@dataclass(frozen=True, slots=True)
class MoneySweepReport:
    """What one money tick actually moved (#1768).

    :meth:`EconomyCleanupSweeper.sweep_money` used to return a bare
    ``int`` — the PvP count — and its docstring said the P2P step
    "reports through its own return value, which this caller does not
    need". It did need it, twice over: the report named every trade the
    pass retired, which is exactly the list required to tell each buyer
    their trade timed out (#1768), and the count never reached
    :class:`EconomyCleanupReport`, so a pass whose only work was P2P
    expiry logged nothing at all (#1771) — the same omission #268 and
    #1034 had already fixed one counter at a time.

    Both fields are counts of work DONE, so a step that degraded
    (its ``except`` arm ran) contributes zero rather than an exception.
    """

    p2p_trades_expired: int = 0
    """Stale pending P2P trades cancelled + un-escrowed this tick (D2)."""
    pvp_offers_expired: int = 0
    """Stale pending PvP offers expired + refunded this tick (AUD-2)."""


@dataclass(frozen=True, slots=True)
class EconomyCleanupReport:
    """Single-pass result, for observability + tests.

    ``inventory_deleted`` is the count of expired inventory rows reaped;
    ``game_plays_deleted`` the count of stale anti-abuse stamps pruned.
    """

    inventory_deleted: int
    game_plays_deleted: int
    # L-95 additions. Defaulted so pre-existing constructors/tests that
    # only know the original two counters keep working unchanged.
    checks_deactivated: int = 0
    """Expired-but-still-active ``checks`` rows flipped to inactive."""
    vip_notices_sent: int = 0
    """VIP expiry-warning DMs successfully delivered this pass."""
    pvp_offers_expired: int = 0
    """Stale pending PvP offers expired + refunded this pass (AUD-2)."""
    p2p_trades_expired: int = 0
    """Stale pending P2P trades cancelled + un-escrowed this pass (D2).

    #1771: the P2P step has run on every money tick since #268 and was
    the one money step with no counter here at all, so a pass whose only
    work was returning somebody's escrowed slice logged nothing.
    """
    privileges_deleted: int = 0
    """Expired ``user_privileges`` rows pruned this pass (M-1)."""
    stale_withdrawals_alerted: int = 0
    """Overdue withdrawal requests named in an owner alert this pass (#169).

    Counts requests newly reported, not alerts sent — one DM covers the
    whole batch. Zero on every pass where the queue is clean *or* where
    nothing has gone stale since the last report, which is the point:
    the owner is told once per request, not once per hour.
    """


class EconomyCleanupSweeper:
    """Background task that reaps expired inventory + stale play stamps."""

    def __init__(
        self,
        registry: EngineRegistry,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], datetime] = _naive_local_now,
        p2p_pending_ttl_minutes: int = 30,
        pvp_offer_ttl_minutes: int = DEFAULT_OFFER_TTL_MINUTES,
        bot: Bot | None = None,
        vip_notice_window: timedelta = VIP_NOTICE_WINDOW,
        vip_notice_budget: int = _VIP_NOTICE_BUDGET_PER_PASS,
        vip_fanout_budget_seconds: float = _VIP_FANOUT_BUDGET_SECONDS,
        admin_chat_id: int = 0,
        stale_withdrawal_age: timedelta = STALE_WITHDRAWAL_AGE,
    ) -> None:
        self._registry = registry
        self._interval_seconds = interval_seconds
        self._clock = clock
        # P2P D2: pending-trade TTL for the expiry sweep (#268 moved
        # that sweep off the hourly cadence onto the money tick).
        self._p2p_pending_ttl_minutes = p2p_pending_ttl_minutes
        # #267: PvP challenges used to ride the P2P knob (30 min) even
        # though legacy expired them in 10 (bot.py:3782). Own knob now.
        self._pvp_offer_ttl_minutes = pvp_offer_ttl_minutes
        # L-95: bot handle for the VIP expiry-warning DM. Optional and
        # None-tolerant — code paths that build the sweeper without a bot
        # (existing tests, hypothetical bot-less deployments) simply skip
        # the notice step; every reaping job still runs. DEGRADED, never
        # crashed.
        self._bot = bot
        self._vip_notice_window = vip_notice_window
        self._vip_notice_budget = vip_notice_budget
        # #1486: wall-clock ceiling on the fan-out itself. See the
        # constant for why a deadline was chosen over a separate task.
        self._vip_fanout_budget_seconds = vip_fanout_budget_seconds
        # #169: the stale-withdrawal alert goes to exactly one recipient.
        # ``0`` (the settings default when ADMIN_CHAT_ID is unset) means
        # there is nowhere to report to, so the whole step is skipped —
        # same posture ``webhook/payments`` takes for its reversal alert.
        self._admin_chat_id = admin_chat_id
        self._stale_withdrawal_age = stale_withdrawal_age
        # #1518: there is no ledger field here any more. The ids
        # already reported live in ``withdrawal_requests.alerted_at``
        # (migration 0016), and this comment used to argue against
        # exactly that column: a repeat DM after a restart is noise, a
        # missed DM is a user waiting on a payout nobody sees, so the
        # cheap side was said to be the right side. That pricing was
        # done when the alert rode the HOURLY hygiene pass. #281 moved
        # it onto the 60-second money tick, and the unit restarts on
        # every deploy with ``Restart=always`` behind it — so the
        # in-process set was emptied far more often than the queue
        # drained, and each restart re-sent the whole backlog. One
        # repeat an hour is noise; a repeat per deploy is what trains
        # the owner to mute the alarm, which is the failure #169 was
        # written to prevent. The column costs one nullable TEXT field
        # and buys a memory that outlives the process.

    async def run(self) -> None:
        """Loop forever until cancelled. Production entry point.

        Wraps :meth:`sweep_once` in a defensive try/except so a transient
        DB hiccup doesn't kill the cleaner — mirrors the FSM sweeper's
        posture.
        """
        log.info(
            "economy cleanup started: hygiene={interval}s money={money}s",
            interval=self._interval_seconds,
            money=min(_MONEY_TICK_SECONDS, self._interval_seconds),
        )
        # #268: two cadences, one loop. The money half (P2P + PvP
        # expiry, both of which hold real coins) runs every tick; the
        # hygiene half only once per ``_interval_seconds`` worth of
        # ticks. Before this the two shared the hourly cadence AND the
        # sleep-first below, so a unit that restarts every ~28 minutes
        # never reached a single refund pass.
        #
        # #1393: the counter below is a LOCAL, so it restarts at zero
        # with the process — and #268's arithmetic applies to the
        # hygiene half exactly as it did to the money half. Production
        # recorded 235 restarts over 24 days with a median incarnation
        # lifetime of 1505 s, i.e. only 25 of the 235 lived long enough
        # to reach the hourly gate at all; on the other 210 the expired
        # inventory, expired privileges, stale play stamps, expired
        # checks and the user-visible VIP expiry DM were all skipped in
        # silence. (The "economy cleanup pass" log line only fires when
        # work was found, so its absence was never the evidence — the
        # restart-gap arithmetic is.) Seeding the counter AT the
        # interval makes the first tick after startup a hygiene tick,
        # so every incarnation sweeps once however short it is.
        #
        # Repeating a sweep is safe: the four reaps are idempotent, and
        # the VIP notice dedups DURABLY on ``users.vip_notified_till``
        # (``VipRepo.list_expiring_global``) rather than in memory, so
        # no restart can re-DM anybody.
        #
        # #1618: safe is not the same as free, and this comment used to
        # say the four reaps were "bulk deletes over indexed columns".
        # Against the production schema, read read-only, two of the
        # four are neither. Named individually, since the seeding above
        # is decided on this paragraph:
        #   * game plays  — ``game_plays.played_at``, indexed
        #     (``idx_game_plays_played``);
        #   * privileges  — ``user_privileges.expires_at``, indexed
        #     (``idx_user_privileges_expires``);
        #   * inventory   — ``inventory.expires``, NOT indexed
        #     (``inventory`` carries only ``idx_inventory_user`` and
        #     ``idx_inventory_used``): a full scan;
        #   * checks      — ``checks.expires_at``, NOT indexed
        #     (``checks`` carries only ``idx_checks_code``): a full
        #     scan, and an ``UPDATE ... SET is_active = 0`` rather than
        #     a delete.
        # The fifth step of the block, the VIP candidate SELECT, scans
        # too — ``vip_till`` is unindexed as well (#1617).
        #
        # None of that is expensive at production's row counts, and the
        # seeding stays. It is written down so the next person pricing
        # a per-incarnation hygiene pass prices the real thing. The
        # models are not the place to check: they declare indexes
        # production does not have and vice versa (#1621).
        #
        # #1487: the counter below is charged in WALL CLOCK, not in
        # nominal sleeps. ``since_hygiene += money_tick`` counted what
        # the loop asked for; what it actually spends is that sleep PLUS
        # the pass that follows it, so the hygiene period was
        # ``interval + sum of pass durations`` and drifted a little
        # further out with every tick. Reading ``time.monotonic()``
        # across the whole iteration charges the real thing.
        #
        # The ``max`` is not defensive padding. A monotonic clock never
        # goes backwards, and ``asyncio.sleep`` never returns early, so
        # in production the measured span is always >= ``money_tick``
        # and the ``max`` is the measurement. It matters when the sleep
        # is a stub — every test of this loop replaces ``asyncio.sleep``
        # with something instant — where the measured span is ~0 and the
        # nominal tick is the only truthful number available. Pinned
        # cadences such as ``test_seeding_the_hygiene_counter_does_not
        # _collapse_the_interval`` therefore keep meaning exactly what
        # they meant before.
        money_tick = min(_MONEY_TICK_SECONDS, self._interval_seconds)
        since_hygiene = self._interval_seconds
        last_tick = time.monotonic()
        try:
            while True:
                # Sleep BEFORE the first sweep (unlike the FSM sweeper,
                # whose sweep_once touches only in-memory storage). This
                # one opens an economy DB session, so acting at t=0 races
                # a fast startup→shutdown (the lifespan tests): the
                # aiosqlite connection could still be settling when
                # ``engines.dispose()`` runs, surfacing a finalizer
                # warning. A minute is long enough for that race and short
                # enough that a held stake is refunded promptly.
                await asyncio.sleep(money_tick)
                # The span measured here runs from just after the
                # PREVIOUS sleep to just after this one, so it contains
                # the sleep and the pass that ran between them — which
                # is the whole point.
                tick_now = time.monotonic()
                since_hygiene += max(money_tick, tick_now - last_tick)
                last_tick = tick_now
                try:
                    if since_hygiene >= self._interval_seconds:
                        report = await self.sweep_once()
                        # #1512: charge the slot AFTER the pass
                        # returns, never before it starts. The reset
                        # used to precede the call, so a single
                        # transient failure (a locked economy.db, one
                        # poisoned row) consumed the whole hour and
                        # pushed the retry an hour out — while the
                        # median incarnation of this unit is SHORTER
                        # than that hour, so in practice a hygiene
                        # pass that raised once was never retried at
                        # all. Still a ZEROING and not a subtraction:
                        # carrying the remainder forward would shorten
                        # the first window after the #1393 seed, and
                        # that cadence is pinned on purpose by
                        # ``test_seeding_the_hygiene_counter_does_not
                        # _collapse_the_interval``. The bug here was
                        # the ORDER of the reset, not its arithmetic.
                        since_hygiene = 0.0
                    else:
                        money = await self.sweep_money()
                        report = EconomyCleanupReport(
                            inventory_deleted=0,
                            game_plays_deleted=0,
                            pvp_offers_expired=money.pvp_offers_expired,
                            p2p_trades_expired=money.p2p_trades_expired,
                            # #281: double-reporting on a hygiene tick is
                            # impossible — the two branches are mutually
                            # exclusive and ``sweep_once`` calls the very
                            # same method.
                            stale_withdrawals_alerted=await self.sweep_alerts(),
                        )
                    if (
                        report.inventory_deleted
                        or report.game_plays_deleted
                        or report.checks_deactivated
                        or report.vip_notices_sent
                        # #268: was missing from both the gate and the
                        # payload, so a pass whose ONLY work was refunding
                        # expired PvP stakes logged nothing at all.
                        or report.pvp_offers_expired
                        # #1771: and again, for the one money step that
                        # never had a counter here at all — a tick whose
                        # only work was returning an escrowed P2P slice
                        # left no trace in the journal.
                        or report.p2p_trades_expired
                        # #1034: same omission as #268, one counter over.
                        # ``privileges_deleted`` is filled by ``sweep_once``
                        # (M-1) but appeared in neither the gate nor the
                        # payload, so a hygiene pass whose only work was
                        # pruning expired privilege rows logged nothing —
                        # and a pass that logged for other reasons never
                        # said how many it had pruned.
                        or report.privileges_deleted
                        or report.stale_withdrawals_alerted
                    ):
                        log.bind(
                            inventory_deleted=report.inventory_deleted,
                            game_plays_deleted=report.game_plays_deleted,
                            checks_deactivated=report.checks_deactivated,
                            vip_notices_sent=report.vip_notices_sent,
                            pvp_offers_expired=report.pvp_offers_expired,
                            p2p_trades_expired=report.p2p_trades_expired,
                            privileges_deleted=report.privileges_deleted,
                            stale_withdrawals_alerted=report.stale_withdrawals_alerted,
                        ).info("economy cleanup pass")
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — defensive top-level
                    log.exception("economy cleanup pass failed; continuing")
        except asyncio.CancelledError:
            log.info("economy cleanup cancelled; exiting cleanly")
            raise

    async def sweep_money(self) -> MoneySweepReport:
        """The money half of a pass: P2P trade expiry + PvP offer expiry.

        Returns a :class:`MoneySweepReport` naming BOTH steps. It used to
        return the PvP count alone, on the grounds that "the P2P step
        reports through its own return value, which this caller does not
        need" — see that class for the two things the dropped half was
        actually carrying (#1768, #1771).

        Split out from :meth:`sweep_once` for #268 so ``run`` can drive it
        on its own, much shorter cadence — see ``_MONEY_TICK_SECONDS``.
        Both steps refund real coins and both are guarded per-item, so
        running them more often is cheap and strictly reduces how long a
        dead offer holds somebody's stake.

        Steps 1/3 and 2/3 get SEPARATE economy transactions, and neither
        shares one with the hygiene half. See :meth:`sweep_once` for why.

        #1513: they also get separate try/except arms, with the step
        bound into the log line. Splitting the transactions bought
        durability — a failing step no longer rolls the other one
        back — but not availability: an exception from either step
        still escaped this method, so one poisoned ``p2p_trades`` row
        stopped the PvP refunds too, and every pass afterwards logged
        the same generic line. Degrading to "this step is down" keeps
        the other half of the money sweep running, and naming the step
        is what turns the journal into a diagnosis.
        """
        # 1/3 — P2P D2: expire stale pending trades, returning each COM
        # slice to its order. NAIVE UTC here, NOT self._clock() — p2p
        # created_at rows are naive UTC like the rest of the new
        # pipeline; the sweeper's naive-LOCAL clock would skew the TTL by
        # the UTC offset.
        p2p_report = P2pSweepReport()
        try:
            async with session_for(self._registry, DBName.ECONOMY) as session:
                p2p_service = P2pService(
                    P2pRepo(session),
                    EconomyRepo(session),
                    TransactionsRepo(session),
                    session,
                    pending_ttl_minutes=self._p2p_pending_ttl_minutes,
                )
                swept_p2p = await p2p_service.sweep(db_now())
            # #2012: bound only after ``session_for`` has committed. The
            # report is a claim about coins that moved, and until the
            # ``async with`` exits cleanly no coins have moved — a commit
            # that fails in ``__aexit__`` lands in the ``except`` below
            # with the work rolled back. Assigned inside, it survived
            # that rollback and the fan-out announced refunds that were
            # undone.
            p2p_report = swept_p2p
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one step down, not the sweep
            log.bind(step="p2p_pending").exception(
                "economy cleanup money step failed; continuing with the next step"
            )

        # 2/3 — AUD-2: expire stale pending PvP offers (nobody accepted
        # the challenge) and refund the creator's held stake. Same
        # naive-UTC frame; own TTL knob since #267.
        pvp_report = PvpSweepReport()
        try:
            async with session_for(self._registry, DBName.ECONOMY) as session:
                pvp_service = PvpService(
                    EconomyRepo(session),
                    TransactionsRepo(session),
                    session,
                    offer_ttl_minutes=self._pvp_offer_ttl_minutes,
                )
                swept_pvp = await pvp_service.sweep_expired(db_now())
            # Same as step 1/3 above, same reason.
            pvp_report = swept_pvp
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one step down, not the sweep
            log.bind(step="pvp_offers").exception(
                "economy cleanup money step failed; continuing with the next step"
            )

        # AFTER both money steps: tell the people whose escrow just
        # moved. Deliberately NOT numbered like the steps above — the
        # numbering counts economy transactions (1/3 P2P, 2/3 PvP, 3/3
        # hygiene in ``sweep_once``) and this opens none. It moves no
        # coins either.
        #
        # OUTSIDE both ``async with`` blocks on purpose: these are
        # Telegram I/O, and a database session must never stay open
        # across it (the money the sessions just committed is what the
        # messages are announcing, so holding one would put a write lock
        # on ``economy.db`` for the length of a fan-out). Outside both
        # ``try`` arms too, so a failure here can never be misreported as
        # "the P2P/PvP refund step is down".
        #
        # #1768 moved this below the PvP step rather than running the
        # P2P half right after step 1/3: coins first, words after. A
        # fan-out wedged between the two steps would delay real refunds
        # by its whole budget, which is the failure #268 split the
        # cadences to end. It also means the PvP step's ``except`` arm
        # can no longer swallow the P2P notifications on its way out —
        # it used to ``return 0`` from here.
        if self._bot is not None:
            if p2p_report.expired:
                await self._notify_p2p_expired(self._bot, p2p_report.expired)
            if pvp_report.expired:
                await self._notify_pvp_expired(self._bot, pvp_report.expired)
        return MoneySweepReport(
            p2p_trades_expired=p2p_report.count,
            pvp_offers_expired=pvp_report.count,
        )

    async def sweep_alerts(self) -> int:
        """The #169 stale-withdrawal scan, on the money cadence.

        #281: this used to live inside :meth:`sweep_once`, i.e. behind
        the hourly hygiene gate AND behind ``run``'s sleep-first, so
        every restart pushed the next scan a further hour out. Left
        alone the unit runs for days and the hourly gate is harmless —
        but a day of deploys recycles it dozens of times, and the
        journal shows the hygiene pass has logged work three times in
        nine days. An alarm whose firing depends on nobody having
        deployed recently is not an alarm.

        Two indexed reads over a queue a human works by hand, so riding
        the minute tick costs what the money half already costs. Own
        session, like the money steps: a Telegram DM must never extend a
        DB transaction, and this scan must not be rolled back by a
        failure in a hygiene step it has nothing to do with.

        Returns the number of requests newly reported — 0 when the queue
        is clean, and 0 when there is nowhere to report to.
        """
        if self._bot is None or not self._admin_chat_id:
            return 0
        # NAIVE UTC (db_now), not the sweeper's naive-LOCAL clock:
        # ``withdrawal_requests.created_at`` is stamped by the module
        # function ``withdraw_service._now_iso`` (NOT a method on
        # ``WithdrawService``) in the naive-UTC frame, so the local
        # clock would skew the threshold by the UTC offset — three
        # hours of false "not yet stale" here.
        cutoff = (db_now() - self._stale_withdrawal_age).isoformat(sep=" ", timespec="seconds")
        async with session_for(self._registry, DBName.ECONOMY) as session:
            repo = WithdrawalsRepo(session)
            # Two predicates, two queries, one purpose: the count is the
            # honest depth of the queue for the header, the sample is
            # the oldest few the owner has NOT been told about yet
            # (#208 — a head that never clears must not hide the tail).
            # Both halves used to be assembled here from an id set and
            # an in-process ledger; since #1518 the second predicate is
            # ``alerted_at IS NULL``, which survives a deploy.
            overdue_total = await repo.count_stale_pending(older_than_iso=cutoff)
            if not overdue_total:
                return 0
            sample = await repo.list_stale_pending(
                older_than_iso=cutoff, limit=_STALE_WITHDRAWAL_SAMPLE
            )
        if not sample:
            return 0
        return await self._alert_stale_withdrawals(self._bot, sample, overdue_total)

    async def sweep_once(self) -> EconomyCleanupReport:
        """One reaping pass. Pure given ``self._clock``.

        #268: the steps run in THREE separate economy transactions, not
        one. The single shared transaction this used to be was a bad
        trade at the money boundary: the P2P and PvP steps refund real
        coins, and a *durable* failure in any later step — a broken
        ``checks`` index, a ``withdrawal_requests`` schema drift — rolled
        those refunds back on every pass, forever. "The next pass retries
        cleanly" is only true for transient faults; for a persistent one
        the retry is the bug. Hygiene (inventory, game plays, checks) is
        idempotent and losing a pass of it costs nothing, so it can share
        a transaction with the reads. Money cannot, and P2P and PvP do
        not share one with each other either.

        ``session_for`` commits on clean exit / rolls back on exception,
        so no explicit ``commit`` here.
        """
        now = self._clock()
        money = await self.sweep_money()

        # 3/3 — hygiene + the VIP read-only alert scan. (#281 moved the
        # stale-withdrawal scan into its own session and its own
        # cadence; this pass still runs it, so a pass is still a pass.)
        #
        # #1706: hygiene gets its OWN ``except`` for the same reason
        # #268 gave it its own transaction. Everything below it — the
        # stale-withdrawal alarm most of all — used to be unreachable
        # whenever the hygiene session raised, and a failure that
        # reaches this line is by definition one ``session_for`` could
        # not roll back into success. A durable one (a broken index, a
        # poisoned row) therefore silenced the ONLY push signal that
        # says a hand-worked payout queue has stopped being worked,
        # and it left ``since_hygiene`` uncharged in ``run``, so the
        # same doomed pass re-ran every money tick instead of hourly.
        #
        # This narrows #1512 rather than reversing it: a hygiene fault
        # no longer earns the fast retry, but #268 already established
        # that hygiene is idempotent and that losing a pass of it costs
        # nothing. A fault in ``sweep_money`` still propagates, so the
        # half that moves real coins keeps the fast retry it was given.
        try:
            hygiene = await self._sweep_hygiene(now)
        except Exception:
            log.exception("economy hygiene step failed; the pass continues")
            hygiene = _HygieneResult()

        stale_withdrawals_alerted = await self.sweep_alerts()

        vip_notices_sent = 0
        if self._bot is not None and hygiene.vip_candidates:
            vip_notices_sent = await self._notify_vip_expiring(
                self._bot, hygiene.vip_candidates, now
            )

        return EconomyCleanupReport(
            inventory_deleted=hygiene.inventory_deleted,
            game_plays_deleted=hygiene.game_plays_deleted,
            checks_deactivated=hygiene.checks_deactivated,
            vip_notices_sent=vip_notices_sent,
            pvp_offers_expired=money.pvp_offers_expired,
            p2p_trades_expired=money.p2p_trades_expired,
            privileges_deleted=hygiene.privileges_deleted,
            stale_withdrawals_alerted=stale_withdrawals_alerted,
        )

    async def _sweep_hygiene(self, now: datetime) -> _HygieneResult:
        """The idempotent third of a pass: prune, deactivate, read.

        Split out of :meth:`sweep_once` for #1706 so a fault here can be
        contained without also containing the money steps\' faults —
        see the comment at the call site for why that asymmetry is the
        point. Everything in here is safe to lose for one interval and
        nothing in here moves coins.

        The VIP candidates are read inside the session and returned
        UN-notified on purpose: the DMs are a network fan-out and must
        not extend the write transaction.

        ``session_for`` commits on clean exit / rolls back on exception,
        so no explicit ``commit`` here.
        """
        async with session_for(self._registry, DBName.ECONOMY) as session:
            inventory_repo = InventoryRepo(session)
            game_limits_repo = GameLimitsRepo(session)
            inventory_deleted = await inventory_repo.delete_expired(now)
            # M-1: prune expired privilege grants in the same hygiene
            # session. ``PrivilegesRepo.delete_expired`` has always
            # documented itself as something "a background job calls on
            # a schedule" — until this line nothing did, and the rows
            # accrued forever. Purely storage hygiene: every reader
            # already filters on ``expires_at`` (``get_active``), so no
            # user-visible behaviour changes. Naive-LOCAL ``now`` is the
            # right frame here for the same reason it is for VipRepo
            # below — the column is a legacy ``time.time()`` REAL and
            # the repo compares via ``.timestamp()``.
            privileges_deleted = await PrivilegesRepo(session).delete_expired(now=now)
            game_plays_deleted = await game_limits_repo.delete_older_than(
                now - _GAME_PLAYS_RETENTION
            )
            # L-95: deactivate expired-but-untouched checks. NAIVE UTC
            # (db_now) like the p2p step — ``checks.expires_at`` is
            # written by the create handler in the naive-UTC frame
            # (the ``_utcnow`` helper, ``handlers/checks.py:165``),
            # NOT the sweeper's naive-LOCAL clock. Legacy wrote that
            # column in naive LOCAL time with a ``'T'`` separator, so a
            # legacy-era row would be compared against the wrong frame
            # here; the port is the only live writer and the production
            # table is empty, so no such row exists (the full picture is
            # in ``_utcnow``'s docstring). Legacy only deactivated
            # lazily at claim time (bot.py:10110-10117); the bulk pass
            # is table hygiene.
            checks_deactivated = await ChecksRepo(session).deactivate_expired(db_now())
            # L-95: collect VIP expiry-notice candidates INSIDE the
            # session, but DM them after it closes — a Telegram network
            # call must not extend the write transaction. Skipped
            # entirely when no bot was injected (degraded, not broken).
            vip_candidates: list[VipExpiryCandidate] = []
            if self._bot is not None:
                vip_candidates = await VipRepo(session).list_expiring_global(
                    # vip_till is a unix timestamp compared via
                    # ``.timestamp()``, so the naive-LOCAL sweeper clock
                    # converts correctly (same frame VipRepo's readers
                    # use — legacy compared ``time.time()``, bot.py:13481).
                    now=now,
                    within=self._vip_notice_window,
                    # #1617: a ceiling on the fan-out below, not on
                    # the read. See the constant.
                    limit=self._vip_notice_budget,
                )
        return _HygieneResult(
            inventory_deleted=inventory_deleted,
            privileges_deleted=privileges_deleted,
            game_plays_deleted=game_plays_deleted,
            checks_deactivated=checks_deactivated,
            vip_candidates=tuple(vip_candidates),
        )

    async def _alert_stale_withdrawals(
        self, bot: Bot, sample: list[StalePending], total: int
    ) -> int:
        """DM the owner about withdrawal requests that have gone overdue.

        Returns the number of requests *newly* reported — 0 when the
        queue is clean and 0 when nothing has aged past the threshold
        since the last pass. That is the whole design: the scan runs on
        the 60-second money tick (#281), so an alert that re-sent the
        same alarm every pass would be muted within the hour, and a
        muted alarm is worse than none because it looks like coverage.

        Selecting what is new is :meth:`sweep_alerts`' job, not this
        one's: it asks the repository for overdue requests with no
        ``alerted_at`` stamp, so every row of ``sample`` is by
        construction new. This method renders, sends, and only then
        stamps. Keeping selection and marking apart is what fixes #208 —
        the old arrangement deduped against the sample it had just been
        handed, which is circular.

        Best-effort, exactly like the payment alerts in
        ``webhook/payments``: a failed DM leaves the rows UNSTAMPED so
        the next pass retries. Losing a minute on a queue measured in
        days is free; losing the alert entirely is not.
        """
        fresh = sample

        hours = int(self._stale_withdrawal_age.total_seconds() // 3600)
        now = db_now()
        lines = [
            "🏧 <b>Заявки на вывод висят без ответа</b>",
            "",
            f"Просрочено (дольше {hours} ч): <b>{total}</b>",
        ]
        # Only when the two differ — on the common "one new request went
        # overdue" pass the numbers are equal and a second count line
        # would be noise. When they differ it is load-bearing: the list
        # below names ONLY the new ones, so without this the reader would
        # take a 5-bullet list under a "8 overdue" header as a truncation.
        if len(fresh) != total:
            lines.append(f"Новые в этой сводке: <b>{len(fresh)}</b>")
        lines.append("")
        for row in fresh:
            parsed = parse_db_timestamp(row.created_at)
            age = format_age(now - parsed) if parsed is not None else "?"
            lines.append(
                f"• <code>#{row.request_id}</code> uid=<code>{row.user_id}</code> "
                f"— <code>{row.amount_com}</code> DLAB, ждёт <b>{age}</b>"
            )
        lines += [
            "",
            "Выплаты здесь ручные: монеты у пользователя уже удержаны, "
            "а деньги он ещё не получил. Откройте очередь и решите по каждой "
            "заявке — <code>/admin_withdrawals</code>.",
        ]
        # Nothing interpolated above is user-controlled: ids and amounts
        # are ints, the age is generated ASCII. ``payment_details`` is
        # deliberately absent (see ``StalePending``), which is also why
        # this message needs no escaping pass.
        try:
            await bot.send_message(self._admin_chat_id, "\n".join(lines))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — courtesy alert, log-only
            log.bind(total=total).warning(
                "stale withdrawal alert DM failed (will retry next pass): {}", exc
            )
            return 0
        # #1518: the stamp gets its own session and its own guard. Its
        # own session because a Telegram round trip must never sit
        # inside a money-DB transaction — the read above is closed by
        # the time the DM goes out. Its own guard because the DM has
        # ALREADY been delivered: a failed write costs at most one
        # repeat next pass, while letting it escape would abort the
        # sweep over bookkeeping that has already served its purpose.
        try:
            async with session_for(self._registry, DBName.ECONOMY) as session:
                await WithdrawalsRepo(session).mark_alerted(
                    [row.request_id for row in fresh],
                    alerted_at=now.isoformat(sep=" ", timespec="seconds"),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — mark failure is recoverable
            log.bind(total=total).warning(
                "stale withdrawal alert mark failed (will re-DM next pass): {}", exc
            )
        log.bind(total=total, reported=len(fresh)).warning(
            "stale withdrawal requests reported to owner"
        )
        return len(fresh)

    async def _notify_p2p_expired(self, bot: Bot, trades: Sequence[ExpiredTrade]) -> int:
        """DM every buyer whose pending P2P trade the sweep just cancelled (#1768).

        ``P2pService.expire_pending`` un-escrows a stale trade — the COM
        slice goes back to ``remaining_com`` if the order still stands,
        or to the seller's wallet if it does not — and flips the trade to
        ``cancelled_timeout``. The seller can see that in the order book.
        The BUYER cannot see anything at all: nothing is posted, nothing
        is edited, and the money that moved was never theirs to watch.

        That asymmetry is the defect. A buyer reaches ``pending`` by
        agreeing to send fiat off-platform; the plausible state at expiry
        is «I already paid and the seller has not confirmed». Telling
        them the trade is dead is the difference between a support
        ticket and a silent loss of trust — and it is the ONLY signal
        they get, because there is no card to strip.

        A DM, not an edit, and that is forced rather than chosen:
        :class:`~telegram_invite_bot.db.models.p2p.P2pTrade` stores no
        ``chat_id``/``message_id``, unlike ``pvp_offers``. So this is the
        one expiry fan-out with nowhere to fall back to.

        Bounded exactly like :meth:`_notify_pvp_expired`, and for the
        same reason — it rides the same SIXTY-second money tick:
        ``_SEND_PAUSE_SECONDS`` between sends so a batch does not earn a
        429, and ``_P2P_DM_BUDGET_SECONDS`` of wall clock so a fan-out
        can never be what keeps somebody else's escrow held past its TTL.
        A flood wait is logged and the pass abandoned rather than slept
        through, matching the sibling PvP pass and NOT
        :meth:`_notify_vip_expiring`: ``_MAX_RETRY_AFTER_SECONDS`` is 60,
        which is this method's entire tick.

        Nothing is retried and nothing is remembered. ``expire_pending``
        retires a trade exactly once, so a buyer missed here is never
        re-swept. That is the accepted cost: the alternative is a durable
        "buyer told" column, and the trade is already durably
        ``cancelled_timeout`` — committed and out of the session — before
        the first send is attempted, so a dropped DM costs information,
        never coins.
        """
        sent = 0
        # Sends ATTEMPTED, which is what the rate limiter counts — a DM
        # Telegram refused (blocked bot, deleted account) cost an API
        # call just the same. Same shape as the PvP pass, minus its
        # cardless-offer skip: every expired trade has a buyer.
        languages = await self._resolve_user_languages({trade.buyer_id for trade in trades})
        deadline = time.monotonic() + _P2P_DM_BUDGET_SECONDS
        for index, trade in enumerate(trades):
            if index:
                await asyncio.sleep(_SEND_PAUSE_SECONDS)
            if time.monotonic() >= deadline:
                log.bind(sent=sent, left=len(trades) - index).info(
                    "p2p expiry dm budget spent; remaining buyers are not told"
                )
                break
            try:
                await bot.send_message(
                    chat_id=trade.buyer_id,
                    text=t(
                        "h_p2p_trade_expired_buyer",
                        languages[trade.buyer_id],
                        trade_id=trade.trade_id,
                        amount=trade.amount_com,
                    ),
                )
            except TelegramRetryAfter as exc:
                log.bind(
                    trade_id=trade.trade_id,
                    retry_after=exc.retry_after,
                    left=len(trades) - index,
                ).warning("p2p expiry dm flood wait; abandoning the rest of the pass")
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one buyer, not the pass
                # A blocked bot is the ordinary case here, not an
                # incident: unlike the PvP card edit there is no benign
                # marker set to match on, so everything lands at debug.
                log.bind(trade_id=trade.trade_id).debug("p2p expiry dm failed: {}", exc)
                continue
            sent += 1
        return sent

    async def _notify_pvp_expired(self, bot: Bot, offers: Sequence[ExpiredOffer]) -> int:
        """Strip the «Accept» keyboard off every card the sweep orphaned (#1766).

        ``PvpService.sweep_expired`` refunds the creator's held stake and
        flips the offer to ``expired``, but the challenge card published
        in the group is untouched: its inline button stays tappable for
        as long as the message exists. A tap answers ``h_pvp_not_found``,
        so nothing is minted — the cost is entirely in trust. Production
        is sitting on thirty such cards, each one an open invitation to a
        game that cannot be played, and the creator whose coins came back
        was never told.

        Rewriting the card is the whole fix: same message, expiry copy,
        ``reply_markup=None``. There is no DM — the refund already shows
        up in ``/balance`` and in the ledger, and a private message per
        expired offer would be a second fan-out on the SIXTY-second money
        tick for a purely informational event.

        Shaped like :meth:`_notify_vip_expiring`, and bounded the same
        two ways for the same reason: ``_SEND_PAUSE_SECONDS`` between
        edits so a batch does not earn a 429, and
        ``_PVP_CARD_BUDGET_SECONDS`` of wall clock so this cosmetic pass
        can never be what keeps somebody else's stake held past its TTL.
        The differences are deliberate:

        * a flood wait is NOT slept through here. The VIP loop honours
          one because a dropped notice is a user who is never warned;
          a dropped card edit is a button that stays dead-but-harmless,
          and this loop runs on a tick sixty times faster than that one.
          It logs the wait and stops the pass instead.
        * :data:`BENIGN_EDIT_REJECTS` is swallowed rather than logged as
          a failure. "message to edit not found" is the expected answer
          for a card somebody deleted, and it is the single most likely
          outcome for the backlog this ticket exists to clear.

        Nothing here is retried: ``expire_guard`` retires an offer
        exactly once, so an offer missed by this pass is not re-swept.
        That is accepted — the alternative is a durable "card closed"
        column for a purely cosmetic edit, and the failure mode it would
        buy back is today's behaviour for every one of these cards.
        """
        edited = 0
        # Edits ATTEMPTED, which is what the rate limiter counts — an
        # edit Telegram refused cost an API call just the same, and a
        # cardless offer cost none at all (#1767's lesson, same shape).
        attempted = 0
        languages = await self._resolve_user_languages({offer.creator_id for offer in offers})
        deadline = time.monotonic() + _PVP_CARD_BUDGET_SECONDS
        for index, offer in enumerate(offers):
            if offer.message_id is None:
                # The card post failed when the offer was created, so the
                # refund is the only thing that ever happened. Nothing to
                # close, and it must not cost a pacing pause either.
                continue
            if attempted:
                await asyncio.sleep(_SEND_PAUSE_SECONDS)
            if time.monotonic() >= deadline:
                log.bind(edited=edited, left=len(offers) - index).info(
                    "pvp expiry card budget spent; remaining cards keep their keyboard"
                )
                break
            attempted += 1
            try:
                await bot.edit_message_text(
                    chat_id=offer.chat_id,
                    message_id=offer.message_id,
                    text=t("h_pvp_expired_card", languages[offer.creator_id]),
                    reply_markup=None,
                )
            except TelegramRetryAfter as exc:
                log.bind(
                    offer_id=offer.offer_id,
                    retry_after=exc.retry_after,
                    left=len(offers) - index,
                ).warning("pvp expiry card flood wait; abandoning the rest of the pass")
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one card, not the pass
                if not any(marker in str(exc) for marker in BENIGN_EDIT_REJECTS):
                    log.bind(offer_id=offer.offer_id).debug("pvp expiry card edit failed: {}", exc)
                continue
            edited += 1
        return edited

    async def _resolve_user_languages(self, user_ids: Collection[int]) -> dict[int, str]:
        """Bot language per user, resolved in ONE short ``users.db`` session.

        Same contract as :meth:`_resolve_languages` and for the same
        reasons — resolved BEFORE the fan-out, never inside the economy
        session, and degrading to the default rather than costing the
        message. The fallback here is the i18n default rather than a
        stored column: neither ``pvp_offers`` nor ``p2p_trades`` carries
        a language, and reading the user's wallet row would mean a second
        economy session opened purely to pick between two strings.

        Takes plain ids rather than the rows themselves (#1768) because
        two fan-outs now need it and they key off different columns — a
        PvP offer's ``creator_id`` and a P2P trade's ``buyer_id``.
        """
        creators = set(user_ids)
        try:
            async with session_for(self._registry, DBName.USERS) as session:
                users_repo = UsersRepo(session)
                settings_repo = UserSettingsRepo(session)
                return {
                    creator_id: await best_effort_language_for_user(
                        creator_id,
                        users_repo=users_repo,
                        settings_repo=settings_repo,
                    )
                    for creator_id in creators
                }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — wording must not cost the edit
            log.bind(count=len(creators)).debug(
                "pvp expiry card language resolution failed; using the default: {}", exc
            )
            return dict.fromkeys(creators, _DEFAULT_CARD_LANGUAGE)

    async def _resolve_languages(self, candidates: Sequence[VipExpiryCandidate]) -> dict[int, str]:
        """Effective bot language per candidate, resolved before any DM (#1511).

        ``VipExpiryCandidate.language`` carries ``economy.users.language``,
        which ``EconomyRepo.get_or_create`` stamps ONCE at wallet creation
        and never refreshes — while ``/lang`` writes
        ``users.user_settings.language`` in a different database. So the
        wallet column is a first guess, not a preference: a user who
        created their wallet on an English client and later ran ``/lang``
        was warned in English about a VIP grant they renew in Russian.

        Resolution is the one every third-party DM already uses
        (:func:`best_effort_language_for_user`, as in ``/give`` and the
        withdrawal decisions), and it runs for the WHOLE batch in ONE
        short ``users.db`` session opened BEFORE the fan-out. Not inside
        the per-candidate write session below: that one belongs to
        ``economy.db``, and a session must never stay open across
        Telegram I/O.

        Degrades instead of failing. A registry that carries no
        ``users.db`` (a bot-less or economy-only wiring — ``session_for``
        would raise ``KeyError`` straight out of the bare dict lookup in
        ``EngineRegistry.session``) falls back to the wallet column,
        which is exactly the last-resort role the ticket assigns it: a
        notice in the wrong language beats no notice at all.
        """
        fallback = {c.user_id: c.language for c in candidates}
        try:
            async with session_for(self._registry, DBName.USERS) as session:
                users_repo = UsersRepo(session)
                settings_repo = UserSettingsRepo(session)
                return {
                    c.user_id: await best_effort_language_for_user(
                        c.user_id,
                        users_repo=users_repo,
                        settings_repo=settings_repo,
                        fallback=c.language,
                    )
                    for c in candidates
                }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — wording must not cost the notice
            log.bind(count=len(candidates)).warning(
                "vip expiry language resolution failed; using stored wallet language: {}", exc
            )
            return fallback

    async def _notify_vip_expiring(
        self, bot: Bot, candidates: Sequence[VipExpiryCandidate], now: datetime
    ) -> int:
        """DM each candidate once and durably mark the grant notified.

        Ports legacy ``maybe_notify_vip_expiring`` (``bot.py:6957``) onto
        the sweep: one private message per expiring grant, hours-left
        computed with legacy's own expression (``max(1, int(left/3600))``,
        ``bot.py:6973``), rendered in the language
        :meth:`_resolve_languages` resolved for that user before the
        fan-out started (#1511 — the wallet column this used to read is
        a stamp from account creation, not a preference).

        Ordering is deliberate: DM first, mark second, each candidate in
        its own short write session. A failed DM (blocked bot, deleted
        account — legacy swallowed these too, ``bot.py:6979``) leaves the
        row unmarked so the next pass retries; a crash after the DM but
        before the mark re-DMs at worst once an hour until the mark
        lands — annoying-at-worst beats silently-never-warned. The mark
        itself is guarded on ``vip_till`` so a grant extended mid-pass
        stays eligible for a fresh notice about its NEW deadline.

        This is a fan-out, so it is paced like one: ``_SEND_PAUSE_SECONDS``
        between candidates, and a flood wait is SLEPT THROUGH and retried
        once rather than counted as a failure. Treating a 429 as an
        ordinary error would have been quietly destructive here — the very
        next send hits the same wait, so one flood would drop every
        remaining notice in the batch, and each hourly pass would re-earn
        the penalty by hammering again.

        And because it is a fan-out that sleeps, it is also bounded in
        WALL CLOCK (#1486): ``_VIP_FANOUT_BUDGET_SECONDS`` after the
        first candidate the loop stops STARTING new ones. A wait already
        in flight is not interrupted — it is bounded separately by
        ``_MAX_RETRY_AFTER_SECONDS``, and it necessarily blows the
        deadline, so a flood ends the pass on the next iteration. Worst
        case is therefore budget + cap, not budget × candidates. ``run``
        awaits this call inside the same loop that drives
        ``sweep_money``, so every second spent here is a second
        somebody's P2P or PvP stake stays held past its TTL. The
        stopped-early candidates are not lost: they are simply the ones
        the next pass sees, since the durable mark is written per
        delivered notice.
        """
        sent = 0
        now_ts = now.timestamp()
        languages = await self._resolve_languages(candidates)
        # #1486: a candidate budget alone does not bound this loop in
        # time — see ``_VIP_FANOUT_BUDGET_SECONDS``.
        deadline = time.monotonic() + self._vip_fanout_budget_seconds
        for index, candidate in enumerate(candidates):
            if index:
                await asyncio.sleep(_SEND_PAUSE_SECONDS)
            if time.monotonic() >= deadline:
                log.bind(sent=sent, left=len(candidates) - index).info(
                    "vip expiry fan-out budget spent; remaining candidates roll over"
                )
                break
            hours = max(1, int((candidate.vip_till - now_ts) / 3600))
            text = t("h_vip_expiring_soon", languages[candidate.user_id], hours=hours)
            try:
                try:
                    await bot.send_message(candidate.user_id, text)
                except TelegramRetryAfter as exc:
                    wait = min(exc.retry_after, _MAX_RETRY_AFTER_SECONDS)
                    # #1486: the wait is NOT measured against the
                    # fan-out deadline. It is already bounded by
                    # ``_MAX_RETRY_AFTER_SECONDS``, and honouring it is
                    # the whole reason that cap exists — declining it
                    # here instead would make the cap dead code (60 > 30)
                    # and would re-earn the penalty next pass. Sleeping
                    # it necessarily blows the deadline, so the check at
                    # the top of the loop ends the pass right after:
                    # at most ONE honoured wait per fan-out.
                    log.bind(
                        user_id=candidate.user_id,
                        retry_after=exc.retry_after,
                        wait=wait,
                    ).warning("vip expiry notice flood wait; sleeping then retrying once")
                    await asyncio.sleep(wait)
                    # One retry only. If the wait was capped the second
                    # attempt may bounce again — that candidate stays
                    # unmarked and the next pass picks it up.
                    await bot.send_message(candidate.user_id, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — per-user DM failure must not kill the sweep
                log.bind(user_id=candidate.user_id).debug(
                    "vip expiry notice DM failed (will retry next pass): {}", exc
                )
                continue
            sent += 1
            # #1514: the mark gets its own guard. It used to sit
            # outside every ``try`` in this loop, so one failed write
            # (a locked economy.db) aborted the whole fan-out: the
            # candidates after this one were never DMed at all, while
            # the ones before it stayed marked. A failure here costs
            # at most ONE repeat DM next pass — exactly the trade
            # this docstring already accepts for a crash between the
            # send and the mark. Losing the rest of the batch was not
            # part of that trade.
            try:
                async with session_for(self._registry, DBName.ECONOMY) as session:
                    await VipRepo(session).mark_expiry_notified(
                        user_id=candidate.user_id, vip_till=candidate.vip_till
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — mark failure is recoverable
                log.bind(user_id=candidate.user_id).warning(
                    "vip expiry notice mark failed (will re-DM next pass): {}", exc
                )
        return sent


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "VIP_NOTICE_WINDOW",
    "EconomyCleanupReport",
    "EconomyCleanupSweeper",
    "MoneySweepReport",
]
