"""``/duel`` (PvP dice game) handler — T-018.

Group-chat only port of the legacy /duel command (bot.py:21212). Same
shape as :mod:`telegram_invite_bot.handlers.rps`:

* Two FSM states (``awaiting_acceptance``, ``awaiting_rolls``) owned
  by the challenger (Variant A — see :mod:`telegram_invite_bot.fsm.rps`).
* Atomic escrow only at the resolution step, never at challenge
  creation (ADR 0009 no-escrow).
* HTML parse mode.
* i18n keys with ``h_duel_*`` prefix.
* Sweeper-compatible — every ``set_state`` stamps ``state_entered_at``.

Flow
----
1. ``/duel <bet> [wins]`` in a group chat, replying to the opponent's
   message:

   * Parse args; reject unparseable / missing / non-reply.
   * Cheap validators (same player, bet range, wins range, busy guard).
   * Set FSM ``awaiting_acceptance`` on the challenger's group-chat
     key with ``{opponent_id, bet, chat_id, state_entered_at,
     challenge_message_id}``.
   * Send the challenge card with Accept/Decline buttons in the group.

2. Accept callback: opponent clicks Accept → flip state to
   ``awaiting_rolls``, edit the card to show two "🎲 Roll" buttons.

3. Decline callback: opponent clicks Decline → clear FSM, edit the
   card to show the declined notice.

4. Roll callback: a seat clicks 🎲 Roll → server-side ``randint(1,6)``
   stamped into FSM data (challenger_roll / opponent_roll). With only
   one roll in, the card is edited to "waiting on opponent". With both
   in, the round is tallied against ``max_wins``:

   * Match still open (best-of-N, RR-2 #22) → clear both rolls, bump
     the running score + round history, restamp ``state_entered_at``
     and edit the card to the "round N done, score X:Y" shape with the
     roll keyboard intact. NO wallet write happens here.
   * Match decided → :meth:`DuelService.play` settles the stake ONCE
     on the decisive round's rolls (a tie decides nothing, so the
     round that ends the match is always one somebody won — its winner
     is the match winner), then the result card renders.

Escrow timing
-------------
No wallet write happens between ``/duel`` and the second roll click.
The handler runs balance pre-checks before sending the challenge
(read-only). A user can drain their wallet between Accept and Roll,
in which case :class:`DuelService.play` returns
``*_INSUFFICIENT_FUNDS`` and the handler renders that as a terminal
outcome with FSM cleared and no leak.

FSM key
-------
The FSM is keyed on the GROUP chat: ``StorageKey(bot_id=bot.id,
chat_id=group_chat_id, user_id=challenger_id)``. This differs from
/cpc which uses the private chat key (``chat_id == user_id``). The
helper :func:`_fsm_context_for` synthesises the key from the
challenger_id payload + the callback's resolved chat_id.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPE_NAMES
from telegram_invite_bot.fsm.duel import DuelStates
from telegram_invite_bot.games.duel import (
    DuelConfig,
    DuelOutcome,
    resolve_round,
    roll_die,
)
from telegram_invite_bot.games.limits import PLAY_LOCKS
from telegram_invite_bot.handlers.game_cards import render_abuse_refusal
from telegram_invite_bot.handlers.group_only import handle_group_only
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import (
    DuelAccept,
    DuelDecline,
    DuelRoll,
)
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.scheduler.fsm_busy import is_user_an_opponent, is_user_busy
from telegram_invite_bot.scheduler.fsm_sweeper import (
    STATE_ENTERED_AT_FIELD,
    utc_now_iso,
)
from telegram_invite_bot.services.duel_service import DuelServiceOutcome
from telegram_invite_bot.utils.aiogram import (
    command_args,
    edit_card,
    mention_html,
    require_from_user,
)
from telegram_invite_bot.utils.keyed_locks import KeyedLocks
from telegram_invite_bot.utils.numbers import parse_int_token
from telegram_invite_bot.utils.rng import money_rng

log = logger.bind(component="handlers.duel")

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from aiogram.filters import CommandObject
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.handlers.rps import AckFn, EditCardFn, RejectFn
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.services.duel_service import DuelService
    from telegram_invite_bot.services.game_limit_service import GameLimitService


# Server-side RNG. Module-level so tests can monkey-patch
# ``handlers.duel._rng`` for deterministic rolls without touching the
# pure resolver (which already supports rng injection at the game
# layer). The production generator is the OS one — a duel pot is money,
# and this stream used to be a dedicated Mersenne Twister whose every
# draw an opponent could watch (utils/rng.py).
_rng = money_rng

# Per-match serialization, mirroring the RPS R-FIX-009 fix. Without it,
# two seats clicking 🎲 Roll near-simultaneously each read the SAME
# pre-stamp FSM snapshot, each see the other's roll as absent, each
# stamp only their own key, and BOTH fall into the "waiting on
# opponent" branch — the round never resolves and hangs until the
# sweeper times it out. The lock makes the read→stamp→re-read→resolve
# section atomic per match; keyed on the same triple as the FSM
# ``StorageKey`` (bot_id, group_chat_id, challenger_id).
#
# #126-fp: the registry is a :class:`KeyedLocks`, which reference-counts
# its slots — created on the first waiter, gone when the last one
# leaves. Mirrors ``handlers/rps.py``; the long-form argument for why
# refcounting beats every hand-placed drop lives there and in
# ``utils/keyed_locks.py``. Short version: a slot can neither be popped
# out from under a live match nor left behind by a tap on a dead card.
_match_locks: KeyedLocks[tuple[int, int, int]] = KeyedLocks()


def _match_lock_cm(
    *, bot_id: int, chat_id: int, challenger_id: int
) -> AbstractAsyncContextManager[asyncio.Lock]:
    """Hold the per-match lock for the duration of the block.

    Acquires on enter, releases on exit even through an exception.
    """
    return _match_locks.acquire((bot_id, chat_id, challenger_id))


def expiry_guard(bot: Bot, key: StorageKey) -> AbstractAsyncContextManager[asyncio.Lock]:
    """The sweeper's hold on a duel while it expires it (#126).

    Handed to :class:`~telegram_invite_bot.scheduler.TimeoutRule` as
    its ``guard``, this puts the timeout path on the SAME lock the
    accept / decline / roll handlers take, so the sweeper's
    re-read → notify → clear region cannot interleave with a click.
    Without it a deadline landing at the same moment as ✅ Accept let
    the sweeper clear an FSM the accept had just advanced to
    ``awaiting_rolls`` — both seats then held roll keyboards that
    answer "match not found" forever.

    The ``StorageKey`` triple is exactly the lock key (the duel FSM
    lives under the challenger's id), so no translation is needed.
    """
    return _match_lock_cm(bot_id=bot.id, chat_id=key.chat_id, challenger_id=key.user_id)


def _build_challenge_keyboard(*, challenger_id: int, bet: int, lang: str) -> InlineKeyboardMarkup:
    """Accept / Decline pair on the challenge card."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_duel_accept_btn", lang),
                    callback_data=DuelAccept(challenger_id=challenger_id, bet=bet).pack(),
                ),
                InlineKeyboardButton(
                    text=t("h_duel_decline_btn", lang),
                    callback_data=DuelDecline(challenger_id=challenger_id).pack(),
                ),
            ]
        ]
    )


