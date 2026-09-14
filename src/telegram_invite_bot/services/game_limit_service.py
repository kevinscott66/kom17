"""Persistent per-user game anti-abuse caps (L-25).

The policy layer over :class:`GameLimitsRepo`. Legacy enforced three
caps per user in ``check_anti_abuse`` (bot.py:14427-14469), every one of
them scoped to ``/roulette`` alone and named after it:

* **cooldown** — ``ROULETTE_COOLDOWN_SEC`` (180s) between consecutive
  completed plays, held in the in-memory cache under the key
  ``roulette_cd_<user_id>`` (bot.py:14438-14444);
* **per-hour** — ``ROULETTE_MAX_PER_HOUR`` (8) rows in ``games`` with
  ``game = 'roulette' AND date >= datetime('now', '-1 hour')``
  (bot.py:14449);
* **per-day** — ``ROULETTE_MAX_PER_DAY`` (25) rows with
  ``game = 'roulette' AND date >= date('now')`` (bot.py:14461).

This service keeps those three values and their evaluation order, and
departs from legacy in three ways worth stating outright, because two of
them make the caps bite harder than the thing they were ported from:

1. **Counted across every game, not just roulette.** ``count_since``
   carries no ``game ==`` filter, so ``/roulette``, ``/roll`` and
   ``/flip`` draw on one shared budget. Under legacy's per-game scoping
   a player could burn the full 25 spins *and* a full 25 dice rolls in
   the same day; here 25 is the total. Strictly tighter — and it is why
   the play lock has to be a single registry covering all three games
   (see :mod:`telegram_invite_bot.games.limits`, #222-A): a cap counted
   across games that is guarded per game is not guarded at all.
2. **A rolling 24h window, not a calendar day.** Legacy compared the
   naive *local* timestamps Python wrote into ``games.date`` against
   SQLite's ``date('now')``, which is UTC. On the MSK production box
   that rolled the day counter over at 03:00 local, and the window a
   player actually faced swung anywhere between 3 and 27 hours
   depending on when they started. ``now - _DAY`` is a flat 24h for
   everyone, all the time.
3. **No developer exemption.** Legacy returned early for
   ``user_id in DEVELOPER_IDS`` (bot.py:14434-14435). Nothing here does,
   so the caps apply to the owner and to staff exactly as they apply to
   everyone else. Unlike the first two this is a real gap rather than a
   hardening, and it is deliberately left open (#222-C) until there is
   an operator-visible list of exempt ids to key it off — a hardcoded
   one would be a second source of truth for "who is staff".

The new pipeline previously enforced these via an in-memory
``RouletteLimiter``. That class was deleted by this cutover — it is named
here as history, not as something to go and read — and it had two defects
this service fixes:

1. **Resets on redeploy.** A process restart hands every abuser a fresh
   slate — the very window an automated farmer exploits around a deploy.
2. **Per-process.** Two webhook workers each grant the full cap, so the
   effective limit is ``N_workers × cap``.

Persisting the play stamps in ``economy.game_plays`` (via the repo) makes
the caps survive restarts and shared across workers — they are now *real*
in the sense the audit (L-25) asks for.

Usage contract (identical to the in-memory limiter it replaces):
``check`` first; if it allows, run the play; ``record`` ONLY after the
play actually settled. A rejected bet / blocked-by-cap attempt records
nothing, so a typo never starts a cooldown — legacy parity.

The cap *values* are read from this module's constants, which re-export
the roulette ones so a single edit moves both the in-memory and the
persistent path. "Admin-adjustable" (the audit's phrasing) is satisfied
by these being module-level ints a future settings-backed override can
point at; wiring an operator knob is out of scope for L-25's
"make the caps real" deliverable and noted as a follow-up.

The verdict enum + dataclass were shaped after the ``AbuseCheck`` the
in-memory limiter returned (deleted alongside it) so the roulette
handler's existing branch (cooldown / hour / day → localised message)
could consume this service with no new i18n keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

# Re-export the canonical cap values so the persistent path and the
# in-memory RouletteLimiter never drift. Single source of truth lives in
# roulette_service (where the legacy numbers were first ported).
from telegram_invite_bot.services.roulette_service import (
    COOLDOWN_SEC,
    MAX_PER_DAY,
    MAX_PER_HOUR,
)

if TYPE_CHECKING:
    from datetime import datetime

    from telegram_invite_bot.repositories.game_limits_repo import GameLimitsRepo

_HOUR = timedelta(seconds=3600)
_DAY = timedelta(seconds=86_400)


class GameAbuse(StrEnum):
    """Which cap blocked the play — maps 1:1 to the roulette i18n keys."""

    COOLDOWN = "cooldown"
    MAX_PER_HOUR = "max_per_hour"
    MAX_PER_DAY = "max_per_day"


@dataclass(frozen=True, slots=True)
class GameAbuseCheck:
    """Outcome of :meth:`GameLimitService.check`.

    ``allowed`` True → the play may proceed (caller MUST ``record`` after
    it settles). Otherwise ``reason`` names the cap and ``wait_sec``
    carries the cooldown remainder (only for :attr:`GameAbuse.COOLDOWN`).

    ``plays_in_hour`` / ``plays_in_day`` are the window counts as of the
    check — i.e. NOT counting the play the caller is about to make. They
    are ``None`` when the cooldown short-circuited before the counts ran
    (RR-3 #34: the success card renders the remaining allowance off
    these, and a cooldown block never reaches a success card).
    """

    allowed: bool
    reason: GameAbuse | None = None
    wait_sec: int | None = None
    plays_in_hour: int | None = None
    plays_in_day: int | None = None


class GameLimitService:
    """DB-backed anti-abuse caps for the games subsystem (L-25)."""

    def __init__(self, repo: GameLimitsRepo) -> None:
        self._repo = repo

    async def check(
        self, user_id: int, *, now: datetime, include_cooldown: bool = True
    ) -> GameAbuseCheck:
        """Evaluate cooldown → per-hour → per-day, in that order.

        Order matches legacy (cooldown bot.py:14438, hour :14449, day
        :14461) and the in-memory limiter:
        cooldown first (the most common block), then the hour cap, then
        the day cap. Read-only — never writes; the stamp is added by
        :meth:`record` only after the play settles, so a blocked or
        rejected attempt consumes no slot.

        Three reads (last-play, hour-count, day-count) rather than one
        windowed scan: each is a single indexed lookup and keeping them
        separate lets the cooldown short-circuit before the COUNTs run
        when the user is on cooldown anyway.

        ``include_cooldown=False`` evaluates the two window caps
        alone, and skips the last-play read entirely. #1664 added it
        for the PvP CHALLENGE commands (``/duel``, ``/cpc``), which
        stamp an invitation rather than a settled play. The three
        minutes exist to space out fast self-served plays that resolve
        the instant they are made; a challenge resolves minutes later
        and only if somebody else agrees, so charging it a cooldown
        would mean a player who invited a friend who then declined is
        locked out of every game in the ecosystem for having received
        nothing. It would also make the one-session-per-chat contract
        R-FIX-008 pins unreachable in practice: the same player may
        hold an independent ``/cpc`` in each group, and they cannot
        open the second one if opening the first started a cooldown.

        The exemption is one-directional on purpose. A challenge is
        still ``record``ed, so it still counts toward both windows AND
        it still starts a cooldown for everything else — the budget is
        shared, and an invitation is a real economic act. What it does
        not do is answer to a clock meant for a different shape of
        play.
        """
        last = await self._repo.last_play_at(user_id) if include_cooldown else None
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < COOLDOWN_SEC:
                wait = int(COOLDOWN_SEC - elapsed)
                # Round any sub-second remainder up so "wait 0s" never
                # shows while still blocked — parity with the deleted
                # in-memory limiter, kept so the cutover changed no
                # user-visible wording.
                wait = max(wait, 1)
                return GameAbuseCheck(allowed=False, reason=GameAbuse.COOLDOWN, wait_sec=wait)

        in_hour = await self._repo.count_since(user_id, since=now - _HOUR)
        if in_hour >= MAX_PER_HOUR:
            return GameAbuseCheck(
                allowed=False, reason=GameAbuse.MAX_PER_HOUR, plays_in_hour=in_hour
            )

        in_day = await self._repo.count_since(user_id, since=now - _DAY)
        if in_day >= MAX_PER_DAY:
            return GameAbuseCheck(
                allowed=False,
                reason=GameAbuse.MAX_PER_DAY,
                plays_in_hour=in_hour,
                plays_in_day=in_day,
            )

        return GameAbuseCheck(allowed=True, plays_in_hour=in_hour, plays_in_day=in_day)

    async def record(self, user_id: int, *, game: str, now: datetime) -> None:
        """Stamp a COMPLETED play. Call only after the play actually ran.

        Pairs with :meth:`check`: an allowed check followed by a real
        play records the stamp so the windows count only completed
        plays — never rejected or blocked attempts (legacy parity, no
        cooldown-on-typo). The write lands on the caller's economy
        session and commits with the play's settlement.
        """
        await self._repo.record(user_id, game=game, now=now)
