"""``/cpc`` (rock-paper-scissors) handler — Stage 34.

Lands the FIRST FSM-driven flow in the strangler pipeline, over the
Stage 33 :class:`RpsService` (atomic escrow + payout) and the Stage
32 pure resolver. The handler is intentionally narrow: private chat
only, two argument forms (``/cpc @username <bet>`` and
``/cpc <user_id> <bet>``) — group form with reply + accept-timer
stays on legacy until Stage 35.

Architecture — FSM Variant A (one session per match, owned by the
challenger). Pinned in ``fsm/rps.py``'s module docstring. The
callback factories in ``keyboards/builders/rps.py`` carry
``challenger_id`` in every payload precisely so this handler can
locate the right :class:`FSMContext` whether the click came from
challenger or opponent.

Flow
----
1. ``/cpc`` (message handler, :func:`handle_cpc`):

   * Parse args; reject unparseable / missing.
   * Resolve opponent (@username → :class:`UsersRepo.get_by_username`,
     numeric → trust the id).
   * Python-level pre-checks: same_player, opponent wallet exists,
     bet is positive/in-range. These mirror :class:`RpsService.play`'s
     own validators so the user sees an immediate rejection rather
     than waiting for the move stage. The service's own validators
     re-run at resolution as the floor — defence in depth.
   * Already-in-game guard: if the challenger's FSM state is set,
     reject. Pins the "no two parallel /cpc from the same player"
     contract that legacy enforces at
     ``rock_paper_scissors.py:99``.
   * Set FSM state ``awaiting_acceptance`` on the challenger's
     session with ``{opponent_id, bet}``.
   * ``bot.send_message(opponent_id, ...)`` with an Accept/Decline
     inline keyboard. If the opponent cannot be reached — blocked the
     bot (``TelegramForbiddenError``) or never opened a PM with it at
     all (``TelegramBadRequest: chat not found``) — clear FSM, tell
     challenger the opponent is unreachable.

2. Accept callback (:func:`handle_rps_accept`):

   * Read the challenger's FSM via :class:`FSMContext` constructed
     from ``callback_data.challenger_id``. Authorize the clicker is
     the expected opponent.
   * Flip state to ``awaiting_moves``; send the move keyboard to
     BOTH players.

3. Decline callback (:func:`handle_rps_decline`):

   * Same authorization. Clear FSM, notify both players.

4. Move callback (:func:`handle_rps_move`):

   * Authorize the clicker is one of the two seats. Stamp their move
     into FSM data.
   * If both moves are now set → call
     :meth:`RpsService.play` (atomic escrow + payout + ledger). Render
     per-seat result cards. Clear FSM.
   * Else → toast "move recorded, waiting".

Escrow timing — money only moves at MOVE stage
---------------------------------------------
This is load-bearing for the no-leak guarantee. Legacy's
``CPCManager.create`` at ``rock_paper_scissors.py:105-108`` reads
balances at challenge-issue time but DOES NOT escrow; escrow happens
when both pick (via the resolve-and-finish path's ``remove_coins``).
The new pipeline does the same: no wallet write happens between
``/cpc`` and the second move click. The handler runs balance
pre-checks before sending the challenge, but those are read-only —
a user can drain their wallet between Accept and Move, in which case
:class:`RpsService.play` returns ``*_INSUFFICIENT_FUNDS`` and the
handler renders that as a terminal outcome (FSM cleared, no leak).
This is a deliberate design — escrow at Accept would create a leak
window (challenger's stake debited but the opponent might never
move, leaving stake parked indefinitely until a future timeout
sweeper Stage 35 ships). Today the only leak vector closed is the
one legacy already closes; the no-timeout posture is documented
below.

Deferred to Stage 35
--------------------
* **Accept timeout** — if opponent never clicks Accept/Decline, the
  FSM hangs in ``awaiting_acceptance`` until the challenger types
  /cancel. No coins are at stake (escrow happens at move-time), so
  the only damage is "stuck busy" for the challenger.
* **Move timeout** — same posture: if one side moves but the other
  never does, the FSM hangs in ``awaiting_moves``. No escrow has
  landed (it only lands at the resolution step, *after* the second
  move click). Stage 35 should land a scheduler-driven sweeper that
  clears stale FSMs.
* **Group form** — ``/cpc`` in a supergroup (with reply or @-mention),
  legacy's primary surface. Stays on legacy until Stage 35 lands the
  group-aware variant + a per-chat accept timer + the in-chat
  challenge card edit (``rock_paper_scissors.py:391``).
* **Custom /cpc_cancel for the opponent.** Today only the challenger
  can /cancel; the opponent's escape hatch is the "Decline" button.
  Variant A's single-FSM design makes this asymmetric on purpose —
  see ``fsm/rps.py``'s rationale.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType  # runtime — isinstance guard
from loguru import logger

from telegram_invite_bot.fsm.rps import RpsStates
from telegram_invite_bot.games.limits import PLAY_LOCKS
from telegram_invite_bot.games.rps import RpsConfig, RpsMove
from telegram_invite_bot.handlers.game_cards import render_abuse_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import (
    RpsAccept,
    RpsDecline,
    RpsMoveCallback,
)
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.scheduler.fsm_busy import is_user_an_opponent, is_user_busy
from telegram_invite_bot.scheduler.fsm_sweeper import (
    STATE_ENTERED_AT_FIELD,
    utc_now_iso,
)
from telegram_invite_bot.services.rps_service import RpsServiceOutcome
from telegram_invite_bot.utils.aiogram import command_args, edit_card, require_from_user
from telegram_invite_bot.utils.keyed_locks import KeyedLocks
from telegram_invite_bot.utils.numbers import parse_int_token

log = logger.bind(component="handlers.rps")

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractAsyncContextManager
    from typing import TypeAlias

    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo
    from telegram_invite_bot.services.game_limit_service import GameLimitService
    from telegram_invite_bot.services.rps_service import RpsService

    # L-02: shared UI-effect signatures for the accept/decline cores.
    # The cores (:func:`accept_rps_challenge` / :func:`decline_rps_challenge`
    # below, plus their duel twins) carry the FSM/business logic once;
    # the surface-specific halves — callback toast vs. group-message
    # reply, edit-this-card vs. edit-by-stored-id — ride in as closures
    # so the inline-button path and the /accept // /decline command
    # path (handlers/challenge_commands.py) share byte-identical state
    # transitions.
    #
    # ``RejectFn`` receives an ALREADY-LOCALIZED text (the core owns
    # key choice + lang); ``AckFn`` is the success acknowledgement
    # (callback: ``callback.answer()``; command: a confirmation reply);
    # ``EditCardFn`` replaces the opponent's challenge card and returns
    # the message_id the new card landed on (``None`` when no card
    # could be rendered).
    RejectFn: TypeAlias = Callable[[str], Awaitable[None]]
    AckFn: TypeAlias = Callable[[], Awaitable[None]]
    EditCardFn: TypeAlias = Callable[[str, InlineKeyboardMarkup | None], Awaitable[int | None]]


# Move display tokens — shared between the move keyboard and the
# result-card rendering. Kept as a dict keyed on :class:`RpsMove` so a
# missing branch fails fast at lookup time rather than rendering an
# empty string.
def _move_label_key(move: RpsMove) -> str:
    """Map an :class:`RpsMove` enum value to its i18n key.

    Three-arm exhaustive — a new enum member added without updating
    this map would crash with KeyError at the move-keyboard build,
    surfacing the gap loudly.
    """
    return {
        RpsMove.ROCK: "h_rps_move_rock",
        RpsMove.PAPER: "h_rps_move_paper",
        RpsMove.SCISSORS: "h_rps_move_scissors",
    }[move]


# L-40: married spouses playing RPS against each other in a GROUP chat
# earn marriage XP on top of the normal coin settlement. The XP
# machinery mirrors legacy ``marriage_add_xp`` (``bot.py:21645``;
# 100 XP per marriage level, ``MARRIAGE_XP_PER_LEVEL`` at
# ``bot.py:21618``). Legacy's CPC module itself settles coins only
# (``rock_paper_scissors.py:541``) and never wired the spouse bonus
# in — L-40 restores the designed behaviour. No RPS-specific legacy
# amount exists, so the grant is anchored just below the smallest
# legacy marriage-activity grant (``bot.py:21627`` — dinner: 15 XP for
# 100 coins): a free game gives a flat 10 XP per completed match
# (win, loss or tie alike — playing together IS the activity).
MARRIED_RPS_XP = 10


async def _grant_spouse_xp_if_married(
    *,
    bonds: BondsWriteRepo,
    chat_id: int,
    challenger_id: int,
    opponent_id: int,
) -> int | None:
    """Grant L-40 marriage XP if the two seats are married to each other.

    Group chats only — marriage is per-chat (``marriages.chat_id``), so
    a private-chat match (``chat_id`` == the challenger's positive user
    id) never qualifies; group/supergroup ids are negative.

    The write goes to users.db — a DIFFERENT session than the economy
    settlement, committed independently by :class:`SessionMiddleware` —
    so this follows the same compensating / non-atomic posture as
    ``handlers/couple_activities.py``: the XP write runs strictly AFTER
    the money settled inside :meth:`RpsService.play`, and any failure
    here is logged and swallowed so a users.db hiccup can never break
    or roll back the game itself.

    Returns the XP granted, or ``None`` when no grant happened (not a
    group chat, not married, married to someone else, or write failed).
    """
    if chat_id >= 0:
        return None
    try:
        marriage = await bonds.get_marriage(chat_id, challenger_id)
        if marriage is None:
            return None
        partner_id = marriage.user2_id if marriage.user1_id == challenger_id else marriage.user1_id
        if partner_id != opponent_id:
            return None
        new_exp = await bonds.add_marriage_xp(chat_id, challenger_id, MARRIED_RPS_XP)
        if new_exp is None:
            # Pair row vanished between the read and the UPDATE
            # (divorce race) — nothing granted, nothing to undo.
            return None
    except Exception:  # noqa: BLE001 — failure must not break the game
        log.bind(chat_id=chat_id, challenger_id=challenger_id, opponent_id=opponent_id).exception(
            "/cpc: spouse-XP grant failed; game result stands"
        )
        return None
    log.bind(
        chat_id=chat_id,
        challenger_id=challenger_id,
        opponent_id=opponent_id,
        xp=MARRIED_RPS_XP,
        new_exp=new_exp,
    ).info("/cpc spouse XP granted")
    return MARRIED_RPS_XP


ParsedRecipient = tuple[Literal["id"], int, int] | tuple[Literal["username"], str, int]
"""Two-arm tagged union, identical shape to ``handlers/send.py``'s
:data:`ParsedRecipient`. Kept independent (not imported) because a
future divergence (e.g. /cpc growing a comment field, or /send growing
a reply form) shouldn't ripple through both modules."""