def _build_roll_keyboard(*, challenger_id: int, lang: str) -> InlineKeyboardMarkup:
    """Single 🎲 Roll button — same callback for both seats; the
    handler resolves which seat clicked via ``callback.from_user.id``.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_duel_roll_btn", lang),
                    callback_data=DuelRoll(challenger_id=challenger_id).pack(),
                )
            ]
        ]
    )


def _fsm_context_for(
    *, bot: Bot, storage: BaseStorage, chat_id: int, challenger_id: int
) -> FSMContext:
    """Build an FSMContext keyed on the group chat + challenger.

    Distinct from the /cpc helper because /duel is group-scoped: the
    key uses ``chat_id=group_chat_id`` (NOT ``user_id``) so the FSM
    session is shared across both seats clicking in the same chat.
    """
    return FSMContext(
        storage=storage,
        key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=challenger_id),
    )


def _bestof_line(lang: str, *, max_wins: int) -> str | None:
    """The "playing to N wins" line, or ``None`` for a best-of-1 match.

    RR-2 #22. ``None`` rather than an empty string so :func:`_with_status`
    can tell "nothing to add" from "add a blank line": a best-of-1 — the
    default, and every match the port could make before this — must
    render byte-identically to before. "Играем до 1 победы" would be
    noise on the shape users already know.
    """
    if max_wins <= 1:
        return None
    return t("h_duel_bestof_line", lang, max_wins=max_wins)


def _score_line(
    lang: str, *, max_wins: int, challenger_wins: int, opponent_wins: int
) -> str | None:
    """The live "📊 Счёт: X : Y" standings line.

    Same best-of-1 opt-out as :func:`_bestof_line`: a single-round match
    has no standings worth showing, only a result.
    """
    if max_wins <= 1:
        return None
    return t(
        "h_duel_score_line",
        lang,
        challenger_wins=challenger_wins,
        opponent_wins=opponent_wins,
        max_wins=max_wins,
    )


def _with_status(body: str, *lines: str | None) -> str:
    """Attach the best-of-N status block under an existing card.

    ``None`` lines are dropped; if nothing survives, ``body`` comes back
    untouched. The block is separated from the card by a blank line so
    the standings read as their own paragraph rather than a stray
    sentence glued to the last line of the copy.
    """
    present = [line for line in lines if line]
    if not present:
        return body
    return body + "\n\n" + "\n".join(present)


def _rounds_block(
    history: list[list[int]],
    lang: str,
    *,
    challenger: str,
    opponent: str,
) -> str:
    """Replay every played round as "Раунд N: 5 : 3 → <winner>".

    Legacy printed a bare 🏆 on both the challenger's and the
    opponent's win (bot.py:21454-21458), which told the reader nothing
    about who took the round. We name the winner instead — the mentions
    are already rendered for the header, so this costs nothing and
    makes the recap actually readable.
    """
    lines: list[str] = []
    for number, (challenger_roll, opponent_roll) in enumerate(history, start=1):
        if challenger_roll > opponent_roll:
            verdict = t("h_duel_round_verdict_win", lang, winner=challenger)
        elif opponent_roll > challenger_roll:
            verdict = t("h_duel_round_verdict_win", lang, winner=opponent)
        else:
            verdict = t("h_duel_round_verdict_tie", lang)
        lines.append(
            t(
                "h_duel_round_line",
                lang,
                number=number,
                challenger_roll=challenger_roll,
                opponent_roll=opponent_roll,
                verdict=verdict,
            )
        )
    return "\n".join(lines)


async def handle_duel(
    message: Message,
    command: CommandObject,
    bot: Bot,
    state: FSMContext,
    economy_repo: EconomyRepo,
    duel_service: DuelService,
    game_limit_service: GameLimitService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/duel <bet> [wins]`` (reply-to opponent's message) — challenge.

    RR-2 #22: the second argument is legacy's ``max_wins`` — the match
    runs until a seat has won that many rounds. Omitted means 1, i.e.
    exactly the single-round match this handler used to be.

    Group-chat-only, enforced by the router's ``F.chat.type`` filter.
    This body used to re-check it and answer ``h_duel_group_only``, but
    the filter runs first and the branch could never be reached (#122);
    the private half now goes to :func:`~handlers.group_only.handle_group_only`,
    registered on the same command words.
    """
    del duel_service  # injected for symmetry; called at roll-time
    del bot
    tg_user = require_from_user(message)

    # Reply-to is mandatory — that's how legacy picks the opponent.
    if message.reply_to_message is None or message.reply_to_message.from_user is None:
        await message.answer(t("h_duel_usage", lang))
        return

    opponent = message.reply_to_message.from_user
    if opponent.is_bot:
        await message.answer(t("h_duel_bot_target", lang))
        return

    raw = command_args(command)
    parts = raw.split()
    if not parts:
        await message.answer(t("h_duel_usage", lang))
        return
    # ``parse_int_token`` rather than a bare ``int()``: the bare call also
    # takes Arabic-Indic digits, surrounding whitespace and a leading sign
    # (``is_int_token`` spells that out, utils/numbers.py:102-105), plus
    # ``_`` separators, so ``/duel ١٠٠`` would stake a real 100 coins.
    # The bounds below still hold either way; this is the project's
    # parse policy, applied consistently.
    bet = parse_int_token(parts[0])
    max_wins = parse_int_token(parts[1]) if len(parts) > 1 else 1
    if bet is None or max_wins is None:
        await message.answer(t("h_duel_invalid_args", lang))
        return
    if bet <= 0:
        await message.answer(t("h_duel_non_positive_bet", lang))
        return

    opponent_id = opponent.id
    if opponent_id == tg_user.id:
        await message.answer(t("h_duel_same_player", lang))
        return

    cfg = DuelConfig()
    if bet < cfg.min_bet or bet > cfg.max_bet:
        await message.answer(
            t("h_duel_invalid_bet", lang, min_bet=cfg.min_bet, max_bet=cfg.max_bet)
        )
        return
    if max_wins < 1 or max_wins > cfg.max_wins_cap:
        await message.answer(t("h_duel_invalid_rounds", lang, max_wins=cfg.max_wins_cap))
        return

    # Balance pre-check (read-only — no escrow at challenge time,
    # ADR 0009). The service re-validates at resolve as the floor.
    challenger_wallet = await economy_repo.get(tg_user.id)
    if challenger_wallet is None or challenger_wallet.balance < bet:
        await message.answer(t("h_duel_insufficient_funds_challenger", lang, bet=bet))
        return
    opponent_wallet = await economy_repo.get(opponent_id)
    if opponent_wallet is None or opponent_wallet.balance < bet:
        await message.answer(t("h_duel_insufficient_funds_opponent", lang, bet=bet))
        return

    # Already-in-game guard, first half — mirrors RPS Stage 34
    # posture. The challenger's own FSM state in THIS group chat is
    # a busy flag.
    prior = await state.get_state()
    if prior is not None:
        await message.answer(t("h_duel_already_in_game", lang))
        return

    # Second half (#1517), twin of the /cpc guard. A match lives on
    # the challenger's key alone, so the check above never sees a
    # match the caller was invited into: an acceptor is mid-duel with
    # an empty key of their own and would sail straight into a
    # second one. Scan for the opponent seat.
    #
    # The OPPONENT seat only, matching /cpc: a duel key is already
    # per-group (``chat_id`` is the group, not the user), so counting
    # the challenger seat across chats would revoke the same
    # one-session-per-chat property R-FIX-008 pins for /cpc. The
    # two-seat predicate belongs to the accept path below.
    #
    # No ``exclude_key`` — the caller's own key was just proven
    # empty, so there is nothing of theirs for the scan to trip over.
    if await is_user_an_opponent(state.storage, user_id=tg_user.id):
        await message.answer(t("h_duel_already_in_game", lang))
        log.bind(uid=tg_user.id).info("/duel rejected — opponent of another match")
        return

    # #1664: the ecosystem-wide anti-abuse caps, which this command had
    # never run. ``GameLimitsRepo`` counts plays on ``user_id`` alone,
    # with no ``game ==`` clause, so /roll, /flip, /roulette, /pvp_coin
    # and /pvp_dice were already drawing on one shared budget while
    # /duel and /cpc — the two with the largest single-match exposure —
    # drew on nothing. A player who spent all 25 daily slots on
    # /roulette simply moved here and kept going.
    #
    # What is stamped is the CHALLENGE, not the settled match. A duel
    # settles rounds later, on someone else's update, so there is no
    # single handler in which check and record could bracket the play
    # the way /roulette's do. The challenge is the act the caller
    # chose, it is what puts a live card in front of a group, and it is
    # therefore the thing worth rate-limiting. The other seat is
    # stamped where it makes its own choice — see the accept path.
    #
    # Placed after every parse and balance rejection above, so a typo
    # still never starts a cooldown, and after the busy guards, so a
    # caller already in a match is refused for that reason rather than
    # spending a slot on it.
    now = datetime.now()  # naive local — matches what game_plays stores
    async with PLAY_LOCKS.acquire(tg_user.id):
        abuse = await game_limit_service.check(tg_user.id, now=now, include_cooldown=False)
        if not abuse.allowed:
            await message.answer(render_abuse_refusal(lang, abuse))
            log.bind(uid=tg_user.id, reason=abuse.reason).info("/duel blocked")
            return

        await state.set_state(DuelStates.awaiting_acceptance)
        await state.set_data(
            {
                "opponent_id": opponent_id,
                "bet": bet,
                "chat_id": message.chat.id,
                # Best-of-N bookkeeping (RR-2 #22). Seeded here rather than
                # at accept so the running score survives the whole match
                # in one place; ``rounds`` is the per-round roll history the
                # final card replays.
                "max_wins": max_wins,
                "challenger_wins": 0,
                "opponent_wins": 0,
                "rounds": [],
                # Stamp the effective language so the sweeper's expiry
                # callbacks (which run outside any update and therefore see
                # no LanguageMiddleware injection) can localise the visible
                # expiry edit instead of falling back to Russian.
                "lang": lang,
                STATE_ENTERED_AT_FIELD: utc_now_iso(),
            }
        )

        keyboard = _build_challenge_keyboard(challenger_id=tg_user.id, bet=bet, lang=lang)
        card = t(
            "h_duel_challenge_card",
            lang,
            challenger_id=tg_user.id,
            opponent_id=opponent_id,
            bet=bet,
        )
        challenge_msg = await message.answer(
            _with_status(card, _bestof_line(lang, max_wins=max_wins)),
            reply_markup=keyboard,
        )
        await state.update_data(challenge_message_id=challenge_msg.message_id)
        # The challenge is live: the card is posted and the FSM points
        # at it. Stamp it, then end the write transaction HERE, inside
        # the lock — ``record`` is a bare ``session.add`` (#222-B), so
        # until something commits it the row is invisible to every other
        # connection, and the caller's next update takes this lock the
        # moment it releases and reads a ``game_plays`` table that still
        # does not know about this challenge.
        await game_limit_service.record(tg_user.id, game="duel", now=now)
        if checkpoint is not None:
            await checkpoint()

    log.bind(
        uid=tg_user.id,
        opponent_id=opponent_id,
        bet=bet,
        max_wins=max_wins,
        chat_id=message.chat.id,
    ).info("/duel challenge sent")


