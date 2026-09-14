"""End marriages and relationships left behind by departed members (#482).

Legacy called ``_cleanup_left_users_bonds`` (``bot.py:21586-21614``)
inline from five bond READ paths — ``/marry_info``, the couple menus,
the leaderboards. Any of those reads could therefore dissolve a bond as
a side effect, and none of them would if nobody happened to look. The
port never had that hook, so the rule it enforced — a member gone from
the group for a week no longer holds bonds in it — has simply not
existed since the cutover. The rows sit there ``active`` forever, and
the leaderboards keep printing couples where one half has been gone for
months.

This is that rule, moved to where it belongs: a background pass. Two
consequences of the move, both improvements:

* A read never writes. ``/marriages`` is a SELECT again.
* The rule applies to every chat, not only the ones somebody happened
  to open a bond card in. The chat set is derived from the departure
  flags themselves — :meth:`UserGroupJoinsRepo.list_departed_chats`.

**What it does NOT do**, deliberately: the legacy production database
also carries roughly a dozen rows in the old ``user_chat_left`` table,
which this pipeline never writes and this sweep never reads. Those
departures predate ``user_group_joins`` and stay invisible here. Ending
their bonds would mean reading a legacy-only table on a schedule, which
is the opposite direction from the strangler; they will age out as those
users are seen again or never.

Shape mirrors :mod:`telegram_invite_bot.scheduler.economy_cleanup`:
a pure ``sweep_once`` with an injectable clock, an async ``run`` loop
that owns cancellation, and Telegram round-trips performed strictly
BETWEEN sessions so a network stall can never hold a write transaction
open.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
    TelegramRetryAfter,
)
from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo
from telegram_invite_bot.repositories.user_group_joins_repo import UserGroupJoinsRepo

if TYPE_CHECKING:
    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry

log = logger.bind(component="scheduler.left_bonds_cleanup")

# Legacy's own constant, ``bot.py:21581``. Kept to the day: this is the
# grace period a member gets to come back before the group forgets they
# were married in it, and shortening it is a user-visible policy change,
# not a tuning knob.
DAYS_AFTER_LEFT_TO_END_BONDS = 7

# Six hours. The threshold above is measured in days, so sweeping more
# often buys nothing but Telegram calls; sweeping less often would let a
# rejoin sit mis-flagged for longer than it needs to.
DEFAULT_INTERVAL_SECONDS = 6 * 3600.0

# Every incarnation sweeps once, this many seconds after startup,
# regardless of ``DEFAULT_INTERVAL_SECONDS``. This is #1393's lesson
# applied before it can bite: production recorded 235 restarts over 24
# days with a median incarnation lifetime of about 25 minutes, so a job
# whose first action is to sleep six hours would, in practice, never run
# at all. The delay itself exists for the reason the economy sweeper
# documents — acting at t=0 races a fast startup/shutdown while the
# aiosqlite connection is still settling.
_STARTUP_DELAY_SECONDS = 60.0

# How many candidate rows one pass looks at per chat. A cheap indexed
# read; the number only has to be comfortably larger than the probe
# budget below so a chat that runs out of budget still had a full queue
# to draw from.
#
# #2013: this bound applies to departures that STILL HOLD A BOND, not to
# departures. The distinction is the whole difference between a scan cap
# and a silent stall — see
# :meth:`UserGroupJoinsRepo.list_departed_with_active_bonds`, which is
# where the bond filter now lives. Applied to raw departures, as it was
# until #2013, any chat with more than this many lifetime departures
# stopped sweeping entirely: settled departures are permanent, they sort
# to the front, and they filled the window forever.
_CANDIDATE_SCAN_LIMIT = 200

# How many live ``get_chat_member`` probes one pass may spend in total,
# across all chats. Every dissolution is gated on a probe (see
# :meth:`_has_left`), and a bot in many groups could otherwise wake up
# and fire hundreds of API calls in a burst. Nothing is lost by
# spreading them: the work is a week overdue by construction, so another
# six hours changes nothing, and the candidate query returns the
# longest-departed first so the queue drains in order — provided a probe
# that answers nothing cannot hold its place in that queue forever, which
# is what ``_MAX_UNKNOWN_PROBES_PER_CHAT`` below exists to guarantee.
_PROBE_BUDGET_PER_PASS = 25

# Errors that condemn the CHAT rather than the one member we asked
# about: the bot was removed or demoted, the chat is gone, or the group
# migrated to a supergroup under a new id. Every remaining probe in that
# chat would answer the same way, so the first one ends the chat for
# this pass instead of eating the whole budget on it.
#
# These are SIBLINGS of ``TelegramBadRequest`` under ``TelegramAPIError``
# — none of them is caught by a ``TelegramBadRequest`` clause. And
# ``TelegramBadRequest`` itself is deliberately NOT in this tuple: it
# covers both "chat not found" (chat-level) and "user not found"
# (member-level, the ordinary case of a deleted account), and there is
# no reliable way to tell them apart other than matching Telegram's
# prose. The unknown cap below is what bounds that case.
_UNREADABLE_CHAT_ERRORS = (
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNotFound,
)

# How many unknown verdicts one chat may cost before the pass gives up
# on it and moves on. Without this the budget is a starvation vector,
# not a rate limit: chat order (``list_departed_chats`` orders by
# ``chat_id``) and candidate order (``list_departed_with_active_bonds``
# orders by ``left_at``) are both deterministic, an unknown verdict writes
# nothing, and the departure rows it failed to act on are never
# cleared — so a single chat whose probes always fail re-runs the same
# 25 doomed calls on every pass, forever, and every chat behind it is
# never reached at all. The pass stays alive, logs, and dissolves
# nothing. Same failure shape and same remedy as the FSM sweeper's
# poisoned keys (#1908).
_MAX_UNKNOWN_PROBES_PER_CHAT = 3


class _ChatUnreadable(Exception):
    """Internal signal: this chat answers nothing, drop it for this pass."""


def _naive_local_now() -> datetime:
    """Naive local-time ``now`` — the frame ``left_at`` is written in.

    ``UserGroupJoinsRepo.mark_left`` stamps naive LOCAL time because
    legacy does (``bot.py:44190``), and the bond columns this sweep
    writes (``divorced_at``, ``ended_at``, ``restore_until``) use the
    same frame. Comparing a naive-UTC ``now`` against ``left_at`` would
    age every departure by the host's UTC offset — three hours early on
    the MSK production host, which is exactly the bug
    ``_record_leave``'s docstring warns this sweep would have caused.
    """
    return datetime.now()  # noqa: DTZ005 — intentional naive local time


@dataclass(frozen=True, slots=True)
class LeftBondsCleanupReport:
    """Single-pass result, for observability + tests."""

    chats_scanned: int = 0
    """Chats that held at least one departure flag this pass."""
    members_probed: int = 0
    """Live ``get_chat_member`` round-trips actually spent."""
    rejoins_healed: int = 0
    """Departure flags cleared because the member was still present."""
    relationships_ended: int = 0
    """Relationship rows moved to ``status='ended'``."""
    marriages_divorced: int = 0
    """Marriage rows soft-divorced, restore window included."""
    budget_exhausted: bool = False
    """Whether the probe budget ran out before every chat was visited."""
    chats_abandoned: int = 0
    """Chats dropped mid-pass because their probes stopped answering.

    A non-zero value is the operator's only signal that the budget is
    being spent on a chat that cannot be read — the WARNINGs from
    :meth:`_has_left` say which chat, but nothing else says the pass
    walked away from it.
    """


class LeftBondsCleanupSweeper:
    """Background task that ends the bonds of long-departed members."""

    def __init__(
        self,
        registry: EngineRegistry,
        *,
        bot: Bot | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        startup_delay_seconds: float = _STARTUP_DELAY_SECONDS,
        clock: Callable[[], datetime] = _naive_local_now,
        grace: timedelta = timedelta(days=DAYS_AFTER_LEFT_TO_END_BONDS),
        probe_budget: int = _PROBE_BUDGET_PER_PASS,
        max_unknown_probes: int = _MAX_UNKNOWN_PROBES_PER_CHAT,
        scan_limit: int = _CANDIDATE_SCAN_LIMIT,
    ) -> None:
        self._registry = registry
        # Without a bot there is no membership probe, and without a probe
        # there is no safe dissolution — see :meth:`sweep_once`. The
        # whole pass is skipped rather than run unverified: degraded, not
        # broken, and the same posture the economy sweeper takes for its
        # bot-dependent steps.
        self._bot = bot
        self._interval_seconds = interval_seconds
        self._startup_delay_seconds = startup_delay_seconds
        self._clock = clock
        self._grace = grace
        self._probe_budget = probe_budget
        self._max_unknown_probes = max_unknown_probes
        self._scan_limit = scan_limit

    async def run(self) -> None:
        """Loop forever until cancelled. Production entry point."""
        log.info(
            "left-bonds cleanup started: every {interval}s, grace {days}d",
            interval=self._interval_seconds,
            days=self._grace.days,
        )
        try:
            await asyncio.sleep(self._startup_delay_seconds)
            while True:
                try:
                    report = await self.sweep_once()
                    if (
                        report.rejoins_healed
                        or report.relationships_ended
                        or report.marriages_divorced
                        or report.budget_exhausted
                        or report.chats_abandoned
                    ):
                        log.bind(
                            chats_scanned=report.chats_scanned,
                            members_probed=report.members_probed,
                            rejoins_healed=report.rejoins_healed,
                            relationships_ended=report.relationships_ended,
                            marriages_divorced=report.marriages_divorced,
                            budget_exhausted=report.budget_exhausted,
                            chats_abandoned=report.chats_abandoned,
                        ).info("left-bonds cleanup pass")
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — defensive top-level
                    log.exception("left-bonds cleanup pass failed; continuing")
                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            log.info("left-bonds cleanup cancelled; exiting cleanly")
            raise

    async def sweep_once(self) -> LeftBondsCleanupReport:
        """One pass. Pure given ``self._clock`` and the bot's answers.

        The ordering is the whole design:

        1. **Read** the departed chats, then per chat the departures old
           enough to act on *that still hold a bond there*. Both are
           cheap, indexed and read-only, and the bond narrowing is what
           keeps the pass affordable — see
           :meth:`UserGroupJoinsRepo.list_departed_with_active_bonds`.
           The narrowing happens inside that query, not after it: #2013
           is what a scan limit applied to the un-narrowed set costs.
        2. **Probe** each survivor with a live ``get_chat_member``, with
           every session closed. A Telegram round-trip inside an open
           write transaction is how a slow network turns into a locked
           database.
        3. **Write**, in a fresh session per chat.

        Fail-closed at step 2, exactly like legacy's bare ``except:
        continue`` around the same call (``bot.py:21603-21605``): a chat
        we cannot read says nothing, and dissolving a marriage on a
        network hiccup is the one outcome waiting cannot undo.

        Failing closed is only safe while it stays local to the member it
        happened to. A chat the bot was thrown out of answers every probe
        with the same error, and because nothing is written the same rows
        come back next pass in the same order: without a bound the pass
        spends its entire budget re-asking one dead chat and never
        reaches the chats behind it, forever. Two bounds keep the failure
        local — the chat is dropped for the pass on the first error that
        condemns the whole chat (``_UNREADABLE_CHAT_ERRORS``), and after
        ``_MAX_UNKNOWN_PROBES_PER_CHAT`` unknown answers of any other
        kind. Both are counted in ``chats_abandoned``.
        """
        if self._bot is None:
            return LeftBondsCleanupReport()

        now = self._clock()
        threshold = now - self._grace
        budget = self._probe_budget
        chats_scanned = 0
        members_probed = 0
        rejoins_healed = 0
        relationships_ended = 0
        marriages_divorced = 0
        chats_abandoned = 0

        async with session_for(self._registry, DBName.USERS) as session:
            chat_ids = await UserGroupJoinsRepo(session).list_departed_chats()

        for chat_id in chat_ids:
            if budget <= 0:
                break
            async with session_for(self._registry, DBName.USERS) as session:
                candidates = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(
                    chat_id, threshold, limit=self._scan_limit
                )
            if not candidates:
                continue
            chats_scanned += 1

            gone: list[int] = []
            present: list[int] = []
            unknowns = 0
            for user_id in candidates:
                if budget <= 0:
                    break
                budget -= 1
                members_probed += 1
                try:
                    verdict = await self._has_left(chat_id, user_id)
                except _ChatUnreadable:
                    chats_abandoned += 1
                    break
                if verdict is None:
                    # A probe that answered nothing still cost a real
                    # round-trip, so it keeps its budget slot — counting
                    # only useful answers would let a hundred failing
                    # calls fly per pass. What it must NOT do is keep
                    # the whole chat's share of the budget: see
                    # ``_MAX_UNKNOWN_PROBES_PER_CHAT``.
                    unknowns += 1
                    if unknowns >= self._max_unknown_probes:
                        log.bind(chat=chat_id, unknowns=unknowns).warning(
                            "left-bonds probes keep failing; abandoning this chat for this pass"
                        )
                        chats_abandoned += 1
                        break
                    continue
                (gone if verdict else present).append(user_id)

            if not gone and not present:
                continue
            async with session_for(self._registry, DBName.USERS) as session:
                joins = UserGroupJoinsRepo(session)
                bonds = BondsWriteRepo(session)
                for user_id in present:
                    await joins.clear_departure(user_id, chat_id)
                    rejoins_healed += 1
                for user_id in gone:
                    relationships_ended += await bonds.end_relationships_for(
                        chat_id, user_id, now=now
                    )
                    marriages_divorced += await bonds.soft_divorce_all_in_chat(
                        chat_id, user_id, now=now
                    )

        return LeftBondsCleanupReport(
            chats_scanned=chats_scanned,
            members_probed=members_probed,
            rejoins_healed=rejoins_healed,
            relationships_ended=relationships_ended,
            marriages_divorced=marriages_divorced,
            budget_exhausted=budget <= 0,
            chats_abandoned=chats_abandoned,
        )

    async def _has_left(self, chat_id: int, user_id: int) -> bool | None:
        """``True`` gone, ``False`` still here, ``None`` unknown.

        ``None`` is not "probably gone" — callers MUST skip it. Legacy
        read the same two statuses (``bot.py:21600``) and treated every
        error as "leave them alone".

        :class:`TelegramRetryAfter` is re-raised rather than folded into
        ``None``: it means the account is being rate-limited, and the one
        thing a background job must not do at that moment is keep firing
        the same call in a loop. The ``run`` loop catches it, logs, and
        the next pass starts from a clean budget six hours later.

        The errors in ``_UNREADABLE_CHAT_ERRORS`` are re-raised as
        :class:`_ChatUnreadable` for the same reason at chat scope: they
        say the CHAT cannot be read, so the remaining probes in it are
        known-doomed before they are spent. The caller drops the chat and
        keeps its budget for chats that can still answer.
        """
        if self._bot is None:  # pragma: no cover — guarded by sweep_once
            return None
        try:
            member = await self._bot.get_chat_member(chat_id, user_id)
        except TelegramRetryAfter:
            raise
        except _UNREADABLE_CHAT_ERRORS as exc:
            log.warning(
                "left-bonds chat unreadable (chat={c}, user={u}): {e!r}",
                c=chat_id,
                u=user_id,
                e=exc,
            )
            raise _ChatUnreadable from exc
        except Exception as exc:  # noqa: BLE001 — probe is advisory only
            log.warning(
                "left-bonds probe failed (chat={c}, user={u}): {e!r}",
                c=chat_id,
                u=user_id,
                e=exc,
            )
            return None
        return member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)


__all__ = [
    "DAYS_AFTER_LEFT_TO_END_BONDS",
    "DEFAULT_INTERVAL_SECONDS",
    "LeftBondsCleanupReport",
    "LeftBondsCleanupSweeper",
]