def _parse_args(raw: str) -> ParsedRecipient | None:
    """Parse ``/cpc <recipient> <bet>``. Returns ``None`` on parse
    error. Mirrors ``handlers/send.py``'s parser shape exactly —
    inlined rather than shared because the two commands' arg
    grammars are independent (a future /cpc reply-form would
    diverge).

    Rejects: missing fields, non-numeric user_id (no ``@`` prefix),
    non-numeric bet, zero/negative bet, non-positive user_id, bare
    ``@`` token, empty username after strip.

    #1666: both numbers go through :func:`parse_int_token` rather than
    a bare ``int()``. ``int()`` is wider than this package's parse
    policy — it also takes Unicode decimal digits (``/cpc @user ١٠٠``
    read as 100), underscore separators (``1_0_0``) and a leading
    sign. Every other money command here already refuses those; this
    one was the last holdout.

    #1667: a non-positive recipient id is refused. Telegram spells
    group and channel ids negative, and the only recipient gate below
    is ``opponent_id == tg_user.id`` — so ``/cpc -100... 100`` used to
    make the bot post a live challenge card into an arbitrary chat it
    is a member of. No money could move (:func:`accept_rps_challenge`
    compares the clicker's user id against ``opponent_id``, which can
    never be a negative chat id, so every tap answered
    ``h_rps_not_your_match``), but the message injection was real and
    the dead card looked alive. Unsigned ``parse_int_token`` already
    rejects the leading ``-``; the explicit bound below also closes
    ``0`` and keeps the intent readable.
    """
    parts = raw.split(maxsplit=2)
    if len(parts) < 2:
        return None
    recipient_raw, bet_raw = parts[0], parts[1]
    bet = parse_int_token(bet_raw)
    if bet is None or bet <= 0:
        return None
    if recipient_raw.startswith("@"):
        username = recipient_raw[1:]
        if not username:
            return None
        return ("username", username, bet)
    to_id = parse_int_token(recipient_raw)
    if to_id is None or to_id <= 0:
        return None
    return ("id", to_id, bet)


def _build_challenge_keyboard(
    *, challenger_id: int, chat_id: int, bet: int, lang: str
) -> InlineKeyboardMarkup:
    """Accept / Decline pair on the challenge card sent to the opponent.

    Both buttons live on one row (legacy posture at
    ``rock_paper_scissors.py:377-380``). The Accept payload re-encodes
    ``bet`` so a stale card still surfaces what was being agreed to in
    the log line — authorization itself is on the FSM, not the wire.

    R-FIX-008: ``chat_id`` is the chat that owns the FSM (where the
    challenger typed /cpc). Stamped into every callback payload so the
    callback handlers can rebuild the storage key.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_rps_accept_btn", lang),
                    callback_data=RpsAccept(
                        challenger_id=challenger_id, bet=bet, chat_id=chat_id
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=t("h_rps_decline_btn", lang),
                    callback_data=RpsDecline(challenger_id=challenger_id, chat_id=chat_id).pack(),
                ),
            ]
        ]
    )


def _build_move_keyboard(*, challenger_id: int, chat_id: int, lang: str) -> InlineKeyboardMarkup:
    """The three move buttons (rock/paper/scissors) on a single row.

    Same payload prefix for both seats — the handler resolves which
    seat clicked via ``callback.from_user.id`` against the FSM data
    (``opponent_id`` field). ``challenger_id`` in the payload is the
    FSM session key; ``move`` is the chosen token."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(_move_label_key(move), lang),
                    callback_data=RpsMoveCallback(
                        challenger_id=challenger_id, move=move.value, chat_id=chat_id
                    ).pack(),
                )
                for move in (RpsMove.ROCK, RpsMove.PAPER, RpsMove.SCISSORS)
            ]
        ]
    )


# R-FIX-009: process-local lock registry, keyed on the FSM
# storage key tuple ``(bot_id, chat_id, user_id)``. The TOCTOU was:
# two concurrent move-button clicks both read FSM state, both saw
# "other move is still None", both stamped their own move, both
# called ``rps_service.play`` → double escrow + double payout. The
# fix serialises the read→decide→play→clear critical section per
# match. Single-worker invariant (workers=1) makes an in-process
# asyncio.Lock sufficient — no cross-process coordination needed.
# We also use it on the accept path so an Accept-then-Decline
# rapid-click can't both flip state.
#
# #126-fp: the registry is a :class:`KeyedLocks`, which reference-counts
# its slots — a slot appears on the first waiter and disappears when the
# last one leaves. That deletes the entire manual bookkeeping this module
# used to carry (a release helper called on every terminal branch, a
# ``drop_on_exit`` flag, a "reclaim the orphan a stale tap just made"
# helper, and an ``after_clear`` hook on the sweeper rule) — and with it
# the two bugs those pieces had to be balanced against each other to
# avoid. Neither is expressible now: a slot is never popped while
# somebody still holds or waits on it, so no click can ever build a
# second Lock beside a live one; and a slot is never left behind by a tap
# on a long-dead card, so the table cannot grow without bound (the #73
# growth class).
_match_locks: KeyedLocks[tuple[int, int, int]] = KeyedLocks()


def _match_lock_cm(
    *, bot_id: int, chat_id: int, user_id: int
) -> AbstractAsyncContextManager[asyncio.Lock]:
    """Hold the per-match lock for the duration of the block.

    Keyed on the same triple as the FSM ``StorageKey`` so callers in
    different handlers serialise against each other for the same match.
    The lock is released on exit even through an exception.
    """
    return _match_locks.acquire((bot_id, chat_id, user_id))


def expiry_guard(bot: Bot, key: StorageKey) -> AbstractAsyncContextManager[asyncio.Lock]:
    """The sweeper's hold on a match while it expires it (#126).

    Handed to :class:`~telegram_invite_bot.scheduler.TimeoutRule` as
    its ``guard``, this puts the timeout path on the SAME lock the
    accept / decline / move handlers take. Before it, a deadline and a
    click could both act on the same match: the sweeper read
    ``awaiting_acceptance``, the accept flipped it to
    ``awaiting_moves``, and the sweeper's unconditional post-callback
    clear then deleted a match that had just started — both seats left
    with move keyboards that answer "match not found" forever.

    The ``StorageKey`` triple is exactly the lock key (Variant A: the
    FSM lives under the challenger's id), so no translation is needed.
    """
    return _match_lock_cm(bot_id=bot.id, chat_id=key.chat_id, user_id=key.user_id)


def _fsm_context_for(*, bot: Bot, storage: BaseStorage, chat_id: int, user_id: int) -> FSMContext:
    """Build an FSMContext targeting ``(bot, chat_id, user_id)``.

    R-FIX-008: previously this helper hardcoded
    ``chat_id=user_id`` (private-chat shape). After Stage 31 widened
    /cpc to group chats that collapsed every /cpc by the same user
    across all groups onto a single FSM key — two parallel /cpc by
    the same challenger in groups G1 and G2 would stomp each other's
    state. The fix threads the actual chat id (where the challenger
    typed /cpc) through every call site. The message-handler side
    receives ``state`` auto-keyed by aiogram on
    ``(message.chat.id, message.from_user.id)``; the callback side
    recovers the chat id from the :class:`RpsAccept` / :class:`RpsDecline`
    / :class:`RpsMoveCallback` payloads (which now carry it).
    """
    return FSMContext(
        storage=storage,
        key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id),
    )