async def accept_duel_challenge(
    *,
    bot: Bot,
    fsm_storage: BaseStorage,
    lang: str,
    chat_id: int,
    challenger_id: int,
    acceptor_id: int,
    payload_bet: int | None,
    game_limit_service: GameLimitService,
    reject: RejectFn,
    ack: AckFn,
    edit_card: EditCardFn,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Shared accept core — the FSM/business half of "opponent accepts".

    L-02: extracted verbatim from the callback handler so the /accept
    standalone command (handlers/challenge_commands.py) drives the
    SAME transitions. ``payload_bet`` is the wire-carried bet on the
    callback path (M-G-1 tamper guard); the command path passes
    ``None`` — there is no payload to forge, the FSM bet is the only
    bet. ``edit_card`` swaps the group challenge card for the roll
    keyboard (callback: edit-in-place; command: edit by stored
    ``challenge_message_id`` with fresh-send fallback).
    """
    challenger_state = _fsm_context_for(
        bot=bot,
        storage=fsm_storage,
        chat_id=chat_id,
        challenger_id=challenger_id,
    )
    # #1664: the acceptor spends a slot of the SAME shared budget the
    # challenger spent when the card went up — accepting a duel is
    # playing one. Gating only the challenger would leave the whole hole
    # open from the other seat: a player who exhausted the day on
    # /roulette cannot start a match, but could still be walked through
    # an unlimited number of them by a friend who types /duel.
    #
    # ``include_cooldown=False`` for the same reason the challenge
    # commands pass it: the 180 s clock spaces out self-served plays
    # that settle the instant they are made, and an accept lands on
    # somebody else's schedule, not the acceptor's.
    #
    # PLAY_LOCKS is taken OUTSIDE the per-match lock. Every other holder
    # of PLAY_LOCKS takes no match lock at all, and every holder of a
    # match lock (rolls, decline, the sweeper) takes no play lock, so
    # this is the only place the two meet and the order cannot invert.
    now = datetime.now()  # naive local — matches what game_plays stores
    async with PLAY_LOCKS.acquire(acceptor_id):
        abuse = await game_limit_service.check(acceptor_id, now=now, include_cooldown=False)
        if not abuse.allowed:
            await reject(render_abuse_refusal(lang, abuse))
            log.bind(
                challenger_id=challenger_id,
                clicker=acceptor_id,
                reason=abuse.reason,
            ).info("/duel accept blocked")
            return

        # #126: the whole check → flip region runs under the per-match
        # lock, the same one the roll handler and the expiry sweeper
        # take. Without it two ✅ Accept clicks (or an accept racing the
        # deadline) each read ``awaiting_acceptance`` and both proceed —
        # one of them flipping a match the other had already resolved.
        # ``is_user_busy`` only reads storage and takes no lock of its
        # own, so it cannot deadlock here; the card I/O below is
        # deliberately left outside the lock.
        async with _match_lock_cm(bot_id=bot.id, chat_id=chat_id, challenger_id=challenger_id):
            state_name = await challenger_state.get_state()
            if state_name != DuelStates.awaiting_acceptance.state:
                await reject(t("h_duel_match_not_found", lang))
                return
            data = await challenger_state.get_data()
            if data.get("opponent_id") != acceptor_id:
                await reject(t("h_duel_not_your_match", lang))
                return

            bet = int(data["bet"])
            # M-G-1: tamper guard — payload bet must match FSM bet. The
            # authoritative stake lives in FSM data; the wire field is
            # decorative. A mismatch is a forged-button signal: log + silently
            # reject without flipping state. Mirrors the /cpc accept-handler
            # guard.
            if payload_bet is not None and payload_bet != bet:
                log.bind(
                    challenger_id=challenger_id,
                    clicker=acceptor_id,
                    payload_bet=payload_bet,
                    fsm_bet=bet,
                ).warning("/duel accept rejected: payload bet != FSM bet")
                await reject(t("h_duel_match_not_found", lang))
                return
            # M-G-2: busy guard — mirrors the /cpc accept-handler posture.
            # Refuse to flip the clicker into the rolls stage if they are
            # already part of any other active match (RPS or duel), from
            # EITHER seat (#1517 — the original scan matched the opponent
            # seat only, so another match's CHALLENGER read as free).
            # Surfaces the localized "already in a duel" toast; the
            # existing match stays untouched.
            if await is_user_busy(
                fsm_storage,
                user_id=acceptor_id,
                exclude_key=challenger_state.key,
            ):
                log.bind(
                    challenger_id=challenger_id,
                    clicker=acceptor_id,
                ).info("/duel accept rejected: clicker already in another match")
                await reject(t("h_duel_already_in_game", lang))
                return
            await challenger_state.set_state(DuelStates.awaiting_rolls)
            await challenger_state.update_data(
                {
                    "challenger_roll": None,
                    "opponent_roll": None,
                    STATE_ENTERED_AT_FIELD: utc_now_iso(),
                }
            )

        # The flip is in storage, so the match is live and this seat is
        # in it — every refusal above returned without reaching here.
        # ``record`` is a bare ``session.add`` (#222-B), so commit it
        # inside the lock: until something commits, the row is invisible
        # to every other connection, and this user's next game takes
        # this lock the moment it releases.
        await game_limit_service.record(acceptor_id, game="duel", now=now)
        if checkpoint is not None:
            await checkpoint()

    keyboard = _build_roll_keyboard(challenger_id=challenger_id, lang=lang)
    max_wins = int(data.get("max_wins") or 1)
    await ack()
    accepted = t(
        "h_duel_accepted",
        lang,
        challenger_id=challenger_id,
        opponent_id=acceptor_id,
        bet=bet,
    )
    await edit_card(
        _with_status(
            accepted,
            _bestof_line(lang, max_wins=max_wins),
            _score_line(lang, max_wins=max_wins, challenger_wins=0, opponent_wins=0),
        ),
        keyboard,
    )
    log.bind(
        challenger_id=challenger_id,
        opponent_id=acceptor_id,
        bet=bet,
        max_wins=max_wins,
        chat_id=chat_id,
    ).info("/duel accepted")


async def handle_duel_accept(
    callback: CallbackQuery,
    callback_data: DuelAccept,
    bot: Bot,
    fsm_storage: BaseStorage,
    game_limit_service: GameLimitService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Opponent clicked ✅ Accept on the challenge card.

    Thin wrapper over :func:`accept_duel_challenge` — closures carry
    the callback-specific UI effects (toasts, edit the clicked card
    in place); the core owns every FSM transition. L-02's /accept
    command builds the same core with message-reply closures.
    """
    assert callback.from_user is not None

    if not isinstance(callback.message, MessageType):
        await callback.answer(t("h_duel_match_not_found", lang), show_alert=False)
        return
    card = callback.message
    chat_id = card.chat.id

    async def _reject(text: str) -> None:
        await callback.answer(text, show_alert=False)

    async def _ack() -> None:
        await callback.answer()

    async def _edit_card(text: str, keyboard: InlineKeyboardMarkup | None) -> int | None:
        # Narrower than the blanket ``suppress`` this used to be: two
        # seats can resolve the same card concurrently, so a lost race
        # ("not modified" / the card already gone) is expected — but a
        # malformed card is our bug and must not vanish silently.
        await edit_card(card, text, reply_markup=keyboard)
        return card.message_id

    await accept_duel_challenge(
        bot=bot,
        fsm_storage=fsm_storage,
        lang=lang,
        chat_id=chat_id,
        challenger_id=callback_data.challenger_id,
        acceptor_id=callback.from_user.id,
        payload_bet=callback_data.bet,
        game_limit_service=game_limit_service,
        reject=_reject,
        ack=_ack,
        edit_card=_edit_card,
        checkpoint=checkpoint,
    )


async def decline_duel_challenge(
    *,
    bot: Bot,
    fsm_storage: BaseStorage,
    lang: str,
    chat_id: int,
    challenger_id: int,
    decliner_id: int,
    reject: RejectFn,
    ack: AckFn,
    edit_card: EditCardFn,
) -> None:
    """Shared decline core — clear FSM, retire the challenge card.

    L-02: extracted from the callback handler so the /decline
    standalone command drives the same terminal transition.
    """
    challenger_state = _fsm_context_for(
        bot=bot,
        storage=fsm_storage,
        chat_id=chat_id,
        challenger_id=challenger_id,
    )
    # #126: check → clear under the per-match lock. An accept arriving at
    # the same moment now queues behind us and re-reads a cleared FSM
    # instead of flipping a declined match into the rolls stage. The
    # card edit stays outside the lock.
    async with _match_lock_cm(bot_id=bot.id, chat_id=chat_id, challenger_id=challenger_id):
        state_name = await challenger_state.get_state()
        if state_name != DuelStates.awaiting_acceptance.state:
            await reject(t("h_duel_match_not_found", lang))
            return
        data = await challenger_state.get_data()
        if data.get("opponent_id") != decliner_id:
            await reject(t("h_duel_not_your_match", lang))
            return

        await challenger_state.clear()
        await ack()
    await edit_card(
        t(
            "h_duel_declined",
            lang,
            challenger_id=challenger_id,
            opponent_id=decliner_id,
        ),
        None,
    )
    log.bind(
        challenger_id=challenger_id,
        opponent_id=decliner_id,
        chat_id=chat_id,
    ).info("/duel declined")


async def handle_duel_decline(
    callback: CallbackQuery,
    callback_data: DuelDecline,
    bot: Bot,
    fsm_storage: BaseStorage,
    lang: str,
) -> None:
    """Opponent clicked ❌ Decline. Clear FSM, edit card.

    Thin wrapper over :func:`decline_duel_challenge` (see
    :func:`handle_duel_accept` for the closure rationale).
    """
    assert callback.from_user is not None

    if not isinstance(callback.message, MessageType):
        await callback.answer(t("h_duel_match_not_found", lang), show_alert=False)
        return
    card = callback.message

    async def _reject(text: str) -> None:
        await callback.answer(text, show_alert=False)

    async def _ack() -> None:
        await callback.answer()

    async def _edit_card(text: str, keyboard: InlineKeyboardMarkup | None) -> int | None:
        # Narrower than the blanket ``suppress`` this used to be: two
        # seats can resolve the same card concurrently, so a lost race
        # ("not modified" / the card already gone) is expected — but a
        # malformed card is our bug and must not vanish silently.
        await edit_card(card, text, reply_markup=keyboard)
        return card.message_id

    await decline_duel_challenge(
        bot=bot,
        fsm_storage=fsm_storage,
        lang=lang,
        chat_id=card.chat.id,
        challenger_id=callback_data.challenger_id,
        decliner_id=callback.from_user.id,
        reject=_reject,
        ack=_ack,
        edit_card=_edit_card,
    )


async def handle_duel_roll(
    callback: CallbackQuery,
    callback_data: DuelRoll,
    bot: Bot,
    fsm_storage: BaseStorage,
    duel_service: DuelService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Either seat clicks 🎲 Roll.

    Branches:

    * Wrong state → silent toast.
    * Cross-user click → silent toast.
    * Only this seat has rolled → stamp + edit "waiting on opponent".
    * Both seats rolled, match still open (best-of-N, RR-2 #22) → tally
      the round, clear both rolls and edit the card to "round N done,
      score X:Y". No wallet is touched.
    * Both seats rolled, match decided → call :meth:`DuelService.play`
      once on the decisive rolls, render the result.
    """
    assert callback.from_user is not None

    if not isinstance(callback.message, MessageType):
        await callback.answer(t("h_duel_match_not_found", lang), show_alert=False)
        return
    chat_id = callback.message.chat.id

    challenger_state = _fsm_context_for(
        bot=bot,
        storage=fsm_storage,
        chat_id=chat_id,
        challenger_id=callback_data.challenger_id,
    )

    # Serialize the whole read→stamp→re-read→resolve section per match.
    # Without the lock two near-simultaneous clicks both observe a
    # roll-less snapshot and both stall in the "waiting" branch.
    async with _match_lock_cm(
        bot_id=bot.id, chat_id=chat_id, challenger_id=callback_data.challenger_id
    ):
        state_name = await challenger_state.get_state()
        if state_name != DuelStates.awaiting_rolls.state:
            await callback.answer(t("h_duel_match_not_found", lang), show_alert=False)
            return
        data = await challenger_state.get_data()
        opponent_id = int(data["opponent_id"])
        bet = int(data["bet"])
        max_wins = int(data.get("max_wins") or 1)
        clicker = callback.from_user.id
        clicker_name = callback.from_user.first_name
        if clicker not in (callback_data.challenger_id, opponent_id):
            await callback.answer(t("h_duel_not_your_match", lang), show_alert=False)
            return

        is_challenger = clicker == callback_data.challenger_id
        key = "challenger_roll" if is_challenger else "opponent_roll"
        if data.get(key) is not None:
            await callback.answer(t("h_duel_already_rolled", lang), show_alert=False)
            return

        my_roll = roll_die(rng=_rng)
        # Stash this seat's display name alongside the roll so the result
        # card (#24) can mention both players by name — the other seat's
        # name was stamped on its own click, so by resolution both are in
        # the FSM data.
        name_key = "challenger_name" if is_challenger else "opponent_name"
        await challenger_state.update_data({key: my_roll, name_key: clicker_name})
        # Re-read AFTER the stamp so the other seat's roll reflects any
        # write that landed before we acquired the lock — never the
        # pre-stamp ``data`` snapshot (the stale read that let both
        # seats miss each other and hang the round).
        fresh = await challenger_state.get_data()
        other_roll = fresh.get("opponent_roll" if is_challenger else "challenger_roll")

        await callback.answer(t("h_duel_rolled_toast", lang, roll=my_roll), show_alert=False)

        if other_roll is None:
            # Edit the card to say this seat has rolled and we are
            # waiting on the other side. Keyboard stays so the second
            # seat can still roll.
            #
            # #1663: the value itself is deliberately NOT on this card.
            # It used to be, and the card is shared and public, so the
            # second seat read the number it had to beat before it had
            # committed anything — and clicking Roll is the only act
            # that puts its money at risk. Waiting was therefore strictly
            # better than playing, at the default best-of-1 too. The
            # roller still learns its own value privately, through the
            # ``h_duel_rolled_toast`` answer on its own callback.
            waiting = t(
                "h_duel_waiting_other",
                lang,
                bet=bet,
                seat_name=mention_html(clicker, clicker_name),
            )
            await edit_card(
                callback.message,
                _with_status(
                    waiting,
                    _score_line(
                        lang,
                        max_wins=max_wins,
                        challenger_wins=int(fresh.get("challenger_wins") or 0),
                        opponent_wins=int(fresh.get("opponent_wins") or 0),
                    ),
                ),
                reply_markup=_build_roll_keyboard(
                    challenger_id=callback_data.challenger_id, lang=lang
                ),
            )
            return

        # Both rolled — the round is decided.
        if is_challenger:
            challenger_roll = my_roll
            opponent_roll = int(other_roll)
        else:
            opponent_roll = my_roll
            challenger_roll = int(other_roll)

        # RR-2 #22: tally the round locally (pure resolver, no wallet
        # touched) and settle only once the match is actually over. A
        # per-round settlement would move the stake N times — legacy
        # moves it once, at ``Duel.finish``.
        round_outcome = resolve_round(challenger_roll, opponent_roll, bet=bet).outcome
        challenger_wins = int(fresh.get("challenger_wins") or 0)
        opponent_wins = int(fresh.get("opponent_wins") or 0)
        if round_outcome is DuelOutcome.CHALLENGER_WIN:
            challenger_wins += 1
        elif round_outcome is DuelOutcome.OPPONENT_WIN:
            opponent_wins += 1
        history: list[list[int]] = [
            *(fresh.get("rounds") or []),
            [challenger_roll, opponent_roll],
        ]
        challenger_name = fresh.get("challenger_name")
        opponent_name = fresh.get("opponent_name")

        # A tied round advances nobody, so in a best-of-N it is simply
        # replayed — that is legacy's rule (``is_finished`` only ever
        # checks the win counters, bot.py:15209) and the only one that
        # makes best-of-N work at all. Best-of-1 keeps the port's
        # existing terminal draw: with a single round on offer, a tied
        # round IS a tied match, and refunding beats an endless reroll
        # of the one thing the players agreed to play.
        if max_wins > 1 and challenger_wins < max_wins and opponent_wins < max_wins:
            await challenger_state.update_data(
                {
                    "challenger_roll": None,
                    "opponent_roll": None,
                    "challenger_wins": challenger_wins,
                    "opponent_wins": opponent_wins,
                    "rounds": history,
                    # Restamp so the sweeper's roll timeout is measured
                    # from THIS round, not from the first one.
                    STATE_ENTERED_AT_FIELD: utc_now_iso(),
                }
            )
            await edit_card(
                callback.message,
                t(
                    "h_duel_round_card",
                    lang,
                    number=len(history),
                    challenger=mention_html(callback_data.challenger_id, challenger_name),
                    opponent=mention_html(opponent_id, opponent_name),
                    challenger_roll=challenger_roll,
                    opponent_roll=opponent_roll,
                    challenger_wins=challenger_wins,
                    opponent_wins=opponent_wins,
                    max_wins=max_wins,
                    next_number=len(history) + 1,
                ),
                reply_markup=_build_roll_keyboard(
                    challenger_id=callback_data.challenger_id, lang=lang
                ),
            )
            log.bind(
                challenger_id=callback_data.challenger_id,
                opponent_id=opponent_id,
                round=len(history),
                score=f"{challenger_wins}:{opponent_wins}",
                max_wins=max_wins,
            ).info("/duel round resolved, match continues")
            return

        result = await duel_service.play(
            challenger_id=callback_data.challenger_id,
            opponent_id=opponent_id,
            challenger_roll=challenger_roll,
            opponent_roll=opponent_roll,
            bet=bet,
        )
        # #1665: commit the settled match before touching Telegram
        # again. ``BaseSessionMiddleware`` commits on return and rolls
        # back on raise, and everything below this line is network I/O:
        # ``edit_card`` re-raises anything outside ``BENIGN_EDIT_REJECTS``
        # and does not catch ``TelegramForbiddenError``,
        # ``TelegramRetryAfter`` or a socket error at all. Without the
        # checkpoint, a bot kicked from the group between the settlement
        # and the result card unwinds the whole economy session — both
        # holds, the winner's credit, both ``bump_totals``, both
        # ``record_game`` rows and every ledger row — while the FSM,
        # which is not transactional, has already been cleared. The coin
        # position stays consistent (both stakes come back), so this is
        # not lost money; it is a settled match silently voided and
        # unreplayable. The sibling handlers say the same thing in their
        # own words (``pvp_stake._accept``, ``games.handle_roll_bet``).
        if checkpoint is not None:
            await checkpoint()
        await challenger_state.clear()

        # Player display names captured at roll time (#24) — both seats
        # have clicked by now, so both names are in the FSM data.
        challenger_mention = mention_html(callback_data.challenger_id, challenger_name)
        opponent_mention = mention_html(opponent_id, opponent_name)
        outcome = result.outcome
        if outcome in (
            DuelServiceOutcome.SUCCESS_CHALLENGER_WIN,
            DuelServiceOutcome.SUCCESS_OPPONENT_WIN,
        ):
            winner = (
                challenger_mention
                if outcome is DuelServiceOutcome.SUCCESS_CHALLENGER_WIN
                else opponent_mention
            )
            payout = result.round_result.payout if result.round_result else 0
            # T-020/R8: the card names the house cut instead of quietly
            # paying less than the 2× players remember from legacy.
            rake = result.round_result.rake if result.round_result else 0
            if max_wins > 1:
                # Best-of-N (RR-2 #22): the single decisive roll pair is
                # the least interesting part of the story by now, so the
                # card leads with the standings and replays every round.
                body = t(
                    "h_duel_match_result",
                    lang,
                    winner=winner,
                    challenger_wins=challenger_wins,
                    opponent_wins=opponent_wins,
                    max_wins=max_wins,
                    payout=payout,
                    rake=rake,
                    rounds=_rounds_block(
                        history,
                        lang,
                        challenger=challenger_mention,
                        opponent=opponent_mention,
                    ),
                )
            else:
                body = t(
                    "h_duel_result_win",
                    lang,
                    winner=winner,
                    challenger_roll=challenger_roll,
                    opponent_roll=opponent_roll,
                    payout=payout,
                    rake=rake,
                )
        elif outcome is DuelServiceOutcome.SUCCESS_TIE:
            body = t(
                "h_duel_result_tie",
                lang,
                challenger=challenger_mention,
                opponent=opponent_mention,
                challenger_roll=challenger_roll,
                opponent_roll=opponent_roll,
                bet=bet,
            )
        elif outcome is DuelServiceOutcome.CHALLENGER_INSUFFICIENT_FUNDS:
            body = t("h_duel_resolve_insufficient_challenger", lang)
        elif outcome is DuelServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS:
            body = t("h_duel_resolve_insufficient_opponent", lang)
        else:
            body = t("h_duel_match_not_found", lang)
            log.bind(
                challenger_id=callback_data.challenger_id,
                opponent_id=opponent_id,
                outcome=outcome.value,
            ).error("/duel unexpected service outcome")

        await edit_card(callback.message, body, reply_markup=None)
        log.bind(
            challenger_id=callback_data.challenger_id,
            opponent_id=opponent_id,
            outcome=outcome.value,
            bet=bet,
            challenger_roll=challenger_roll,
            opponent_roll=opponent_roll,
            max_wins=max_wins,
            score=f"{challenger_wins}:{opponent_wins}",
            rounds_played=len(history),
        ).info("/duel resolved")


# ── Sweeper timeout callbacks ──────────────────────────────────────────


async def _drop_keyboard(bot: Bot, chat_id: int, message_id: int | None) -> None:
    """Best-effort ``edit_message_reply_markup(None)`` on a stored id.

    Mirrors the helper in :mod:`telegram_invite_bot.handlers.rps`.
    Swallows the common race failures (message already deleted,
    already edited, bot forbidden) — every caller's terminal action is
    "match is dead", so a failed keyboard-drop is cosmetic once the
    FSM is cleared.
    """
    if message_id is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        log.bind(chat_id=chat_id, message_id=message_id).debug(
            "drop_keyboard swallowed; non-fatal at terminal-state cleanup",
        )


async def _edit_to_expired(bot: Bot, chat_id: int, message_id: int, text: str) -> bool:
    """Best-effort ``edit_message_text`` of the challenge card to the
    expiry notice (keyboard dropped in the same call).

    L-16: legacy edits the original duel card in place when the accept
    window lapses; posting a *new* timeout message left the stale card
    (with its dead buttons) in chat history. Returns ``True`` when the
    edit landed so the caller can skip the send-fallback.
    """
    try:
        await bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        log.bind(chat_id=chat_id, message_id=message_id).debug(
            "expiry edit swallowed; falling back to keyboard-drop + notice",
        )
        return False
    return True


def _lang_from_fsm(data: dict[str, object]) -> str:
    """Language stamped at challenge time; Russian when absent (legacy
    default and the pre-stamp matches the sweeper's old behaviour)."""
    lang = data.get("lang")
    return lang if isinstance(lang, str) else "ru"


async def _expire_duel(
    bot: Bot, key: StorageKey, data: dict[str, object], *, stage: str, text_key: str
) -> None:
    """Shared expiry path for both duel stages.

    No escrow has run at either stage (ADR 0009 + wait-to-resolve), so
    the timeout is purely cosmetic. Order of preference:

    1. Edit the original challenge card to the localized expiry notice
       (visible expiry edit, keyboard dropped atomically) — L-16.
    2. If the edit fails (card deleted, bot kicked, already edited
       beyond Telegram's window) fall back to the M-G-3 posture: drop
       the dead keyboard, then post the notice as a fresh message.

    The sweeper clears the FSM AFTER this callback returns, holding the
    per-match lock throughout (:func:`expiry_guard`); the registry slot
    frees itself when that guard exits and nobody else is queued.
    """
    chat_id_raw = data.get("chat_id")
    if not isinstance(chat_id_raw, int):
        log.bind(key=str(key), data=data, stage=stage).warning(
            "duel timeout: stage missing chat_id; abandoning"
        )
        return
    lang = _lang_from_fsm(data)
    text = t(text_key, lang)
    challenge_msg_id = data.get("challenge_message_id")
    edited = False
    if isinstance(challenge_msg_id, int):
        edited = await _edit_to_expired(bot, chat_id_raw, challenge_msg_id, text)
    if not edited:
        if isinstance(challenge_msg_id, int):
            await _drop_keyboard(bot, chat_id_raw, challenge_msg_id)
        with contextlib.suppress(TelegramForbiddenError, TelegramBadRequest):
            await bot.send_message(chat_id_raw, text)
    log.bind(challenger_id=key.user_id, chat_id=chat_id_raw, stage=stage, edited=edited).info(
        "/duel timeout expired by sweeper"
    )


async def cancel_match_from_global(
    bot: Bot,
    *,
    state: FSMContext,
    state_name: str,
    data: dict[str, object],
    caller_id: int,
    lang: str,
) -> None:
    """Terminal teardown of a live duel on behalf of the global ``/cancel``.

    ``/cancel`` (:mod:`telegram_invite_bot.handlers.cancel`) is a generic
    escape hatch, and it used to tear a duel down the way it tears down a
    withdraw form: read the FSM data, drop a keyboard, ``state.clear()``,
    done. A duel is not a withdraw form. Two things were wrong with that,
    and this module had already solved both for its own exits (#259).

    1. **No lock.** Every other terminal path — accept, decline, roll, and
       the sweeper via :func:`expiry_guard` — serialises on
       :data:`_match_locks`. ``/cancel`` did not, so the challenger could
       clear the FSM in the window between a decisive :guilabel:`🎲 Roll`
       reading its snapshot and :meth:`DuelService.play` committing the
       escrow and payout. That is the #126 interleave the guard exists to
       close, except aimed by hand instead of arriving on a deadline. The
       clear now happens inside the same lock.

    2. **The other seat learned nothing.** In ``awaiting_rolls`` the
       opponent is looking at a live card with a Roll button on it.
       Dropping the buttons in silence leaves them waiting on a match that
       no longer exists. The timeout path edits the card to a notice
       (:func:`_expire_duel`); so does this, with the cancel wording.

    Nothing is rolled back because nothing is escrowed: no wallet write
    happens between ``/duel`` and the second roll click (ADR 0009, and the
    "Escrow timing" pin in this module's docstring). Whether a challenger
    *should* be able to walk away from a round they are losing is a policy
    question this fix deliberately does not answer — the 300 s roll
    timeout already lets them, and closing that is an owner decision.

    ``awaiting_acceptance`` keeps the old bare keyboard-drop: at that
    stage the opponent has committed to nothing, and a card losing its
    buttons says all there is to say.
    """
    chat_id_raw = data.get("chat_id")
    if not isinstance(chat_id_raw, int):
        # No group id stamped, so there is no card to edit and no lock
        # key that would mean anything. Clear and let /cancel confirm.
        log.bind(caller_id=caller_id, prior_state=state_name, data=data).warning(
            "/cancel on a duel FSM with no chat_id; clearing unguarded"
        )
        await state.clear()
        return
    chat_id = chat_id_raw
    message_id_raw = data.get("challenge_message_id")
    message_id = message_id_raw if isinstance(message_id_raw, int) else None

    async with _match_lock_cm(bot_id=bot.id, chat_id=chat_id, challenger_id=caller_id):
        if state_name == DuelStates.awaiting_rolls.state:
            text = t("h_duel_cancelled", lang)
            edited = False
            if message_id is not None:
                edited = await _edit_to_expired(bot, chat_id, message_id, text)
            if not edited:
                await _drop_keyboard(bot, chat_id, message_id)
                with contextlib.suppress(TelegramForbiddenError, TelegramBadRequest):
                    await bot.send_message(chat_id, text)
        else:
            await _drop_keyboard(bot, chat_id, message_id)
        await state.clear()

    log.bind(challenger_id=caller_id, chat_id=chat_id, prior_state=state_name).info(
        "/cancel tore down a duel under the match lock"
    )


async def on_expire_duel_acceptance(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """Timeout callback for ``DuelStates.awaiting_acceptance``.

    The opponent never clicked Accept/Decline — the card is edited in
    place to the localized "challenge expired" notice (L-16 visible
    expiry edit); see :func:`_expire_duel` for the fallback ladder.
    """
    await _expire_duel(bot, key, data, stage="accept", text_key="h_duel_timeout_accept")


async def on_expire_duel_rolls(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """Timeout callback for ``DuelStates.awaiting_rolls``.

    Same posture — no escrow has landed, so the timeout is a pure
    notice. Future-proofing: if the legacy "auto-loss for the seat
    that didn't roll" semantics is desired, this callback would be
    the place to coerce a 0-roll and call DuelService.play; punted
    for now to keep the timeout policy simple and safe.
    """
    await _expire_duel(bot, key, data, stage="rolls", text_key="h_duel_timeout_rolls")


def build_router(registry: EngineRegistry) -> Router:
    """Factory — fresh router + middleware per call.

    Group-only filter at the router level (mirrors legacy
    bot.py:21228-21230 rejecting non-group invocations), plus a
    private-chat twin that says so out loud (#122) — the filter alone
    made a DM ``/duel`` match nothing at all. EconomyMiddleware on both
    observers so the handler sees ``duel_service`` injected on the
    callback path where the actual escrow + payout runs.
    """
    router = Router(name="duel")

    router.message.middleware(EconomyMiddleware(registry))
    router.callback_query.middleware(EconomyMiddleware(registry))

    router.message.register(
        handle_duel,
        Command("duel", "дуэль", ignore_case=True),
        F.from_user,
        F.chat.type.in_(GROUP_TYPE_NAMES),
    )
    # #122: the private half of the same command words. Without it a
    # ``/duel`` in a DM matched nothing and the user got silence.
    router.message.register(
        handle_group_only,
        Command("duel", "дуэль", ignore_case=True),
        F.from_user,
        F.chat.type == ChatType.PRIVATE,
    )
    router.callback_query.register(handle_duel_accept, DuelAccept.filter(), F.from_user)
    router.callback_query.register(handle_duel_decline, DuelDecline.filter(), F.from_user)
    router.callback_query.register(handle_duel_roll, DuelRoll.filter(), F.from_user)
    return router
