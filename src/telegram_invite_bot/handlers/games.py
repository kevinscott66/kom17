"""Games handlers — Stages 12 + 18.

Stage 12 shipped ``/dice`` (the no-args vanity roll). Stage 18 adds the
**vanity-guess** slices of two legacy commands:

* ``/roll <1-6>`` / ``/кубик <1-6>`` — guess a number; the bot rolls a
  native dice and tells you whether you got it. Legacy at
  ``bot.py:17334``.
* ``/flip <орёл|решка>`` / ``/монетка ...`` — call heads or tails; the
  bot flips a coin (pure Python ``random``) and tells you the result.
  Legacy at ``bot.py:17446``.

Filters are deliberately TIGHT — each handler only matches the exact
vanity input form. Everything else (bare command, invalid guess) falls
through. That used to mean legacy rendered its usage hint; T-011
removed legacy, and :mod:`handlers.unknown_form` (#158) was added for
exactly this — it matches the command words the tree registers and no
argument shape at all, so a mistyped ``/roll`` reaches it and gets the
``/help`` line back instead of silence. The tight filters here are
still the same minimal-surface-area pattern Stage 17's ``/buy`` used;
what changed is who answers the forms they decline.

L-17 adds the **stake variants** on the SAME commands:

* ``/roll <bet> <1-6>`` — legacy ``cmd_roll`` bet branch
  (``bot.py:17346-17383``) + ``DiceGame.play`` (``bot.py:14605``).
  Atomic stake debit, Telegram server-side dice roll, and on a correct
  guess a checked gross credit of ``bet × 5.7`` (``DICE_MULTIPLIER`` —
  legacy's 6 was zero-edge, ``bot.py:2575``).
* ``/flip <bet> орёл|решка`` — legacy ``cmd_flip`` bet branch
  (``bot.py:17462-17493``) + ``FlipGame.play`` (``bot.py:14687``).
  Same flow with a 50/50 coin and a ``bet × 1.9`` gross payout
  (``FLIP_MULTIPLIER`` — legacy's 2 was zero-edge, ``bot.py:2578``).

Both stake paths are GROUP-ONLY (legacy ``require_group=True``,
``bot.py:17339/17455``; the vanity paths stay private-friendly, see
below), run the persistent :class:`GameLimitService` anti-abuse caps
(check → play → record, exactly the /roulette posture), and settle via
:class:`~telegram_invite_bot.services.stake_games_service.
StakeGamesService` — atomic debit, checked credit, A-11 ``games``-row
write + achievements on one economy session. The stake handlers live on
a CHILD router with their own :class:`EconomyMiddleware` so the vanity
handlers stay session-free.

Legacy inline-keyboard note: legacy rendered a button pair
(``flip_choice_orel`` / ``flip_choice_reshka``, ``bot.py:17505-17508``)
for the *bare* ``/flip``; we replaced that with an animated coin toss
(:func:`handle_flip`) since legacy is dead in prod and a bare ``/flip``
must produce a visible result rather than a button that no callback
handler answers.

Both stake cards close with the remaining anti-abuse allowance
(RR-3 #34, :func:`~telegram_invite_bot.handlers.game_cards
.render_allowance`) — the windows are shared with ``/roulette``, so the
footer is rendered by the same helper.

Still in legacy: **``/cpc``** — multi-turn PvP state through a
``threading.Lock``-guarded in-memory manager. Needs aiogram FSM with a
persistent backing store; a memory-only port would lose state on every
restart, which legacy already does — but porting *only* the handler
without fixing it means writing throwaway code. (``/duel`` and the
``/games`` menu have since been ported — see
:mod:`telegram_invite_bot.handlers.duel`.)

Why widen vanity to private chats (legacy is group-only):
The legacy ``require_group=True`` guard exists to keep group games
visible to everyone in the room. The vanity-no-bet path has no
economy or social side effects, so private use is harmless. ``/dice``
already widened this in Stage 12 — keeping the policy consistent
across the games module avoids one-off surprise for users.

Two legacy operator/group gates that the widening does NOT honour
(land with the bet-variant stage that re-introduces the wider
games-policy surface area):

* ``PVP_GAMES_ONLY`` env flag (``bot.py:17451``) — if set, legacy
  refuses ``/flip`` entirely. Our port currently runs vanity flips
  regardless. Operators using this flag should keep the bet variant
  in legacy and wait for the Stage-N port to add the gate.
* ``require_group_feature(..., "games", ...)`` — group admins can
  disable the ``games`` feature per chat (``bot.py:17341, 17457``).
  Vanity here bypasses the gate. Same Stage 12 precedent.
(``AUTO_DELETE_GAMES`` + ``GAME_MESSAGES_TTL``, legacy ``bot.py:17381,
17491-17492`` — group game replies swept after a TTL — is back as of
RR-3 #33, via ``GAMES_AUTO_DELETE``/``GAMES_MESSAGE_TTL`` and
:func:`~telegram_invite_bot.handlers.game_cards.schedule_card_sweep`. It
defaults OFF rather than legacy's ON; see
:class:`~telegram_invite_bot.config.settings.GamesConfig`.)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Literal

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
    schedule_card_sweep,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.services.stake_games_service import (
    DICE_MAX_BET,
    DICE_MIN_BET,
    DICE_MULTIPLIER,
    FLIP_MAX_BET,
    FLIP_MIN_BET,
    FLIP_MULTIPLIER,
    StakeGamesService,
    StakeOutcome,
    StakeResult,
)
from telegram_invite_bot.utils.aiogram import (
    command_body,
    edit_card,
    require_from_user,
)
from telegram_invite_bot.utils.keyed_locks import KeyedLocks
from telegram_invite_bot.utils.numbers import format_number, is_int_token
from telegram_invite_bot.utils.rng import money_rng

log = logger.bind(component="handlers.games")

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
    from telegram_invite_bot.services.game_limit_service import (
        GameAbuseCheck,
        GameLimitService,
    )


# Heads aliases the user might type. Mirrors legacy
# ``_normalize_flip_guess`` at ``bot.py:17436`` so anyone who used
# ``/flip орл`` or ``/flip heads`` keeps working unchanged.
_HEADS_ALIASES = frozenset({"орёл", "орел", "орл", "eagle", "heads"})
_TAILS_ALIASES = frozenset({"решка", "решку", "tails"})
# #1350: ``/flip`` and the PvP coin canonicalise the SAME concept
# two different ways. Here the canonical token stays legacy's
# Russian one — it is what ``_normalize_flip_guess`` mirrors
# byte-for-byte and what the game log persists in ``detail`` — while
# ``games/pvp.py`` uses Latin ``heads``/``tails`` because those
# double as its ``h_pvp_side_*`` key suffixes. Both are
# ``Literal``-typed so mypy refuses the crossing: a Latin token
# reaching :func:`_flip_side_label` would silently render the wrong
# face, and a Russian one reaching the PvP card would leak the raw
# key ``h_pvp_side_орёл``.
FlipSide = Literal["орёл", "решка"]

_HEADS_CANONICAL: FlipSide = "орёл"
_TAILS_CANONICAL: FlipSide = "решка"


def _flip_side_label(side: FlipSide, lang: str) -> str:
    """Localized DISPLAY label for a canonical coin side.

    The canonical value (``орёл``/``решка``) stays Russian everywhere it
    matters for logic (RNG output, guess normalisation). Only the string
    shown to the user is localized — EN sees ``heads``/``tails``.
    """
    key = "h_flip_side_heads" if side == _HEADS_CANONICAL else "h_flip_side_tails"
    return t(key, lang)


# Telegram's dice family (🎲🎯🏀⚽🎳🎰) has no coin, so /flip can't use a
# native server-side animation the way /dice does. We mimic the feel
# with a two-step message: send the 🪙 "throw", pause, then edit the
# SAME message into the result — the user sees one message transition
# (spin → settled) instead of two separate replies. The throw copy lives
# in the ``h_flip_throw`` i18n key; this is just the module-level delay
# so tests can zero it out.
_FLIP_REVEAL_DELAY = 1.4


def _flip_side() -> FlipSide:
    """Single source of truth for the coin RNG.

    ``< 0.5`` → ``орёл`` — same threshold as legacy at ``bot.py:17497``,
    kept in one place so the bare toss and the guess path can't drift
    apart.

    The generator is :data:`~telegram_invite_bot.utils.rng.money_rng`
    rather than the module-level ``random``, and this function is
    exactly why: the free ``/flip`` and the staked ``/flip 500 орёл``
    both land here, so a predictable stream would let anyone spin the
    free coin until they could read the paid one.
    """
    return _HEADS_CANONICAL if money_rng.random() < 0.5 else _TAILS_CANONICAL


def _normalize_flip_guess(token: str) -> FlipSide | None:
    """Return ``'орёл'`` / ``'решка'`` for any legacy alias, else ``None``.

    Tokens are lowercased before lookup so ``/flip ОРЁЛ`` works the
    same as ``/flip орёл``. Same byte-level coverage as legacy.
    """
    norm = token.strip().lower()
    if norm in _HEADS_ALIASES:
        return _HEADS_CANONICAL
    if norm in _TAILS_ALIASES:
        return _TAILS_CANONICAL
    return None


def _is_vanity_roll(message: Message) -> bool:
    """Tight filter: exactly one arg, an int in 1..6, no bet token.

    Anything else (bare command, ``/roll abc``, ``/roll 7``,
    ``/roll 100 4``) falls through so legacy's usage / bet code runs.
    Keeps our surface area minimal and lets us delete this whole
    module the day legacy goes away without leaving copy behind.
    """
    parts = command_body(message).split()
    args = parts[1:]
    if len(args) != 1 or not is_int_token(args[0]):
        return False
    return 1 <= int(args[0]) <= 6


def _is_vanity_flip(message: Message) -> bool:
    """Same shape as :func:`_is_vanity_roll`, but for coin tosses.

    Two-token forms (legacy bet: ``/flip 100 орёл``) fall through.
    Bare ``/flip`` is owned by :func:`handle_flip` (animated toss) — it
    matches a disjoint ``magic=F.args.is_(None)`` registration, not this
    filter, so there's no overlap.
    """
    parts = command_body(message).split()
    args = parts[1:]
    if len(args) != 1:
        return False
    return _normalize_flip_guess(args[0]) is not None


def _is_stake_roll(message: Message) -> bool:
    """Stake form: ``/roll <bet> <guess>`` — two leading digit tokens.

    Mirrors the legacy bet-branch entry condition (``len(args) >= 2 and
    args[0].isdigit() and args[1].isdigit()``, bot.py:17346). A guess
    outside 1..6 still MATCHES — the handler renders the localised
    "1 to 6" hint (legacy fell to its usage text, bot.py:17400-17404);
    swallowing it here would leave the user with silence.
    """
    parts = command_body(message).split()
    args = parts[1:]
    return len(args) >= 2 and is_int_token(args[0]) and is_int_token(args[1])


def _is_stake_flip(message: Message) -> bool:
    """Stake form: ``/flip <bet> <side…>`` — digit bet + at least one more
    token.

    Mirrors legacy (``len(args) >= 2 and args[0].isdigit()``,
    bot.py:17462). An unrecognisable side still MATCHES so the handler
    can render the localised side hint instead of dropping the message
    (legacy showed its keyboard fallback there, which we don't port).
    Garbage like ``/flip pizza`` (non-digit first token) keeps falling
    through, same as before.
    """
    parts = command_body(message).split()
    args = parts[1:]
    return len(args) >= 2 and is_int_token(args[0])


# ── L-17 stake flows ─────────────────────────────────────────────────
#
# Per-user locks serialising one user's check→play→record so the
# anti-abuse caps can't be raced by two concurrent stake updates from
# the same user.
#
# An ALIAS of the one ecosystem-wide registry, not a registry of its
# own (#222). The GameLimitService windows count plays across every
# game — ``GameLimitsRepo.count_since`` filters on ``user_id`` alone —
# so a lock that spans only this module leaves /roll racing /roulette
# through the very window it was added to protect, which is what this
# used to do. ``handlers.roulette._play_locks`` is the same object.
_stake_locks: KeyedLocks[int] = PLAY_LOCKS


async def _stake_gates(
    message: Message,
    *,
    user_id: int,
    bet: int,
    min_bet: int,
    max_bet: int,
    economy_repo: EconomyRepo,
    transactions_repo: TransactionsRepo,
    game_limit_service: GameLimitService,
    now: datetime,
    lang: str,
) -> tuple[StakeGamesService, GameAbuseCheck] | None:
    """Run the shared pre-play gates; reply + return ``None`` on a block.

    Order: anti-abuse caps first (read-only, so a capped user never
    burns a wallet read), then the service's bounds/affordability
    pre-check. Mirrors the /roulette handler's sequence; the localised
    refusals reuse the ``h_roulette_*`` keys because the legacy copy was
    game-agnostic ("Минимальная ставка…", "Недостаточно средств…").

    Returns the ready :class:`StakeGamesService` together with the
    passing :class:`GameAbuseCheck` — the caller needs the latter to
    render the remaining-allowance footer (RR-3 #34) on the result card.
    """
    abuse = await game_limit_service.check(user_id, now=now)
    if not abuse.allowed:
        await message.answer(render_abuse_refusal(lang, abuse))
        log.bind(uid=user_id, reason=abuse.reason).info("stake game blocked")
        return None

    service = StakeGamesService(economy_repo, transactions_repo)
    rejection = await service.validate(user_id=user_id, bet=bet, min_bet=min_bet, max_bet=max_bet)
    if rejection is not None:
        await _reply_stake_rejection(
            message, rejection, min_bet=min_bet, max_bet=max_bet, lang=lang
        )
        return None
    return service, abuse


async def _reply_stake_rejection(
    message: Message,
    rejection: StakeResult,
    *,
    min_bet: int,
    max_bet: int,
    lang: str,
) -> None:
    """Localised refusal for a non-SUCCESS :class:`StakeResult`."""
    if rejection.outcome is StakeOutcome.BELOW_MIN_BET:
        await message.answer(t("h_roulette_min_bet", lang, min_bet=min_bet))
    elif rejection.outcome is StakeOutcome.ABOVE_MAX_BET:
        await message.answer(t("h_roulette_max_bet", lang, max_bet=max_bet))
    else:  # NO_WALLET / INSUFFICIENT_FUNDS — legacy showed the balance.
        await message.answer(t("h_roulette_insufficient", lang, balance=rejection.balance or 0))


async def handle_roll_bet(
    message: Message,
    economy_repo: EconomyRepo,
    transactions_repo: TransactionsRepo,
    game_limit_service: GameLimitService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/roll <bet> <1-6>`` — dice stake (L-17, legacy bot.py:17346).

    Group-only (legacy ``require_group=True``). Telegram's server-side
    dice animation supplies the roll (legacy parity, bot.py:17362-17366);
    payout on a correct guess is ``bet × DICE_MULTIPLIER`` gross,
    settled by :class:`StakeGamesService` with an atomic debit +
    checked credit. That multiplier is 5.7, NOT legacy's 6
    (``bot.py:2575``): 6/6 is a zero-edge game, 5.7/6 carries the same
    ~5% house edge /roulette does. The deviation is deliberate and
    argued in ``services/stake_games_service.py:17-22`` — read it
    before recomputing this game's EV from the legacy number.
    """
    tg_user = require_from_user(message)
    if message.chat.type not in GROUP_TYPE_NAMES:
        await message.answer(t("h_stake_group_only", lang))
        return

    parts = command_body(message).split()
    bet, guess = int(parts[1]), int(parts[2])  # filter-guaranteed digits
    if not 1 <= guess <= 6:
        await message.answer(t("h_roll_bet_invalid_guess", lang))
        return

    now = datetime.now()  # noqa: DTZ005 — naive local, matches game_plays
    # #1564: ``answer_dice`` below is a network round trip made while
    # holding this lock, and that is deliberate. ``_stake_locks`` is
    # ``PLAY_LOCKS`` (games/limits.py), one per-user lock shared with
    # /flip and /roulette, so a slow or hung Telegram call blocks all
    # three commands for this player until it returns. The alternative
    # — releasing between the spin and ``settle`` — reopens #222-B: the
    # play stamp is only visible to other connections once the commit
    # at the end of this block lands, so a gap there lets the same
    # player's next update read ``game_plays`` without the row and walk
    # straight through the cooldown. The ordering is also legacy's
    # (validate, then spin), which is why the dice is inside at all.
    # Do not "optimise" the network call out from under the lock.
    #
    # Only /roll is affected: /flip draws locally with ``_flip_side()``
    # and /roulette spins on its injected RNG, so neither holds the
    # lock across anything but database work.
    async with _stake_locks.acquire(tg_user.id):
        gates = await _stake_gates(
            message,
            user_id=tg_user.id,
            bet=bet,
            min_bet=DICE_MIN_BET,
            max_bet=DICE_MAX_BET,
            economy_repo=economy_repo,
            transactions_repo=transactions_repo,
            game_limit_service=game_limit_service,
            now=now,
            lang=lang,
        )
        if gates is None:
            return
        service, abuse = gates

        # Animation AFTER validation (legacy order: validate_bet →
        # send_dice, bot.py:17356-17362) so an invalid bet never spins.
        sent = await message.answer_dice(emoji="🎲")
        roll = sent.dice.value if sent.dice is not None else 0
        if not 1 <= roll <= 6:
            # Defensive fallback — same posture as handle_roll_guess and
            # legacy bot.py:17367-17368.
            roll = money_rng.randint(1, 6)

        result = await service.settle(
            user_id=tg_user.id,
            bet=bet,
            game="dice",
            won=roll == guess,
            multiplier=DICE_MULTIPLIER,
            detail={"roll": roll, "guess": guess, "multiplier": DICE_MULTIPLIER},
        )
        if result.outcome is not StakeOutcome.SUCCESS:
            # Debit lost a race after the pre-check — refuse, no stamp.
            #
            # #1967: release the write transaction before the refusal is
            # sent. ``settle`` reaches this branch through a ``debit``
            # whose ``WHERE balance >= amount`` matched zero rows, and a
            # guarded UPDATE that changes nothing still promotes the
            # connection to ``BEGIN IMMEDIATE`` (``db/engines.py``) — so
            # ``economy.db`` would otherwise be locked over a write that
            # never happened for the whole Telegram round trip, against
            # a 5s ``busy_timeout``. Same shape as ``/daily``'s
            # RACE_LOST checkpoint (#1861).
            if checkpoint is not None:
                await checkpoint()
            await _reply_stake_rejection(
                message, result, min_bet=DICE_MIN_BET, max_bet=DICE_MAX_BET, lang=lang
            )
            return
        # Stamp the completed play (rejections above recorded nothing —
        # a typo never burns a cooldown slot; /roulette parity).
        await game_limit_service.record(tg_user.id, game="dice", now=now)
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

    key = "h_roll_bet_win" if result.won else "h_roll_bet_lose"
    body = t(key, lang, roll=roll, guess=guess, payout=result.payout, bet=bet)
    assert result.balance is not None  # SUCCESS always carries it
    body += t("h_roulette_balance", lang, balance=result.balance)
    body += render_achievements(lang, result.awarded)
    body += render_allowance(lang, abuse)
    await post_game_card(message, body)
    log.bind(uid=tg_user.id, bet=bet, guess=guess, roll=roll, won=result.won).info(
        "/roll stake played"
    )


async def handle_flip_bet(
    message: Message,
    economy_repo: EconomyRepo,
    transactions_repo: TransactionsRepo,
    game_limit_service: GameLimitService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/flip <bet> орёл|решка`` — coin stake (L-17, legacy bot.py:17462).

    Group-only. The coin uses :func:`_flip_side` — the same RNG and
    threshold as the vanity path and legacy ``FlipGame.play``
    (bot.py:14694) — so the free and stake flips can't drift. Payout on
    a correct call is ``bet × FLIP_MULTIPLIER`` gross — 1.9, NOT
    legacy's 2 (``bot.py:2578``), for the same house-edge reason
    ``/roll`` deviates; see ``services/stake_games_service.py:17-22``.
    Single result message (legacy bet path had no animation,
    bot.py:17479-17488).
    """
    tg_user = require_from_user(message)
    if message.chat.type not in GROUP_TYPE_NAMES:
        await message.answer(t("h_stake_group_only", lang))
        return

    parts = command_body(message).split()
    bet = int(parts[1])  # filter-guaranteed digit token
    guess = _normalize_flip_guess(" ".join(parts[2:]))
    if guess is None:
        await message.answer(t("h_flip_bet_invalid_side", lang))
        return

    now = datetime.now()  # noqa: DTZ005 — naive local, matches game_plays
    async with _stake_locks.acquire(tg_user.id):
        gates = await _stake_gates(
            message,
            user_id=tg_user.id,
            bet=bet,
            min_bet=FLIP_MIN_BET,
            max_bet=FLIP_MAX_BET,
            economy_repo=economy_repo,
            transactions_repo=transactions_repo,
            game_limit_service=game_limit_service,
            now=now,
            lang=lang,
        )
        if gates is None:
            return
        service, abuse = gates

        side = _flip_side()
        result = await service.settle(
            user_id=tg_user.id,
            bet=bet,
            game="flip",
            won=side == guess,
            multiplier=FLIP_MULTIPLIER,
            detail={"side": side, "guess": guess, "multiplier": FLIP_MULTIPLIER},
        )
        if result.outcome is not StakeOutcome.SUCCESS:
            # Same lost-race release as ``/roll`` above, for the same
            # reason (#1967).
            if checkpoint is not None:
                await checkpoint()
            await _reply_stake_rejection(
                message, result, min_bet=FLIP_MIN_BET, max_bet=FLIP_MAX_BET, lang=lang
            )
            return
        await game_limit_service.record(tg_user.id, game="flip", now=now)
        # Same commit-inside-the-lock as ``/roll`` above, for the same
        # reason and with the same safety argument (#222-B).
        if checkpoint is not None:
            await checkpoint()

    key = "h_flip_bet_win" if result.won else "h_flip_bet_lose"
    body = t(
        key,
        lang,
        side=_flip_side_label(side, lang),
        guess=_flip_side_label(guess, lang),
        payout=result.payout,
        bet=bet,
    )
    assert result.balance is not None  # SUCCESS always carries it
    body += t("h_roulette_balance", lang, balance=result.balance)
    body += render_achievements(lang, result.awarded)
    body += render_allowance(lang, abuse)
    await post_game_card(message, body)
    log.bind(uid=tg_user.id, bet=bet, guess=guess, side=side, won=result.won).info(
        "/flip stake played"
    )


def _dice_flavor(value: int, lang: str) -> str:
    """One-line reaction to a bare roll (RR-3 #31).

    Bare ``/dice`` has no guess, so there is no win/loss to report —
    legacy's framing came from its stake form. We give the roll a verdict
    anyway (crit / good / meh / worst) so the echo reads like a game
    rather than a number. Out-of-range values (the defensive ``0`` below)
    get no line at all.
    """
    if not (1 <= value <= 6):
        return ""
    if value == 6:
        key = "h_dice_flavor_crit"
    elif value >= 4:
        key = "h_dice_flavor_good"
    elif value >= 2:
        key = "h_dice_flavor_meh"
    else:
        key = "h_dice_flavor_worst"
    return "\n" + t(key, lang)


def _dice_hints(chat_type: str, lang: str) -> str:
    """How to turn the vanity roll into a real game (RR-3 #31).

    The guess form works everywhere; the stake form is group-only (same
    gate as ``/roll <bet>``), so its line is rendered only in groups —
    advertising a command that would answer "group only" is worse than
    silence.
    """
    hint = "\n\n" + t("h_dice_hint_guess", lang)
    if chat_type in GROUP_TYPE_NAMES:
        hint += "\n" + t(
            "h_dice_hint_stake",
            lang,
            min=format_number(DICE_MIN_BET),
            max=format_number(DICE_MAX_BET),
            mult=DICE_MULTIPLIER,
        )
    return hint


async def handle_dice(message: Message, lang: str) -> None:
    """Roll Telegram's native 🎲 emoji and echo the value as text.

    The dice is rolled by Telegram (``answer_dice`` returns a Message
    whose ``.dice.value`` is 1..6, drawn server-side). That's strictly
    fairer than legacy's local ``random.randint(1, 6)`` — both because
    server-side is harder to influence and because the user sees the
    animation. The value line keeps legacy's exact wording so
    parity-checkers see no diff; RR-3 #31 adds a verdict for the roll and
    the two ways to play it for real.
    """
    sent = await message.answer_dice(emoji="🎲")
    value = sent.dice.value if sent.dice is not None else 0
    body = (
        t("h_dice_result", lang, value=value)
        + _dice_flavor(value, lang)
        + _dice_hints(message.chat.type, lang)
    )
    await post_game_card(message, body)
    log.bind(
        uid=message.from_user.id if message.from_user else None,
        value=value,
    ).info("/dice rolled")


async def handle_roll_guess(message: Message, lang: str) -> None:
    """Vanity guess roll: ``/roll 4`` or ``/кубик 4``.

    Filter guarantees ``args[0]`` is a 1-digit int in 1..6. Telegram's
    server-side dice is the source of truth — same fairness argument
    as :func:`handle_dice`. The reply mirrors legacy at
    ``bot.py:17424-17433`` modulo Markdown→HTML: legacy uses
    ``**bold**``, we use ``<b>bold</b>`` because every outgoing reply
    in the new pipeline is HTML-mode.
    """
    parts = command_body(message).split()
    guess = int(parts[1])
    sent = await message.answer_dice(emoji="🎲")
    roll = sent.dice.value if sent.dice is not None else 0
    if not (1 <= roll <= 6):
        # Defensive: ``Dice.value`` is documented 1..6, but if Telegram
        # ever returns something off-range we fall back to the same
        # local source legacy uses (bot.py:17422-17423). Keeps the user
        # from ever seeing a "выпало 0" bug.
        roll = money_rng.randint(1, 6)
    key = "h_roll_guess_win" if roll == guess else "h_roll_guess_lose"
    body = t(key, lang, guess=guess, roll=roll)
    await post_game_card(message, body)
    log.bind(
        uid=message.from_user.id if message.from_user else None,
        guess=guess,
        roll=roll,
        won=(roll == guess),
    ).info("/roll vanity guess")


async def handle_flip(message: Message, lang: str) -> None:
    """Bare ``/flip`` / ``/монетка`` / ``/kom_flip`` — animated coin toss.

    The no-guess sibling of :func:`handle_dice`. Legacy rendered an
    inline keyboard here (``bot.py:17504-17510``); since legacy is dead
    in prod a bare toss must produce a visible result. We send the 🪙
    "throw", pause, then edit it into the settled side so the user sees
    one spin→result transition instead of falling through silently.
    """
    thrown = await message.answer(t("h_flip_throw", lang))
    await asyncio.sleep(_FLIP_REVEAL_DELAY)
    side = _flip_side()
    # ``edit_card`` because the reveal is a second round-trip a whole
    # ``_FLIP_REVEAL_DELAY`` after the throw: the user (or a group's
    # cleaner bot) can delete the 🪙 message in between, and Telegram
    # then answers "message to edit not found". Raw, that reaches the
    # global error router and reports a failure for a toss nobody is
    # waiting on any more.
    await edit_card(thrown, t("h_flip_result", lang, side=_flip_side_label(side, lang)))
    schedule_card_sweep(thrown, chat_type=message.chat.type)
    log.bind(
        uid=message.from_user.id if message.from_user else None,
        side=side,
    ).info("/flip toss")


async def handle_flip_guess(message: Message, lang: str) -> None:
    """Vanity coin flip: ``/flip орёл`` or ``/монетка решка``.

    Filter guarantees the arg normalises to ``орёл``/``решка``. Same
    throw→reveal animation as :func:`handle_flip`, with a win/lose
    verdict appended. The coin uses :func:`_flip_side` — same RNG and
    threshold as legacy at ``bot.py:17497``.
    """
    parts = command_body(message).split()
    guess = _normalize_flip_guess(parts[1])
    # Filter guarantees this; assert documents the invariant.
    assert guess is not None
    thrown = await message.answer(t("h_flip_throw", lang))
    await asyncio.sleep(_FLIP_REVEAL_DELAY)
    side = _flip_side()
    won = side == guess
    verdict = t("h_flip_verdict_win" if won else "h_flip_verdict_lose", lang)
    # Same delayed-reveal race as the bare toss above.
    await edit_card(
        thrown,
        t(
            "h_flip_guess_result",
            lang,
            side=_flip_side_label(side, lang),
            guess=_flip_side_label(guess, lang),
            verdict=verdict,
        ),
    )
    schedule_card_sweep(thrown, chat_type=message.chat.type)
    log.bind(
        uid=message.from_user.id if message.from_user else None,
        guess=guess,
        side=side,
        won=won,
    ).info("/flip vanity guess")


def build_router(registry: EngineRegistry | None = None) -> Router:
    """Factory — fresh ``Router`` per call so tests can re-wire dispatchers.

    The vanity handlers are stateless and read no DB, so the parent
    router carries NO middleware. The L-17 stake handlers live on a
    CHILD router with their own :class:`EconomyMiddleware` (the pattern
    the old docstring promised): aiogram checks the parent's own
    handlers first, their tight filters reject the two-token stake
    forms, and the event propagates into the child — so the free games
    never pay for an economy session.

    ``registry`` is optional so a caller can mount the free games on
    their own — tests that only exercise ``/flip`` do exactly that.
    Without a registry the stake child is simply not mounted and the
    stake forms keep falling through, which is the pre-L-17 behaviour;
    ``main_router`` always passes a registry, so in the running bot the
    stake flows are live.
    """
    router = Router(name="games")
    # ``roll`` belongs in this list for the same reason bare ``/flip``
    # stopped falling through (see :func:`handle_flip`): legacy is dead
    # in prod, so "legacy owns the bare form" is no longer a fallback,
    # it is silence. Bare ``/roll`` matched nothing at all — not this
    # handler (the alias was missing), not ``_is_vanity_roll`` (needs
    # exactly one arg), not ``_is_stake_roll`` (needs two) — while
    # ``h_games_menu_card`` advertised it as "/roll [число] — брось
    # кубик" with the number optional. Now it rolls, and the hints under
    # the result spell out the guess and stake forms the legacy usage
    # text used to.
    router.message.register(
        handle_dice,
        Command("dice", "кубик", "roll", ignore_case=True, magic=F.args.is_(None)),
    )
    # ``/roll`` and ``/кубик <n>`` share a handler. Each alias is also
    # the no-args form above — aiogram matches the first router whose
    # filter accepts the event, and the magic guards on both
    # registrations are disjoint, so there's no ambiguity:
    #   /кубик       → handle_dice  (args is None)
    #   /кубик 4     → handle_roll_guess (args == "4")
    #   /кубик 100 4 → the stake child below
    #
    # RR-3 #31: ``/dice`` carries the same grammar. Legacy's ``/dice``
    # WAS the stake command (``/dice <bet> <guess>``, bot.py:21129) while
    # the port left it a bare echo, so ``/dice 100 4`` matched nothing at
    # all. Aliasing it onto the guess + stake handlers restores the
    # legacy grammar without moving the bare form off its animation.
    router.message.register(
        handle_roll_guess,
        Command("roll", "кубик", "dice", "dice_roll", ignore_case=True),
        _is_vanity_roll,
    )
    # Bare ``/flip`` / ``/монетка`` / ``/kom_flip`` (no args) → animated
    # toss. Registered before the guess handler; its ``magic`` guard is
    # disjoint from ``_is_vanity_flip`` (zero args vs exactly one), so
    # the two never compete:
    #   /flip        → handle_flip       (args is None)
    #   /flip орёл   → handle_flip_guess (one normalisable arg)
    #   /flip 100 ор → falls through     (filter rejects)
    router.message.register(
        handle_flip,
        Command(
            "flip",
            "монетка",
            "kom_flip",
            "coin",
            "coinflip",
            ignore_case=True,
            magic=F.args.is_(None),
        ),
    )
    router.message.register(
        handle_flip_guess,
        Command("flip", "монетка", "kom_flip", "coin", "coinflip", ignore_case=True),
        _is_vanity_flip,
    )
    if registry is not None:
        # L-17 stake child. Routing is disjoint by construction: the
        # vanity filters above accept ZERO or ONE argument forms only,
        # the stake filters require a digit bet + a second token, so no
        # update can match both. EconomyMiddleware injects
        # ``economy_repo`` (atomic debit / checked credit / A-11 games
        # write) and ``game_limit_service`` (persistent anti-abuse caps)
        # on ONE shared session — wallet, games row and the game_plays
        # stamp commit together, /roulette parity.
        stakes = Router(name="games_stakes")
        stakes.message.middleware(EconomyMiddleware(registry))
        stakes.message.register(
            handle_roll_bet,
            Command("roll", "кубик", "dice", "dice_roll", ignore_case=True),
            F.from_user,
            _is_stake_roll,
        )
        stakes.message.register(
            handle_flip_bet,
            Command("flip", "монетка", "kom_flip", "coin", "coinflip", ignore_case=True),
            F.from_user,
            _is_stake_flip,
        )
        router.include_router(stakes)
    return router