async def handle_cpc(
    message: Message,
    command: CommandObject,
    bot: Bot,
    state: FSMContext,
    users_repo: UsersRepo,
    economy_repo: EconomyRepo,
    rps_service: RpsService,
    game_limit_service: GameLimitService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/cpc`` entry point — issue the challenge."""
    del rps_service  # service injected for symmetry; called at move-time
    tg_user = require_from_user(message)

    raw = command_args(command)
    parsed = _parse_args(raw)
    if parsed is None:
        # Distinguish "no args" from "bad args" via a quick re-split —
        # same posture as ``handlers/send.py`` (one usage card per
        # missing-args, distinct invalid_args card per malformed
        # input). Both surfaces are parse-time; service never sees
        # invalid input from this branch.
        if not raw or len(raw.split()) < 2:
            await message.answer(t("h_rps_usage", lang))
        else:
            await message.answer(t("h_rps_invalid_args", lang))
        return

    # Resolve opponent. Username form goes through UsersRepo; numeric
    # form trusts the id (the service still rejects unknown wallets
    # via NO_OPPONENT_WALLET at resolution).
    if parsed[0] == "username":
        _, username, bet = parsed
        opponent_entity = await users_repo.get_by_username(username)
        if opponent_entity is None:
            await message.answer(t("h_rps_opponent_not_found", lang))
            log.bind(uid=tg_user.id, username=username, outcome="opponent_unknown").info(
                "/cpc rejected"
            )
            return
        opponent_id = opponent_entity.user_id
    else:
        _, opponent_id, bet = parsed

    # Cheap validators in the handler — same posture as
    # ``handlers/send.py``: surface a typed outcome at the parser-edge
    # before opening any inline-keyboard render path. The service
    # re-checks at resolution time as the floor.
    if opponent_id == tg_user.id:
        await message.answer(t("h_rps_same_player", lang))
        return
    cfg = RpsConfig()
    if bet < cfg.min_bet or bet > cfg.max_bet:
        await message.answer(t("h_rps_invalid_bet", lang, min_bet=cfg.min_bet, max_bet=cfg.max_bet))
        return

    # #1754: balance pre-check (read-only — no escrow at challenge
    # time, ADR 0009; the service re-validates at resolve as the
    # floor). This is the same pair of reads ``handle_duel`` has always
    # done, and the docstring above this function claimed /cpc did too.
    # It did not: ``RpsService.play`` was the ONLY place a wallet was
    # ever consulted, and that runs at MOVE time — after the card was
    # sent, after the opponent accepted, and after the accept stamped a
    # ``game_plays`` row against the opponent's shared 25-a-day budget.
    # A zero-balance account could therefore lock a victim out of every
    # game in the ecosystem for a day, one dead match at a time, at no
    # cost to its own wallet.
    challenger_wallet = await economy_repo.get(tg_user.id)
    if challenger_wallet is None or challenger_wallet.balance < bet:
        await message.answer(t("h_rps_insufficient_funds_challenger", lang, bet=bet))
        log.bind(uid=tg_user.id, bet=bet, outcome="challenger_insufficient").info("/cpc rejected")
        return
    opponent_wallet = await economy_repo.get(opponent_id)
    if opponent_wallet is None or opponent_wallet.balance < bet:
        await message.answer(t("h_rps_insufficient_funds_opponent", lang, bet=bet))
        log.bind(uid=tg_user.id, bet=bet, outcome="opponent_insufficient").info("/cpc rejected")
        return

    # Already-in-game guard, first half. Mirrors
    # ``rock_paper_scissors.py:99``. The challenger's own FSM state
    # is a "busy" flag — a non-None state means one of
    # awaiting_acceptance / awaiting_moves, both of which represent
    # an in-flight match.
    #
    # Checking the TARGET opponent is still deliberately skipped:
    # the worst case there is the opponent gets a SECOND challenge
    # card while their first is still pending — they can decline one
    # and accept the other, no coin damage.
    prior = await state.get_state()
    if prior is not None:
        await message.answer(t("h_rps_already_in_game", lang))
        log.bind(uid=tg_user.id, prior_state=prior).info("/cpc rejected — busy")
        return

    # Second half (#1517). The check above only sees matches the
    # CALLER STARTED, because a match lives on the challenger's key
    # alone (``fsm/rps.py``, "Variant A"). Someone who ACCEPTED
    # another player's challenge is mid-match with an empty key of
    # their own, walks straight through ``prior is not None`` and
    # ends up in two matches at once. Scan for the opponent seat.
    #
    # The OPPONENT seat only, not both: R-FIX-008 pins that the same
    # user may hold one independent /cpc session per chat, so being a
    # challenger elsewhere is not busy here — and the current chat's
    # own key was just read above. ``is_user_busy`` (the two-seat
    # predicate) belongs to the accept path, where entering a match
    # somebody else can resolve is the step being guarded.
    #
    # No ``exclude_key``: the caller's own key was just proven empty,
    # so there is nothing of theirs for the scan to trip over.
    #
    # Cost is one storage scan per /cpc — a command path, not a loop
    # — and the scan skips the empty records aiogram's middleware
    # leaves behind (#1516).
    if await is_user_an_opponent(state.storage, user_id=tg_user.id):
        await message.answer(t("h_rps_already_in_game", lang))
        log.bind(uid=tg_user.id).info("/cpc rejected — opponent of another match")
        return

    # Push state + match data BEFORE the outbound send_message: if the
    # send fails because the opponent is unreachable, we'll clear the
    # state and apologize. The opposite
    # ordering (send first, set state on success) opens a race window
    # where the opponent clicks Accept before the state lands.
    # #1664: the ecosystem-wide anti-abuse caps, which this command had
    # never run. ``GameLimitsRepo`` counts plays on ``user_id`` alone,
    # with no ``game ==`` clause, so /roll, /flip, /roulette, /pvp_coin
    # and /pvp_dice were already drawing on one shared budget while
    # /cpc and /duel — the two with the largest single-match exposure —
    # drew on nothing. A player who spent all 25 daily slots on
    # /roulette simply moved here and kept going.
    #
    # What is stamped is the CHALLENGE, not the settled match. A /cpc
    # round settles on somebody else's update, so there is no single
    # handler in which check and record could bracket the play the way
    # /roulette's do. The challenge is the act the caller chose, it is
    # what puts a live card in front of another player, and it is
    # therefore the thing worth rate-limiting. The other seat is
    # stamped where it makes its own choice — see the accept path.
    #
    # Placed after every parse, balance and busy rejection above, so a
    # typo never starts a cooldown and a caller already in a match is
    # refused for that reason rather than spending a slot on it.
    now = datetime.now()  # naive local — matches what game_plays stores
    async with PLAY_LOCKS.acquire(tg_user.id):
        abuse = await game_limit_service.check(tg_user.id, now=now, include_cooldown=False)
        if not abuse.allowed:
            await message.answer(render_abuse_refusal(lang, abuse))
            log.bind(uid=tg_user.id, reason=abuse.reason).info("/cpc blocked")
            return

        challenger_chat_id = message.chat.id
        await state.set_state(RpsStates.awaiting_acceptance)
        # ``state_entered_at`` is the deadline reference for Stage 35's
        # :class:`FsmTimeoutSweeper`. Stamped on EVERY set_state call in
        # this module — a missing stamp means the sweeper logs a warning
        # and leaves the session alone (see fsm_sweeper.sweep_once). ISO
        # string, tz-aware UTC; the sweeper round-trips via fromisoformat.
        # R-FIX-008: ``challenger_chat_id`` is stashed so the sweeper's
        # synthesised-from-key callback path can recover the chat where
        # the match lives — the StorageKey already carries it, but
        # spelling it out in the data dict keeps the sweeper callbacks
        # readable without poking into the key tuple.
        await state.set_data(
            {
                "opponent_id": opponent_id,
                "bet": bet,
                "challenger_chat_id": challenger_chat_id,
                # M-G-6: stash the challenger's resolved lang so the accept
                # handler renders the challenger's move card in the
                # challenger's locale, not the opponent's. The accept path
                # only knows the opponent's ``language_code`` (Telegram
                # update carries the clicker's user, not both seats).
                "challenger_lang": lang,
                STATE_ENTERED_AT_FIELD: utc_now_iso(),
            }
        )

        keyboard = _build_challenge_keyboard(
            challenger_id=tg_user.id,
            chat_id=challenger_chat_id,
            bet=bet,
            lang=lang,
        )
        try:
            opponent_msg = await bot.send_message(
                opponent_id,
                t("h_rps_challenge_received", lang, challenger_id=tg_user.id, bet=bet),
                reply_markup=keyboard,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            # #305: two errors, one outcome. ``TelegramForbiddenError`` is
            # "bot was blocked by the user" — it presupposes the PM once
            # existed. A user who never wrote to the bot at all has no PM to
            # forbid, and Telegram answers ``Bad Request: chat not found``
            # instead. That is the more common case, and it is the one
            # ``h_rps_opponent_blocked_bot`` already describes out loud
            # ("возможно, он не писал боту") — yet only the first was caught.
            #
            # Escaping here was not cosmetic. The FSM is set above, before
            # the send, precisely so this branch can clear it; an escaping
            # exception skipped ``state.clear()`` and left the challenger
            # wedged in ``awaiting_acceptance`` — every later /cpc answered
            # ``h_rps_already_in_game``, and the sweeper's ``on_expire``
            # raised again on the same unreachable id every 30 s, because
            # ``fsm_sweeper.sweep_once`` deliberately keeps state on error.
            # Legacy swallows every exception on these sends
            # (``rock_paper_scissors.py:489-492``); ``duel.py`` catches both.
            #
            # ``exc`` is logged rather than discarded: BadRequest also covers
            # genuine bugs (bad entities, oversized text), and those must
            # stay visible even though the user-facing answer is the same.
            await state.clear()
            await message.answer(t("h_rps_opponent_blocked_bot", lang))
            log.bind(uid=tg_user.id, opponent_id=opponent_id, error=str(exc)).info(
                "/cpc rejected — opponent unreachable"
            )
            return

        # Stash the opponent's challenge card message_id so /cpc_cancel
        # (Stage 35) AND the timeout sweeper can edit the keyboard out
        # when the match aborts. Without this, an opponent who never
        # clicked could still see live Accept/Decline buttons after the
        # match is dead.
        await state.update_data(opponent_accept_message_id=opponent_msg.message_id)

        # The challenge is live: the opponent has the card and the FSM
        # points at it. Stamp it only HERE — the unreachable-opponent
        # branch above clears the FSM and returns, and a challenge that
        # never reached anyone must burn no slot. Then end the write
        # transaction inside the lock: ``record`` is a bare
        # ``session.add`` (#222-B), so until something commits it the
        # row is invisible to every other connection, and the caller's
        # next update takes this lock the moment it releases and reads a
        # ``game_plays`` table that still does not know about this
        # challenge.
        await game_limit_service.record(tg_user.id, game="cpc", now=now)
        if checkpoint is not None:
            await checkpoint()

    await message.answer(t("h_rps_challenge_sent", lang, opponent_id=opponent_id, bet=bet))
    log.bind(uid=tg_user.id, opponent_id=opponent_id, bet=bet).info("/cpc challenge sent")


async def accept_rps_challenge(
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
    edit_opponent_card: EditCardFn,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Shared accept core — the FSM/business half of "opponent accepts".

    L-02: extracted verbatim from the callback handler so the /accept
    standalone command (handlers/challenge_commands.py) drives the
    SAME transitions. Reads the CHALLENGER's FSM (Variant A), verifies
    ``acceptor_id`` is the registered opponent, flips state to
    ``awaiting_moves``, then sends the move keyboard to BOTH players
    (opponent: via ``edit_opponent_card``; challenger: fresh
    send_message because their original /cpc reply doesn't have a
    keyboard).

    ``payload_bet`` is the wire-carried bet on the callback path
    (M-G-1 tamper guard); the command path passes ``None`` — there is
    no payload to forge, the FSM bet is the only bet.

    #126: everything from the state read down to the ``awaiting_moves``
    flip runs under the per-match lock. Decline, cancel and the move
    handler have held it since R-FIX-009-fp; accept was the one
    decide-then-mutate path still running bare, so two clicks on the
    same card (or an accept meeting a decline) could each read
    ``awaiting_acceptance`` and each act on it. The lock is released
    before the card I/O below — Telegram calls are slow and must not
    hold a match hostage.
    """
    challenger_state = _fsm_context_for(
        bot=bot,
        storage=fsm_storage,
        chat_id=chat_id,
        user_id=challenger_id,
    )
    # #1664: the acceptor spends a slot of the SAME shared budget the
    # challenger spent when the card went up — accepting a /cpc is
    # playing one. Gating only the challenger would leave the hole open
    # from the other seat: a player who exhausted the day on /roulette
    # cannot start a match, but could still be walked through an
    # unlimited number of them by a friend who keeps typing /cpc.
    #
    # ``include_cooldown=False`` for the same reason the challenge
    # commands pass it: the 180 s clock spaces out self-served plays
    # that settle the instant they are made, and an accept lands on
    # somebody else's schedule, not the acceptor's.
    #
    # PLAY_LOCKS is taken OUTSIDE the per-match lock. Every other holder
    # of PLAY_LOCKS takes no match lock at all, and every holder of a
    # match lock (moves, decline, cancel, the sweeper) takes no play
    # lock, so this is the only place the two meet and the order cannot
    # invert.
    now = datetime.now()  # naive local — matches what game_plays stores
    async with PLAY_LOCKS.acquire(acceptor_id):
        abuse = await game_limit_service.check(acceptor_id, now=now, include_cooldown=False)
        if not abuse.allowed:
            await reject(render_abuse_refusal(lang, abuse))
            log.bind(
                challenger_id=challenger_id,
                clicker=acceptor_id,
                reason=abuse.reason,
            ).info("/cpc accept blocked")
            return

        async with _match_lock_cm(bot_id=bot.id, chat_id=chat_id, user_id=challenger_id):
            state_name = await challenger_state.get_state()
            if state_name != RpsStates.awaiting_acceptance.state:
                # Match cleared (declined, cancelled, race) — silent toast,
                # no edit. Telegram caches click acks for 60s; a second click
                # on a stale card mustn't crash.
                await reject(t("h_rps_match_not_found", lang))
                return
            data = await challenger_state.get_data()
            if data.get("opponent_id") != acceptor_id:
                # Cross-user click — A invited B, C clicked. Silent toast,
                # no state mutation. Matches legacy's posture at
                # ``rock_paper_scissors.py:140-141`` (CPCManager.accept rejects
                # if user_id != opponent_id).
                await reject(t("h_rps_not_your_match", lang))
                return

            bet = int(data["bet"])
            # M-G-1: payload bet must agree with FSM-stored bet. The callback
            # carries ``bet`` as decorative metadata (the authoritative stake
            # lives in FSM data), but a hand-crafted button with a forged bet
            # is a tamper signal: reject silently and log so a future audit can
            # spot the pattern. Without this guard a leaked / replayed button
            # could surface ``bet=N`` in the log line while resolution debited a
            # different N from the FSM — confusing post-mortem.
            if payload_bet is not None and payload_bet != bet:
                log.bind(
                    challenger_id=challenger_id,
                    clicker=acceptor_id,
                    payload_bet=payload_bet,
                    fsm_bet=bet,
                ).warning("/cpc accept rejected: payload bet != FSM bet")
                await reject(t("h_rps_match_not_found", lang))
                return
            # M-G-2: busy guard. Scan storage for any other active match
            # (RPS or duel) the clicker is already part of — if one exists,
            # surface the localized "already in a match" toast and refuse
            # to flip state. The challenger's match stays intact; the older
            # match for the clicker stays intact too.
            #
            # #1517: "part of" means EITHER seat. The original scan matched
            # ``opponent_id`` only, so a clicker who is the CHALLENGER of
            # another live match read as free — and nothing else caught it,
            # because this handler opens the key of the match being
            # accepted, never the clicker's own.
            #
            # Runs under the lock and takes none of its own — it only reads
            # storage — so it cannot deadlock against a sibling match.
            if await is_user_busy(
                fsm_storage,
                user_id=acceptor_id,
                exclude_key=challenger_state.key,
            ):
                log.bind(
                    challenger_id=challenger_id,
                    clicker=acceptor_id,
                ).info("/cpc accept rejected: clicker already in another match")
                await reject(t("h_rps_already_in_game", lang))
                return
            await challenger_state.set_state(RpsStates.awaiting_moves)
            # Preserve opponent_id and bet; reset move slots to None so we can
            # branch on "both filled?" at move-click time. Also re-stamp
            # ``state_entered_at`` — the awaiting_moves deadline is independent
            # of how long the user spent in awaiting_acceptance, and the
            # sweeper reads this field every pass.
            # ``update_data`` takes a positional Mapping and/or kwargs; we
            # pass a mapping so the dynamic ``STATE_ENTERED_AT_FIELD`` key
            # name (whose type is ``str``, not a literal) types cleanly under
            # ``--strict`` instead of getting widened to ``str`` kwargs.
            await challenger_state.update_data(
                {
                    "challenger_move": None,
                    "opponent_move": None,
                    STATE_ENTERED_AT_FIELD: utc_now_iso(),
                }
            )

        # Edit the opponent's challenge card into the move keyboard.
        keyboard_for_opponent = _build_move_keyboard(
            challenger_id=challenger_id,
            chat_id=chat_id,
            lang=lang,
        )
        await ack()
        opponent_move_message_id = await edit_opponent_card(
            t("h_rps_choose_move", lang), keyboard_for_opponent
        )
        # Send the move keyboard to the CHALLENGER too — their original
        # /cpc reply ("waiting") has no keyboard.
        # M-G-6: render the challenger's move card in the challenger's
        # locale (stashed at /cpc time as ``challenger_lang``) rather than
        # in ``lang`` (which is the opponent's locale — the clicker is the
        # opponent here). Falls back to ``lang`` if the field is missing
        # (records seeded before this fix, or future flows).
        challenger_lang_raw = data.get("challenger_lang")
        challenger_lang = challenger_lang_raw if isinstance(challenger_lang_raw, str) else lang
        keyboard_for_challenger = _build_move_keyboard(
            challenger_id=challenger_id,
            chat_id=chat_id,
            lang=challenger_lang,
        )
        challenger_move_message_id: int | None = None
        try:
            challenger_msg = await bot.send_message(
                challenger_id,
                t("h_rps_choose_move", challenger_lang),
                reply_markup=keyboard_for_challenger,
            )
            challenger_move_message_id = challenger_msg.message_id
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            # Pathological — the challenger became unreachable AFTER sending
            # the /cpc (blocked the bot, or deleted the account, which reads
            # as ``chat not found``). Clear the FSM so the opponent's later
            # move click sees ``match_not_found`` and surface nothing
            # further. The bet never moved; no leak. #305: without the
            # BadRequest half this clear was skipped and the challenger
            # stayed pinned in ``awaiting_moves``.
            await challenger_state.clear()
            log.bind(
                challenger_id=challenger_id,
                opponent_id=acceptor_id,
                error=str(exc),
            ).warning("/cpc: challenger unreachable post-accept; match aborted")
            return

        # Stash both move-card message ids so the Stage 35 timeout sweeper
        # (and a future /cpc_cancel during awaiting_moves) can edit the
        # inline keyboards out — a click on a dead match would otherwise
        # re-enter the move handler and surface ``match_not_found``, which
        # is correct but uglier than no buttons at all.
        await challenger_state.update_data(
            opponent_move_message_id=opponent_move_message_id,
            challenger_move_message_id=challenger_move_message_id,
        )

        # Both seats hold a move keyboard, so the match is really being
        # played — the unreachable-challenger branch above aborts it and
        # returns without reaching here, exactly like the undelivered
        # /cpc challenge burns no slot. ``record`` is a bare
        # ``session.add`` (#222-B), so commit it inside the lock: until
        # something commits, the row is invisible to every other
        # connection, and this user's next game takes this lock the
        # moment it releases.
        await game_limit_service.record(acceptor_id, game="cpc", now=now)
        if checkpoint is not None:
            await checkpoint()

    log.bind(
        challenger_id=challenger_id,
        opponent_id=acceptor_id,
        bet=bet,
    ).info("/cpc accepted; move stage")


async def handle_rps_accept(
    callback: CallbackQuery,
    callback_data: RpsAccept,
    bot: Bot,
    fsm_storage: BaseStorage,
    game_limit_service: GameLimitService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Opponent clicked ✅ Accept on the challenge card.

    Thin wrapper over :func:`accept_rps_challenge` — the closures
    carry the callback-specific UI effects (toast acks, edit the
    clicked card in place with a fresh-send fallback) while the core
    owns every FSM transition. L-02's /accept command builds the same
    core with message-reply closures instead.
    """
    assert callback.from_user is not None  # filter guarantee
    acceptor_id = callback.from_user.id

    async def _reject(text: str) -> None:
        await callback.answer(text, show_alert=False)

    async def _ack() -> None:
        await callback.answer()

    async def _edit_opponent_card(text: str, keyboard: InlineKeyboardMarkup | None) -> int | None:
        if not isinstance(callback.message, MessageType):
            return None
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except (TelegramBadRequest, TelegramForbiddenError):
            # Stale card / message-not-modified — fall back to a fresh
            # send so the opponent still sees the move surface. #306:
            # ``TelegramForbiddenError`` belongs on the edit too. For a
            # group ``/cpc`` this card lives in the group, and a bot that
            # was kicked or restricted there cannot edit it; the escape
            # landed between ``set_state(awaiting_moves)`` above and the
            # challenger's own move card below, so neither seat got a
            # keyboard and the match sat in ``awaiting_moves`` until the
            # sweeper expired it a minute later.
            try:
                fresh_opponent = await bot.send_message(acceptor_id, text, reply_markup=keyboard)
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                # #306: the fallback was itself unguarded, which is the
                # sharper half — it DMs the opponent, and the opponent of
                # a group challenge need never have opened a PM with the
                # bot. That is ``chat not found``, and it re-raised the
                # very failure the fallback exists to absorb.
                #
                # ``None`` is a contract value here, not a swallow: the
                # caller stores it as ``opponent_move_message_id`` and
                # ``_drop_keyboard`` already accepts ``None``. The match
                # continues for the challenger, and the sweeper owns the
                # terminal transition — same posture as
                # ``challenge_commands._edit_rps_card``.
                log.bind(acceptor_id=acceptor_id, error=str(exc)).warning(
                    "/cpc: opponent move card undeliverable; match continues without it"
                )
                return None
            return fresh_opponent.message_id
        return callback.message.message_id

    await accept_rps_challenge(
        bot=bot,
        fsm_storage=fsm_storage,
        lang=lang,
        chat_id=callback_data.chat_id,
        challenger_id=callback_data.challenger_id,
        acceptor_id=acceptor_id,
        payload_bet=callback_data.bet,
        game_limit_service=game_limit_service,
        reject=_reject,
        ack=_ack,
        edit_opponent_card=_edit_opponent_card,
        checkpoint=checkpoint,
    )


async def decline_rps_challenge(
    *,
    bot: Bot,
    fsm_storage: BaseStorage,
    lang: str,
    chat_id: int,
    challenger_id: int,
    decliner_id: int,
    reject: RejectFn,
    ack: AckFn,
    edit_opponent_card: EditCardFn,
) -> None:
    """Shared decline core — clear FSM, notify both seats.

    L-02: extracted from the callback handler so the /decline
    standalone command drives the same terminal transition. Runs
    under the match lock (R-FIX-009-fp) so an accept-then-decline
    rapid double-action can't interleave the state-flip in
    :func:`accept_rps_challenge` with the clear here.
    """
    challenger_state = _fsm_context_for(
        bot=bot,
        storage=fsm_storage,
        chat_id=chat_id,
        user_id=challenger_id,
    )
    async with _match_lock_cm(
        bot_id=bot.id,
        chat_id=chat_id,
        user_id=challenger_id,
    ):
        state_name = await challenger_state.get_state()
        if state_name != RpsStates.awaiting_acceptance.state:
            await reject(t("h_rps_match_not_found", lang))
            return
        data = await challenger_state.get_data()
        if data.get("opponent_id") != decliner_id:
            await reject(t("h_rps_not_your_match", lang))
            return

        await challenger_state.clear()
        await ack()
    await edit_opponent_card(t("h_rps_declined_opponent", lang), None)
    # Tell the challenger. Challenger unreachable → silent (FSM already
    # cleared above, no leak). #305: ``chat not found`` counts as
    # unreachable too, and letting it escape would have skipped the log
    # line below and surfaced an error for a notification nobody needs.
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(challenger_id, t("h_rps_declined_challenger", lang))
    log.bind(
        challenger_id=challenger_id,
        opponent_id=decliner_id,
    ).info("/cpc declined")


async def handle_rps_decline(
    callback: CallbackQuery,
    callback_data: RpsDecline,
    bot: Bot,
    fsm_storage: BaseStorage,
    lang: str,
) -> None:
    """Opponent clicked ❌ Decline. Clear FSM, notify both.

    Thin wrapper over :func:`decline_rps_challenge` (see
    :func:`handle_rps_accept` for the closure rationale).
    """
    assert callback.from_user is not None
    decliner_id = callback.from_user.id

    async def _reject(text: str) -> None:
        await callback.answer(text, show_alert=False)

    async def _ack() -> None:
        await callback.answer()

    async def _edit_opponent_card(text: str, keyboard: InlineKeyboardMarkup | None) -> int | None:
        if not isinstance(callback.message, MessageType):
            return None
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except (TelegramBadRequest, TelegramForbiddenError):
            try:
                fresh = await bot.send_message(decliner_id, text)
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                # #306: sibling of the accept closure, and the cost here
                # falls on the *other* seat. ``decline_rps_challenge``
                # calls this before it notifies the challenger, so an
                # escape meant the challenger was never told their
                # challenge had been declined — their FSM was already
                # cleared, leaving a "waiting" message that would never
                # resolve. The decliner's own confirmation is the part
                # worth losing; the challenger's is not.
                log.bind(decliner_id=decliner_id, error=str(exc)).warning(
                    "/cpc: decline confirmation undeliverable to the decliner"
                )
                return None
            return fresh.message_id
        return callback.message.message_id

    await decline_rps_challenge(
        bot=bot,
        fsm_storage=fsm_storage,
        lang=lang,
        chat_id=callback_data.chat_id,
        challenger_id=callback_data.challenger_id,
        decliner_id=decliner_id,
        reject=_reject,
        ack=_ack,
        edit_opponent_card=_edit_opponent_card,
    )


async def _render_result_for_seat(*, bot: Bot, lang: str, user_id: int, body: str) -> None:
    """Send the result card to one of the two seats, swallowing an
    unreachable recipient. The result is terminal; no keyboard.

    #305: this runs after the coins have already moved, and it is called
    once per seat. An escaping error on the first seat would have
    abandoned the second seat's card and the rest of the caller — so
    ``chat not found`` belongs here every bit as much as a block does.
    """
    try:
        await bot.send_message(user_id, body)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        log.bind(uid=user_id, error=str(exc)).warning("/cpc: result delivery failed")


async def handle_rps_move(
    callback: CallbackQuery,
    callback_data: RpsMoveCallback,
    bot: Bot,
    fsm_storage: BaseStorage,
    rps_service: RpsService,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """One of the seats clicked a move button.

    Branches:

    * Wrong state (match already resolved/declined) → silent toast.
    * Cross-user click (clicker is neither challenger nor opponent)
      → silent toast.
    * Only this seat has moved → stamp the move, edit the keyboard
      out (so a re-click on a different move doesn't replace the
      first one — legacy at ``rock_paper_scissors.py:191`` ignores
      re-picks via ``if s.challenger_choice is not None: return``,
      we drop the keyboard which is a stronger guarantee), toast
      "move recorded".
    * Both seats now have moves → call :meth:`RpsService.play`,
      render result cards to both, clear FSM.
    """
    assert callback.from_user is not None

    challenger_state = _fsm_context_for(
        bot=bot,
        storage=fsm_storage,
        chat_id=callback_data.chat_id,
        user_id=callback_data.challenger_id,
    )

    # R-FIX-009: serialise the read→decide→play→clear critical
    # section per match. Without this lock, two concurrent move
    # clicks both saw ``other_move is None``, both stamped, both
    # called ``rps_service.play`` → double escrow + double payout.
    # #126-fp: the registry slot's lifetime is the union of its users'
    # lock holds, so the second-move click finds the same lock object as
    # the first without anybody having to decide when to keep the entry.
    async with _match_lock_cm(
        bot_id=bot.id,
        chat_id=callback_data.chat_id,
        user_id=callback_data.challenger_id,
    ):
        state_name = await challenger_state.get_state()
        if state_name != RpsStates.awaiting_moves.state:
            await callback.answer(t("h_rps_match_not_found", lang), show_alert=False)
            return
        data = await challenger_state.get_data()
        opponent_id = int(data["opponent_id"])
        bet = int(data["bet"])
        clicker = callback.from_user.id
        if clicker not in (callback_data.challenger_id, opponent_id):
            await callback.answer(t("h_rps_not_your_match", lang), show_alert=False)
            return

        # Validate the move token strictly — a hand-crafted
        # ``rps_mv:<cid>:eat_paper`` falls through to ValueError here.
        try:
            chosen_move = RpsMove(callback_data.move)
        except ValueError:
            await callback.answer(t("h_rps_match_not_found", lang), show_alert=False)
            return

        is_challenger = clicker == callback_data.challenger_id
        key = "challenger_move" if is_challenger else "opponent_move"
        if data.get(key) is not None:
            # Re-click by the same seat after they already moved — ignore.
            # Stronger than legacy (which lets a re-pick land if the state
            # is still 'choosing'). We don't want a user changing their
            # mind after a click.
            await callback.answer(t("h_rps_move_recorded", lang), show_alert=False)
            return

        await challenger_state.update_data({key: chosen_move.value})
        other_move = data.get("opponent_move" if is_challenger else "challenger_move")

        # Drop the keyboard on the clicker's card so a re-click can't fire
        # against a stale view of the match.
        if isinstance(callback.message, MessageType):
            # Both seats can be dropping their keyboard at once, so a
            # lost race is expected — but only a lost race. ``edit_card``
            # keeps a malformed card loud where the old blanket
            # ``suppress`` would have hidden it.
            await edit_card(callback.message, t("h_rps_move_recorded", lang), reply_markup=None)

        if other_move is None:
            # Waiting for the second move; nothing more to do.
            await callback.answer()
            log.bind(
                challenger_id=callback_data.challenger_id,
                clicker=clicker,
                move=chosen_move.value,
            ).info("/cpc first move recorded")
            return

        # Both moved — resolve. Reconstruct challenger/opponent moves from
        # what we just stamped vs. what was already there.
        if is_challenger:
            challenger_move = chosen_move
            opponent_move = RpsMove(other_move)
        else:
            opponent_move = chosen_move
            challenger_move = RpsMove(other_move)

        result = await rps_service.play(
            challenger_id=callback_data.challenger_id,
            opponent_id=opponent_id,
            challenger_move=challenger_move,
            opponent_move=opponent_move,
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

        # Clear FSM immediately on terminal outcome — every branch below
        # is terminal. Doing this before the message sends keeps the
        # invariant "FSM is clean once the result is decided" robust
        # against a partial send failure.
        await challenger_state.clear()
        await callback.answer()

    outcome = result.outcome
    # L-40: spouse-XP bonus. Only attempted AFTER the money fully
    # settled inside RpsService.play (any SUCCESS_* outcome — win,
    # loss and tie all count as a played match); rejection outcomes
    # (insufficient funds etc.) never reach this. The grant targets a
    # different DB (users.db) than the settlement (economy.db) — see
    # :func:`_grant_spouse_xp_if_married` for the non-atomic posture.
    spouse_xp: int | None = None
    if outcome in (
        RpsServiceOutcome.SUCCESS_TIE,
        RpsServiceOutcome.SUCCESS_CHALLENGER_WIN,
        RpsServiceOutcome.SUCCESS_OPPONENT_WIN,
    ):
        spouse_xp = await _grant_spouse_xp_if_married(
            bonds=bonds_write_repo,
            chat_id=callback_data.chat_id,
            challenger_id=callback_data.challenger_id,
            opponent_id=opponent_id,
        )
    if outcome is RpsServiceOutcome.SUCCESS_TIE:
        assert result.round_result is not None
        body_challenger = t(
            "h_rps_result_tie",
            lang,
            your_move=t(_move_label_key(challenger_move), lang),
            bet=bet,
        )
        body_opponent = t(
            "h_rps_result_tie",
            lang,
            your_move=t(_move_label_key(opponent_move), lang),
            bet=bet,
        )
    elif outcome is RpsServiceOutcome.SUCCESS_CHALLENGER_WIN:
        assert result.round_result is not None
        payout = result.round_result.payout
        # T-020/R8: name the house cut on the winner's card rather than
        # quietly paying less than the 2× legacy players remember.
        rake = result.round_result.rake
        body_challenger = t(
            "h_rps_result_win",
            lang,
            your_move=t(_move_label_key(challenger_move), lang),
            their_move=t(_move_label_key(opponent_move), lang),
            payout=payout,
            rake=rake,
        )
        body_opponent = t(
            "h_rps_result_loss",
            lang,
            your_move=t(_move_label_key(opponent_move), lang),
            their_move=t(_move_label_key(challenger_move), lang),
            bet=bet,
        )
    elif outcome is RpsServiceOutcome.SUCCESS_OPPONENT_WIN:
        assert result.round_result is not None
        payout = result.round_result.payout
        rake = result.round_result.rake
        body_challenger = t(
            "h_rps_result_loss",
            lang,
            your_move=t(_move_label_key(challenger_move), lang),
            their_move=t(_move_label_key(opponent_move), lang),
            bet=bet,
        )
        body_opponent = t(
            "h_rps_result_win",
            lang,
            your_move=t(_move_label_key(opponent_move), lang),
            their_move=t(_move_label_key(challenger_move), lang),
            payout=payout,
            rake=rake,
        )
    elif outcome is RpsServiceOutcome.CHALLENGER_INSUFFICIENT_FUNDS:
        body_challenger = t("h_rps_resolve_insufficient_challenger", lang)
        body_opponent = body_challenger
    elif outcome is RpsServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS:
        body_challenger = t("h_rps_resolve_insufficient_opponent", lang)
        body_opponent = body_challenger
    else:
        # NO_*_WALLET / SAME_PLAYER / *_BET — all defensive at this
        # stage (the entry handler already filtered same_player /
        # invalid_bet; service validators are the floor). Surface a
        # generic "match not found" rather than leaking the typed
        # outcome string to the user, but log it.
        body_challenger = t("h_rps_match_not_found", lang)
        body_opponent = body_challenger
        log.bind(
            challenger_id=callback_data.challenger_id,
            opponent_id=opponent_id,
            outcome=outcome.value,
        ).error("/cpc: unexpected service outcome at resolve")

    if spouse_xp is not None:
        # L-40: announce the couple-XP gain on BOTH seats' result
        # cards. Appended (not replacing the coin line) — the coin
        # settlement stays exactly as legacy
        # ``rock_paper_scissors.py:541`` rendered it.
        spouse_note = "\n\n" + t("h_rps_spouse_xp", lang, xp=spouse_xp)
        body_challenger += spouse_note
        body_opponent += spouse_note

    await _render_result_for_seat(
        bot=bot,
        lang=lang,
        user_id=callback_data.challenger_id,
        body=body_challenger,
    )
    await _render_result_for_seat(bot=bot, lang=lang, user_id=opponent_id, body=body_opponent)
    log.bind(
        challenger_id=callback_data.challenger_id,
        opponent_id=opponent_id,
        outcome=outcome.value,
        bet=bet,
    ).info("/cpc resolved")


async def _drop_keyboard(bot: Bot, chat_id: int, message_id: int | None) -> None:
    """Best-effort ``edit_reply_markup(None)`` on a stored message id.

    Used by /cpc_cancel and the timeout sweeper to retire dead inline
    keyboards (accept/decline on the opponent's challenge card; move
    buttons on either seat). Swallows the common race failures —
    message already deleted, already edited to identical markup,
    forbidden by the user — because every caller's terminal action is
    "match is dead", and a failed keyboard-drop is purely cosmetic
    once the FSM is cleared.
    """
    if message_id is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        # ``message to edit not found`` / ``message is not modified`` /
        # bot blocked — all benign at this point.
        log.bind(chat_id=chat_id, message_id=message_id).debug(
            "drop_keyboard swallowed; non-fatal at terminal-state cleanup",
        )


async def handle_cpc_cancel(message: Message, state: FSMContext, bot: Bot, lang: str) -> None:
    """``/cpc_cancel`` — challenger-side abort, mid-flight.

    Variant A architecture (``fsm/rps.py``) makes the challenger the
    sole FSM owner, so /cpc_cancel reads the caller's state directly.
    Three branches:

    * No state → ``cancel_no_active`` card. Idempotent with the
      global /cancel which also no-ops on a clean FSM.
    * ``awaiting_acceptance`` → notify the opponent (with their
      challenge card's keyboard dropped) and confirm to the
      challenger. No escrow has run yet — see the module docstring's
      "Escrow timing" pin — so this branch has nothing to roll back.
    * ``awaiting_moves`` → same shape, but the dead keyboards live on
      potentially two cards (one in each seat's PM). Again no
      rollback: :class:`RpsService.play` is the ONLY money-mover and
      it only fires at resolution.

    The opponent cannot use /cpc_cancel — Variant A's docstring spells
    out why. Their escape hatches are the inline "❌ Decline" button
    during awaiting_acceptance, and waiting out the move-timeout
    during awaiting_moves (the sweeper does the rest).
    """
    tg_user = require_from_user(message)

    state_name = await state.get_state()
    if state_name is None:
        await message.answer(t("h_rps_cancel_no_active", lang))
        return

    data = await state.get_data()
    opponent_id = int(data.get("opponent_id", 0))

    # R-FIX-009-fp: cancel is terminal — serialise against an in-flight
    # move click via the match lock.
    challenger_chat_id = int(data.get("challenger_chat_id", tg_user.id))
    async with _match_lock_cm(
        bot_id=bot.id,
        chat_id=challenger_chat_id,
        user_id=tg_user.id,
    ):
        # Drop dead keyboards on the opponent's side first; the FSM clear
        # at the end is what makes future clicks see ``match_not_found``,
        # but a clean visual transition is worth the two edits.
        if state_name == RpsStates.awaiting_acceptance.state:
            await _drop_keyboard(bot, opponent_id, data.get("opponent_accept_message_id"))
        elif state_name == RpsStates.awaiting_moves.state:
            await _drop_keyboard(bot, opponent_id, data.get("opponent_move_message_id"))
            # Challenger's own move card lives in their own PM (chat ==
            # user_id since /cpc is private-only).
            await _drop_keyboard(bot, tg_user.id, data.get("challenger_move_message_id"))

        await state.clear()

    # Notify the opponent. An unreachable opponent is benign here —
    # match is cleared, no leak. #305: letting ``chat not found`` escape
    # would have skipped the challenger's own confirmation below.
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(
            opponent_id,
            t("h_rps_cancel_opponent_notified", lang, challenger_id=tg_user.id),
        )
    await message.answer(t("h_rps_cancel_challenger_notified", lang, opponent_id=opponent_id))
    log.bind(
        uid=tg_user.id,
        opponent_id=opponent_id,
        prior_state=state_name,
    ).info("/cpc_cancel processed")


async def cancel_match_from_global(
    bot: Bot,
    *,
    state: FSMContext,
    state_name: str,
    data: dict[str, object],
    caller_id: int,
    lang: str,
) -> None:
    """Terminal teardown of a live /cpc match for the global ``/cancel``.

    The challenger already has a dedicated abort — :func:`handle_cpc_cancel`
    — which holds the match lock across the clear (R-FIX-009-fp) and tells
    the opponent the match is over. The generic ``/cancel`` reached the same
    FSM under the same key and did neither: it dropped the dead keyboards
    and cleared, unlocked and in silence. One command, two standards of care
    for the same state, and the unlocked one sat on the path whose
    resolution step moves money. Routing the generic path through the
    careful one is what stops the two drifting apart again (#259).

    No rollback, for the same reason /cpc_cancel needs none:
    :meth:`RpsService.play` is the only money-mover and it fires only at
    resolution (module docstring, "Escrow timing").

    The challenger's own confirmation is deliberately NOT sent from here —
    ``/cancel`` prints its own, and two acknowledgements for one command
    read as a bug.
    """

    def _int_field(field: str) -> int | None:
        raw = data.get(field)
        return raw if isinstance(raw, int) else None

    opponent_id = _int_field("opponent_id")
    challenger_chat_id = _int_field("challenger_chat_id")
    if challenger_chat_id is None:
        # /cpc is private-only, so the challenger's chat id IS their user
        # id whenever the stamp is missing — the same fallback
        # /cpc_cancel uses, and the same one that makes the lock key
        # match the FSM StorageKey.
        challenger_chat_id = caller_id

    async with _match_lock_cm(bot_id=bot.id, chat_id=challenger_chat_id, user_id=caller_id):
        if opponent_id is not None:
            if state_name == RpsStates.awaiting_acceptance.state:
                await _drop_keyboard(bot, opponent_id, _int_field("opponent_accept_message_id"))
            elif state_name == RpsStates.awaiting_moves.state:
                await _drop_keyboard(bot, opponent_id, _int_field("opponent_move_message_id"))
                await _drop_keyboard(bot, caller_id, _int_field("challenger_move_message_id"))
        await state.clear()

    if opponent_id is not None:
        with contextlib.suppress(TelegramForbiddenError, TelegramBadRequest):
            await bot.send_message(
                opponent_id,
                t("h_rps_cancel_opponent_notified", lang, challenger_id=caller_id),
            )
    log.bind(uid=caller_id, opponent_id=opponent_id, prior_state=state_name).info(
        "/cancel tore down a /cpc match under the match lock"
    )


# ── Stage 35 timeout callbacks — invoked by FsmTimeoutSweeper ─────────
#
# The sweeper hands these ``(bot, key, data)``. The key carries the
# challenger's id (Variant A FSM owner == challenger), and data is the
# last-known FSM dict — opponent_id, bet, message ids. Both callbacks
# clear keyboards on the sweeper's behalf BEFORE returning; the sweeper
# itself then clears the FSM (post-callback ordering, see
# :meth:`FsmTimeoutSweeper.sweep_once`).


async def on_expire_awaiting_acceptance(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """Timeout callback for ``RpsStates.awaiting_acceptance``.

    Mirrors legacy's ``ACCEPT_TIMEOUT_SEC = 60`` posture
    (``rock_paper_scissors.py:42,231``): notify the challenger that
    the opponent didn't respond, notify the opponent that their
    window closed, drop the accept/decline keyboard on the dead card.
    No escrow rollback — see /cpc_cancel docstring.

    #122: a missing ``opponent_id`` used to abandon the whole callback
    at the first line, leaving the challenger — whose id the key
    carries unconditionally — waiting on a match that had already
    expired. Only the opponent-side half genuinely depends on the id;
    everything else runs either way. (The per-match Lock release that
    the early return also used to skip is not this callback's problem
    at all any more — the registry frees its own slots, see
    :data:`_match_locks`.)
    """
    challenger_id = key.user_id
    opponent_id_raw = data.get("opponent_id")
    opponent_id = opponent_id_raw if isinstance(opponent_id_raw, int) else None
    if opponent_id is None:
        log.bind(challenger_id=challenger_id, data=data).warning(
            "timeout: accept stage missing opponent_id; challenger half only",
        )

    # #126: the match lock is held around this whole callback by
    # :func:`expiry_guard`, and the registry slot is freed by the
    # refcount when that guard exits — the manual drop that used to sit
    # here, ahead of the sweeper's clear and of the I/O below, was the
    # window where a click could build a second Lock over a live match.
    accept_msg_id = data.get("opponent_accept_message_id")
    if opponent_id is not None and isinstance(accept_msg_id, int):
        await _drop_keyboard(bot, opponent_id, accept_msg_id)

    # Locale: the challenge handler stamps the effective ``lang`` into FSM
    # data at set-time, so the sweeper honours it (mirrors duel's
    # ``_lang_from_fsm``). Falls back to the project default if absent.
    _raw_lang = data.get("lang")
    lang = _raw_lang if isinstance(_raw_lang, str) else "ru"
    # #305: both halves. These run inside ``FsmTimeoutSweeper``, which
    # catches the exception, counts an error and *keeps the state* so the
    # next pass retries — so an escaping ``chat not found`` here is not a
    # lost notification, it is a permanent 30-second loop.
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(challenger_id, t("h_rps_timeout_accept_challenger", lang))
    if opponent_id is not None:
        with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.send_message(opponent_id, t("h_rps_timeout_accept_opponent", lang))
    log.bind(challenger_id=challenger_id, opponent_id=opponent_id, stage="accept").info(
        "/cpc timeout expired by sweeper"
    )


async def on_expire_awaiting_moves(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """Timeout callback for ``RpsStates.awaiting_moves``.

    Matches legacy's ``CHOOSE_TIMEOUT_SEC = 30``
    (``rock_paper_scissors.py:43,254``): ``app.py:87-91`` registers this
    state with ``timeout_seconds=30``. An earlier version of this
    docstring described a uniform 60s budget and a blocker — that
    reaching parity would need the *accept moment* persisted across the
    state transition. Both statements are stale: the accept handler
    re-stamps ``STATE_ENTERED_AT_FIELD`` in the same ``update_data``
    that clears the move slots, immediately after it sets
    ``RpsStates.awaiting_moves``, so the 30s is measured from the
    accept, which is the reference point legacy used. (That correction
    used to name an ``_enter_awaiting_moves`` helper. There is no such
    helper — the transition is inline — so this says where to look
    instead of naming a function to grep for and not find.) Both seats' move keyboards get
    dropped if their message ids are known.

    #122: same split as :func:`on_expire_awaiting_acceptance` — a
    missing ``opponent_id`` costs the opponent their half, not the
    lock release and not the challenger's notice.
    """
    challenger_id = key.user_id
    opponent_id_raw = data.get("opponent_id")
    opponent_id = opponent_id_raw if isinstance(opponent_id_raw, int) else None
    if opponent_id is None:
        log.bind(challenger_id=challenger_id, data=data).warning(
            "timeout: moves stage missing opponent_id; challenger half only",
        )

    # #126: no registry drop here either — see
    # :func:`on_expire_awaiting_acceptance`.
    challenger_msg_id = data.get("challenger_move_message_id")
    if isinstance(challenger_msg_id, int):
        await _drop_keyboard(bot, challenger_id, challenger_msg_id)
    opponent_msg_id = data.get("opponent_move_message_id")
    if opponent_id is not None and isinstance(opponent_msg_id, int):
        await _drop_keyboard(bot, opponent_id, opponent_msg_id)

    _raw_lang = data.get("lang")
    lang = _raw_lang if isinstance(_raw_lang, str) else "ru"
    # #305: same sweeper-retry reasoning as ``on_expire_awaiting_accept``.
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(challenger_id, t("h_rps_timeout_moves_challenger", lang))
    if opponent_id is not None:
        with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.send_message(opponent_id, t("h_rps_timeout_moves_opponent", lang))
    log.bind(challenger_id=challenger_id, opponent_id=opponent_id, stage="moves").info(
        "/cpc timeout expired by sweeper"
    )


# R-FIX-008-fp: Legacy callback compatibility shims (deploy window).
# -------------------------------------------------------------------
# The new wire format (post-R-FIX-008) is ``rps_acc:<challenger>:<bet>:<chat>``
# / ``rps_dec:<challenger>:<chat>`` / ``rps_mv:<challenger>:<move>:<chat>``.
# The OLD wire format (pre-R-FIX-008, still in flight on cards rendered
# before the deploy boundary) omits the trailing ``chat_id`` field:
# ``rps_acc:<challenger>:<bet>``, ``rps_dec:<challenger>``,
# ``rps_mv:<challenger>:<move>``.
#
# Two shapes share the same first segment so aiogram's CallbackData
# filter (which dispatches on the exact field count) won't match the
# legacy form. The fallback handlers below detect the legacy length,
# synthesise the missing ``chat_id`` from ``callback.message.chat.id``
# (the chat where the button card lives — same chat the challenger
# typed ``/cpc`` in, which is exactly what the new payload carries),
# and reuse the canonical handlers. Without this, every button rendered
# before deploy would surface ``match_not_found`` instead of resolving.
#
# Remove after the longest legacy-card lifetime (24h timeout sweeper)
# has elapsed post-deploy.
def _legacy_chat_id(callback: CallbackQuery) -> int | None:
    """Derive chat_id from the message the legacy button is attached to.

    Returns ``None`` if the message reference is missing or is an
    inaccessible-message stub — caller should toast and bail.
    """
    msg = callback.message
    if isinstance(msg, MessageType):
        return msg.chat.id
    return None


async def handle_rps_accept_legacy(
    callback: CallbackQuery,
    bot: Bot,
    fsm_storage: BaseStorage,
    game_limit_service: GameLimitService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Legacy ``rps_acc:<challenger>:<bet>`` (no chat_id). See R-FIX-008-fp."""
    assert callback.data is not None
    parts = callback.data.split(":")
    if len(parts) != 3:
        return  # not legacy shape; new-format handler already matched
    try:
        challenger_id = int(parts[1])
        bet = int(parts[2])
    except ValueError:
        # #122: the payload matched our prefix and colon count but does
        # not parse, so no other handler will pick this up — a bare
        # ``return`` left Telegram's button clock spinning until it
        # timed out. From the tapper's side a card whose payload is
        # corrupt is a dead card, which is what the toast says.
        await callback.answer(t("h_rps_match_not_found", lang), show_alert=False)
        return
    chat_id = _legacy_chat_id(callback)
    if chat_id is None:
        await callback.answer(t("h_rps_match_not_found", lang), show_alert=False)
        return
    await handle_rps_accept(
        callback=callback,
        callback_data=RpsAccept(challenger_id=challenger_id, bet=bet, chat_id=chat_id),
        bot=bot,
        fsm_storage=fsm_storage,
        game_limit_service=game_limit_service,
        lang=lang,
        checkpoint=checkpoint,
    )


async def handle_rps_decline_legacy(
    callback: CallbackQuery,
    bot: Bot,
    fsm_storage: BaseStorage,
    lang: str,
) -> None:
    """Legacy ``rps_dec:<challenger>`` (no chat_id). See R-FIX-008-fp."""
    assert callback.data is not None
    parts = callback.data.split(":")
    if len(parts) != 2:
        return
    try:
        challenger_id = int(parts[1])
    except ValueError:
        # #122: see :func:`handle_rps_accept_legacy` — an unparseable
        # payload is nobody else's to answer.
        await callback.answer(t("h_rps_match_not_found", lang), show_alert=False)
        return
    chat_id = _legacy_chat_id(callback)
    if chat_id is None:
        await callback.answer(t("h_rps_match_not_found", lang), show_alert=False)
        return
    await handle_rps_decline(
        callback=callback,
        callback_data=RpsDecline(challenger_id=challenger_id, chat_id=chat_id),
        bot=bot,
        fsm_storage=fsm_storage,
        lang=lang,
    )


async def handle_rps_move_legacy(
    callback: CallbackQuery,
    bot: Bot,
    fsm_storage: BaseStorage,
    rps_service: RpsService,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
) -> None:
    """Legacy ``rps_mv:<challenger>:<move>`` (no chat_id). See R-FIX-008-fp."""
    assert callback.data is not None
    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    try:
        challenger_id = int(parts[1])
    except ValueError:
        # #122: see :func:`handle_rps_accept_legacy`.
        await callback.answer(t("h_rps_match_not_found", lang), show_alert=False)
        return
    move = parts[2]
    chat_id = _legacy_chat_id(callback)
    if chat_id is None:
        await callback.answer(t("h_rps_match_not_found", lang), show_alert=False)
        return
    await handle_rps_move(
        callback=callback,
        callback_data=RpsMoveCallback(challenger_id=challenger_id, move=move, chat_id=chat_id),
        bot=bot,
        fsm_storage=fsm_storage,
        rps_service=rps_service,
        bonds_write_repo=bonds_write_repo,
        lang=lang,
    )


def build_router(registry: EngineRegistry) -> Router:
    """Factory — fresh router + middleware per call so tests can re-wire.

    Stage 31: Extended to support group chats with rate limiting.
    Private chats work as before; group invocations are subject to the
    same per-user token bucket.

    EconomyMiddleware on BOTH message and callback observers so the
    handler sees ``rps_service`` injected on the callback side (where
    the actual escrow + payout runs).

    SessionMiddleware on the message side only — UsersRepo is needed
    for the @username resolution path at /cpc entry, but callbacks
    never read users.db.

    There is deliberately NO per-chat feature gate here. An earlier
    revision registered a ``FeatureGateMiddleware`` and hung a
    ``RequireFeature("cpc")`` filter on both commands, but aiogram 3
    resolves filters BEFORE inner (router-level) middlewares — outer
    middlewares → filters → inner middlewares → handler — so the filter
    never saw the ``feature_gate`` the middleware injects. It took its
    "middleware not installed" branch on every single ``/cpc``, logged a
    WARNING, and returned True. The gate was inert; its only production
    effect was that false warning. Wiring it as an OUTER middleware
    would have been worse: the default policy listed ``cpc`` as
    disruptive, so every group would have lost ``/cpc`` with no way back
    (``set_feature`` had no callers and the flags lived in per-instance
    memory, lost on restart). Per-command availability already has a
    real, DB-backed, operator-facing home in ``/cmdcfg``
    (:mod:`handlers.command_access`, an outer middleware with a
    localized refusal); the rate limiter below covers the "don't let
    /cpc flood a group" half. Two gates for one job was the bug.
    """
    from telegram_invite_bot.middlewares.rate_limit import RateLimitMiddleware

    router = Router(name="rps")

    # No private-only filter — /cpc is a group game too. Availability
    # per chat is /cmdcfg's job (see the docstring above).

    # Add middlewares in order: rate limit -> economy -> session
    router.message.middleware(RateLimitMiddleware(capacity=5, refill_per_second=1.0 / 30.0))
    router.message.middleware(EconomyMiddleware(registry))
    router.message.middleware(SessionMiddleware(registry))
    router.callback_query.middleware(EconomyMiddleware(registry))
    # L-40: the move-resolution path now reads/writes users.db (the
    # spouse-XP grant via ``bonds_write_repo``), so the callback side
    # gets a SessionMiddleware too — independent session/commit from
    # the EconomyMiddleware one, same dual-DB posture as
    # ``handlers/couple_activities.py``.
    router.callback_query.middleware(SessionMiddleware(registry))

    router.message.register(
        handle_cpc,
        Command("cpc", "rps", "кнб", "knb", ignore_case=True),
        F.from_user,
    )
    router.message.register(
        handle_cpc_cancel,
        Command("cpc_cancel", "кнб_отмена", ignore_case=True),
        F.from_user,
    )
    router.callback_query.register(handle_rps_accept, RpsAccept.filter(), F.from_user)
    router.callback_query.register(handle_rps_decline, RpsDecline.filter(), F.from_user)
    router.callback_query.register(handle_rps_move, RpsMoveCallback.filter(), F.from_user)
    # R-FIX-008-fp: legacy-format fallbacks. Registered AFTER the
    # new-format handlers so aiogram's first-match dispatch prefers the
    # canonical shape; legacy only fires when the field-count mismatch
    # caused the CallbackData filter to reject. Filter on the literal
    # ``rps_acc:``/``rps_dec:``/``rps_mv:`` prefix and on the legacy
    # field count (2 colons → accept/move, 1 colon → decline).
    router.callback_query.register(
        handle_rps_accept_legacy,
        F.data.startswith("rps_acc:") & F.data.func(lambda d: d.count(":") == 2),
        F.from_user,
    )
    router.callback_query.register(
        handle_rps_decline_legacy,
        F.data.startswith("rps_dec:") & F.data.func(lambda d: d.count(":") == 1),
        F.from_user,
    )
    router.callback_query.register(
        handle_rps_move_legacy,
        F.data.startswith("rps_mv:") & F.data.func(lambda d: d.count(":") == 2),
        F.from_user,
    )
    return router
