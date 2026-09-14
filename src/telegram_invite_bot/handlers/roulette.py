"""``/roulette`` (RUSSIAN ROULETTE) handler — A-10.

Group-chat-only port of the legacy ``/roulette`` command
(``cmd_roulette``, bot.py:21053). This is Russian roulette — a single-
player 6-chamber spin over ``economy.users.balance`` — NOT the casino
red/black wheel.

Shape & conventions (copied from :mod:`telegram_invite_bot.handlers.duel`
and :mod:`telegram_invite_bot.handlers.chatstats`):

* ``Command("roulette")`` only. Legacy also registered ``/kom_roulette``;
  the parity snapshot lists only ``roulette`` so we register just that.
* GROUP-ONLY, gated IN-HANDLER (mirrors ``chatstats``' group gate) so a
  private invocation gets a localised "only in group" refusal rather
  than silently falling through a router filter.
* HTML parse mode; ``h_roulette_*`` i18n keys.
* RNG injected via the module-level ``_rng`` so tests pin a
  deterministic shot — same ``_rng`` pattern as ``handlers.duel``.
* Anti-abuse caps (cooldown / per-hour / per-day) via the persistent
  :class:`~telegram_invite_bot.services.game_limit_service.GameLimitService`
  (L-25). DI-injected by ``EconomyMiddleware`` as ``game_limit_service``;
  the stamps live in ``economy.game_plays`` so the caps survive a
  redeploy and are shared across workers (the old in-memory
  ``RouletteLimiter`` reset on restart and was per-process).

* The remaining anti-abuse allowance is appended to every SUCCESS card
  (RR-3 #34) via :func:`~telegram_invite_bot.handlers.game_cards
  .render_allowance`, shared with ``/roll`` and ``/flip`` — the windows
  are common to all three.

Since A-11/A-12 the success path also writes the ``games`` row (with the
signed profit) and grants achievements — both inside
:class:`~telegram_invite_bot.services.roulette_service.RouletteService`,
whose ``play`` returns the freshly-awarded ids for the card.

DEFERRED — intentionally NOT built:

* The legacy ``DEVELOPER_IDS`` anti-abuse exemption — the new games
  pipeline has no dev-exempt list wired in, so the caps apply to
  everyone.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPE_NAMES
from telegram_invite_bot.games.limits import PLAY_LOCKS
from telegram_invite_bot.handlers.game_cards import (
    post_game_card,
    render_abuse_refusal,
    render_achievements,
    render_allowance,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.services.roulette_service import (
    MAX_BET,
    MIN_BET,
    RouletteOutcome,
    RouletteService,
)
from telegram_invite_bot.utils.aiogram import command_args, require_from_user
from telegram_invite_bot.utils.keyed_locks import KeyedLocks
from telegram_invite_bot.utils.numbers import parse_int_token
from telegram_invite_bot.utils.rng import money_rng

log = logger.bind(component="handlers.roulette")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
    from telegram_invite_bot.services.game_limit_service import (
        GameAbuseCheck,
        GameLimitService,
    )

# Server-side RNG. Module-level so tests can monkey-patch
# ``handlers.roulette._rng`` for a deterministic shot without touching
# the service (which takes the rng as a parameter). Mirrors
# ``handlers.duel._rng``, including the OS-backed production generator:
# a 35:1 pocket is the single most rewarding thing in this bot to be
# able to predict (utils/rng.py).
_rng = money_rng

# L-25: the anti-abuse caps are now persistent — they live in
# ``economy.game_plays`` behind the DI-injected ``GameLimitService``
# (see :mod:`telegram_invite_bot.services.game_limit_service`), so there
# is no module-level limiter singleton any more. The clock is
# ``datetime.now()`` (naive local time, matching the repo's stored-
# datetime convention); tests inject a ``GameLimitService`` over a real
# session and seed ``game_plays`` to pin window boundaries.

# Per-user locks serialising a single user's check→play→record so the
# anti-abuse caps can't be raced by two concurrent /roulette updates from
# the same user (BUG audit: a TOCTOU window let simultaneous requests both
# pass ``check`` before either ``record``). Keyed by user_id. Cross-user
# plays stay fully parallel (a lock is only contended by one user's own
# requests), and :class:`KeyedLocks` frees each slot once nobody is
# holding or waiting on it — "bounded by distinct players" was true of
# the plain dict this replaced only in the sense that it grew forever.
#
# The lock spans check -> record; the result card is outside it.
# ``record`` is a bare ``session.add``
# (repositories/game_limits_repo.py:57), so leaving the commit to the
# economy middleware (middlewares/base.py:157-158) would let a second
# update take the lock after the first released it but before that
# commit landed, and still count the *old* ``game_plays`` rows — the
# one-commit-wide #222-B window. This handler closes it by committing
# INSIDE the lock right after ``record`` (the ``await checkpoint()`` at
# :276-277). That call is load-bearing: removing it as redundant, on the
# reading that the lock already serialises everything, silently stops
# the anti-abuse caps from biting a user who fires two updates
# milliseconds apart.
#
# An ALIAS of the one ecosystem-wide registry (#222-A). The caps this
# guards are counted across every game, so /roulette and /roll have to
# queue behind each other; they used to hold two disjoint registries and
# therefore never did. ``handlers.games._stake_locks`` is the same
# object, and the leak regression below asserts on it under this name.
_play_locks: KeyedLocks[int] = PLAY_LOCKS


def _render_result(
    lang: str,
    *,
    won: bool,
    shot: int,
    bet: int,
    win_amount: int,
    balance: int,
    awarded: list[str],
    abuse: GameAbuseCheck,
) -> str:
    """Build the WIN/LOSE card + the trailing balance line.

    Legacy rendered Markdown (bot.py:14531/14534) and appended a balance
    line (bot.py:21116); here we render HTML. All interpolated values are
    integers, but every dynamic field is HTML-escaped defensively per the
    project's "escape every dynamic string" rule (utils/html.py) so a
    future non-int field cannot inject markup.

    ``abuse`` is the PRE-play check whose windows allowed this spin; it
    only feeds the trailing allowance footer (RR-3 #34).
    """
    if won:
        body = t(
            "h_roulette_win",
            lang,
            shot=html.escape(str(shot)),
            win=html.escape(str(win_amount)),
            # RR-3 #28: show the NET profit (gross win − stake) alongside.
            profit=html.escape(str(win_amount - bet)),
        )
    else:
        body = t(
            "h_roulette_lose",
            lang,
            shot=html.escape(str(shot)),
            bet=html.escape(str(bet)),
        )
    balance_line = t("h_roulette_balance", lang, balance=html.escape(str(balance)))
    return (
        f"{body}{balance_line}{render_achievements(lang, awarded)}{render_allowance(lang, abuse)}"
    )


async def handle_roulette(
    message: Message,
    command: CommandObject,
    economy_repo: EconomyRepo,
    transactions_repo: TransactionsRepo,
    game_limit_service: GameLimitService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/roulette <bet>`` — one Russian-roulette spin in a group chat."""
    tg_user = require_from_user(message)

    # Group-only — in-handler gate (mirrors chatstats / legacy
    # require_group=True). A private call gets the localised refusal.
    if message.chat.type not in GROUP_TYPE_NAMES:
        await message.answer(t("h_roulette_group_only", lang))
        return

    # Grammar: /roulette <bet>. Missing/non-int → usage. No "all"/К/М
    # suffixes/floats (legacy parity). ``parse_int_token`` rather than a
    # bare ``int()``: the bare call also takes Arabic-Indic digits and
    # ``_`` separators, so ``/roulette ١٠٠`` and ``/roulette 1_000``
    # would start a real game (utils/numbers.py:102-108).
    raw = command_args(command)
    parts = raw.split()
    if not parts:
        await message.answer(t("h_roulette_usage", lang, min_bet=MIN_BET, max_bet=MAX_BET))
        return
    bet = parse_int_token(parts[0])
    if bet is None:
        await message.answer(t("h_roulette_invalid_bet", lang))
        return

    # Anti-abuse: cooldown / per-hour / per-day (L-25, persistent). ``check``
    # is read-only — a blocked play never debits, and (unlike a naive
    # check-and-stamp) a play that's about to be REJECTED for an invalid bet
    # / insufficient funds won't burn a cooldown slot here. The completed
    # play is stamped via ``game_limit_service.record`` only after a SUCCESS
    # spin below, matching legacy (which counted ``games`` rows written on
    # result.save()). A single ``now`` is captured for both check and record
    # so the cooldown reference and the stamp agree to the microsecond.
    #
    # The whole check→play→record sequence runs under a per-user lock so
    # two concurrent updates from the same user can't both pass ``check``
    # before either ``record`` (the TOCTOU the BUG audit flagged).
    now = datetime.now()  # noqa: DTZ005 — naive local, matches game_plays
    async with _play_locks.acquire(tg_user.id):
        abuse = await game_limit_service.check(tg_user.id, now=now)
        if not abuse.allowed:
            await message.answer(render_abuse_refusal(lang, abuse))
            log.bind(uid=tg_user.id, reason=abuse.reason).info("/roulette blocked")
            return

        service = RouletteService(economy_repo, transactions_repo)
        result = await service.play(user_id=tg_user.id, bet=bet, rng=_rng)

        if result.outcome is RouletteOutcome.BELOW_MIN_BET:
            await message.answer(t("h_roulette_min_bet", lang, min_bet=MIN_BET))
            return
        if result.outcome is RouletteOutcome.ABOVE_MAX_BET:
            await message.answer(t("h_roulette_max_bet", lang, max_bet=MAX_BET))
            return
        if result.outcome in (
            RouletteOutcome.NO_WALLET,
            RouletteOutcome.INSUFFICIENT_FUNDS,
        ):
            # #1967: ``INSUFFICIENT_FUNDS`` can arrive from either of two
            # places in ``RouletteService.play`` — the affordability
            # pre-check (a plain SELECT, nothing to release) or a
            # ``debit`` whose ``WHERE balance >= amount`` matched zero
            # rows. The second one is the lost race, and a guarded
            # UPDATE that changes nothing still promotes the
            # transaction to ``BEGIN IMMEDIATE`` (``db/engines.py``):
            # ``economy.db`` is then locked over a write that never
            # happened, for the whole Telegram round trip below, and
            # SQLite's ``busy_timeout`` is 5s. Release it first, exactly
            # as ``/daily`` does on RACE_LOST (#1861). No wallet at all
            # returns before the UPDATE and has nothing to release; the
            # checkpoint is a no-op there.
            if checkpoint is not None:
                await checkpoint()
            await message.answer(t("h_roulette_insufficient", lang, balance=result.balance or 0))
            return

        # SUCCESS — the spin actually happened, so stamp the play against
        # the anti-abuse caps now (rejected outcomes above returned without
        # recording, so a typo'd bet never triggers a cooldown). The stamp
        # lands on the same economy session as the wallet settlement, so it
        # commits atomically with the debit/credit when the middleware
        # commits after this handler returns.
        await game_limit_service.record(tg_user.id, game="roulette", now=now)
        # #222-B: end the write transaction HERE, inside the lock, not
        # when the middleware gets round to it after the handler returns.
        # ``record`` is a bare ``session.add``, so until something
        # commits it the stamp is invisible to every other connection —
        # and the next update from this same player takes the lock the
        # moment this one releases it, reads ``game_plays`` without the
        # row, and is waved through. The lock alone only narrowed that
        # race from one handler wide to one commit wide; this closes it.
        #
        # Safe because the play is over. The RNG rolled, the wallet
        # moved, and nothing below writes to a database at all — the
        # card is a Telegram call. What changes is that a failure while
        # sending the card no longer rolls the play back, which is the
        # behaviour we want: an undo there would refund a lost bet, claw
        # back a win, and erase the anti-abuse stamp — the last of those
        # being precisely what an abuser would arrange to have happen.
        if checkpoint is not None:
            await checkpoint()

    # Render the WIN/LOSE card + balance line. ``balance`` is always
    # populated on success.
    assert result.balance is not None
    await post_game_card(
        message,
        _render_result(
            lang,
            won=result.won,
            shot=result.shot,
            bet=result.bet,
            win_amount=result.win_amount,
            balance=result.balance,
            awarded=result.awarded,
            abuse=abuse,
        ),
    )
    log.bind(uid=tg_user.id, bet=result.bet, shot=result.shot, won=result.won).info(
        "/roulette played"
    )


def build_router(registry: EngineRegistry) -> Router:
    """Factory — fresh router + ``EconomyMiddleware`` per call.

    Group gate lives in the handler (not a router filter) so a private
    invocation gets the localised refusal, mirroring ``/chatstats`` and
    the legacy ``require_group=True`` posture. ``EconomyMiddleware``
    injects ``economy_repo`` for the atomic debit/credit AND
    ``game_limit_service`` (L-25) for the persistent anti-abuse caps —
    both ride the same economy session, so the wallet write and the
    ``game_plays`` stamp commit together.
    """
    router = Router(name="roulette")
    router.message.middleware(EconomyMiddleware(registry))
    router.message.register(
        handle_roulette,
        Command("roulette", ignore_case=True),
        F.from_user,
    )
    return router
