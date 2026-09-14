"""``/roulette`` service — RUSSIAN ROULETTE spin + atomic settlement (A-10).

Single-player casino game over ``economy.users.balance``. NOT the casino
red/black wheel — it is Russian roulette: a 6-chamber spin where shots
1/2/3 LOSE and 4/5/6 WIN (exactly 50%). The headline economic shape
(pinned by tests) mirrors legacy ``RussianRoulette.play`` (bot.py:14503):

1. Validate cheaply — min/max bounds, then wallet existence/affordability.
2. **Debit the bet first, atomically** (``EconomyRepo.debit`` ``WHERE
   balance >= amount``) so a wallet drained between the read and the
   write physically cannot over-spend.
3. Spin: ``shot = rng.randint(1, 6)``; win iff ``shot not in
   LOSE_SHOTS``.
4. On win, credit ``int(round(bet * MULTIPLIER))`` (gross payout —
   net win ≈ +0.89×bet at MULTIPLIER=1.89). On loss, nothing further
   (the debit already took the stake). Net loss = −bet.

House edge: each play has a 50% win chance paying 1.89×, so expected
return per coin is ``0.5 * 1.89 = 0.945`` — a ~5.5% edge, i.e. NOT
farmable. That edge is the whole reason the multiplier is 1.89 and not
2.0.

RNG is injected (``random.Random``) so tests pin a deterministic shot —
same ``_rng`` pattern as :mod:`telegram_invite_bot.services.duel_service`
/ :mod:`telegram_invite_bot.handlers.duel`.

A completed spin records a ``games`` row + bumps ``games_played`` /
``games_won`` via :meth:`EconomyRepo.record_game` (A-11), on the same
session as the settlement so the stats commit atomically with the wallet.

The anti-abuse caps are NOT enforced here. They live in the persistent
:class:`~telegram_invite_bot.services.game_limit_service.GameLimitService`
(L-25), which the /roulette handler consults via DI; this module only
re-exports the cap *constants* (:data:`COOLDOWN_SEC` / :data:`MAX_PER_HOUR`
/ :data:`MAX_PER_DAY`) so the policy layer and the handler's localised
messages share one source of truth.

Since A-11/A-12 :meth:`RouletteService.play` also writes the ``games``
row (with the signed profit) and grants achievements via
``EconomyRepo.record_game``, returning the freshly-awarded ids so the
handler can render them — legacy's ``check_game_achievements``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.games.limits import MAX_BET as _MAX_BET
from telegram_invite_bot.games.limits import MIN_BET as _MIN_BET

_log = logger.bind(component="services.roulette")

if TYPE_CHECKING:
    import random

    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo


# Module constants — match legacy ``ROULETTE_*`` settings (bot.py:3767).
# The bet bounds come from the shared ecosystem ceiling (T-020/R9)
# rather than a local copy; re-exported under these names because
# handlers and tests import them from here.
MIN_BET = _MIN_BET
MAX_BET = _MAX_BET
MULTIPLIER = 1.89
# Shots 1/2/3 lose, 4/5/6 win — exactly 50% (legacy
# ``RussianRoulette.LOSE_SHOTS = (1, 2, 3)``, bot.py:14419).
LOSE_SHOTS: frozenset[int] = frozenset({1, 2, 3})
DICE_FACES = 6

# Anti-abuse caps — match legacy ``roulette_*`` settings (bot.py:2570).
# NOTE on the dev-exempt divergence: legacy exempted ``DEVELOPER_IDS``
# from these caps. The new games pipeline has no dev-exempt list wired
# in, so the caps here apply to EVERYONE (documented divergence — a
# later task can thread a dev allow-list through if wanted).
#
# These constants are the single source of truth: the persistent
# :class:`~telegram_invite_bot.services.game_limit_service.GameLimitService`
# (L-25) re-imports them so the cap policy and the handler's localised
# messages never drift.
COOLDOWN_SEC = 180
MAX_PER_HOUR = 8
MAX_PER_DAY = 25


class RouletteOutcome(StrEnum):
    """Mutually-exclusive verdicts the /roulette handler branches on.

    One ``SUCCESS`` (the spin happened, win OR loss is carried on the
    result), the rest are validation rejections that prevent any
    economic side-effect.
    """

    SUCCESS = "success"
    INVALID_BET = "invalid_bet"
    BELOW_MIN_BET = "below_min_bet"
    ABOVE_MAX_BET = "above_max_bet"
    NO_WALLET = "no_wallet"
    INSUFFICIENT_FUNDS = "insufficient_funds"


@dataclass(frozen=True, slots=True)
class RouletteResult:
    """What :meth:`RouletteService.play` produced.

    On ``SUCCESS`` the post-spin ``balance`` is the wallet-write return
    value (not a re-read) so the receipt is race-faithful — same posture
    as :class:`DuelServiceResult`. ``balance`` is also populated on the
    ``INSUFFICIENT_FUNDS`` rejection so the handler can show the current
    balance in its refusal (legacy parity, bot.py:14498).
    """

    outcome: RouletteOutcome
    won: bool = False
    shot: int = 0
    bet: int = 0
    win_amount: int = 0
    balance: int | None = None
    # A-12: achievement ids newly unlocked by this play (SUCCESS only),
    # so the handler can render the "new achievements" line. Empty default.
    awarded: list[str] = field(default_factory=list)


class RouletteService:
    """Single-player /roulette spin + atomic settlement over economy.

    Pure-ish: the only side effects are the two economy writes (debit
    the stake, credit the payout on win) and their ledger rows. RNG is
    injected so the spin is deterministic under test.
    """

    def __init__(
        self,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
    ) -> None:
        self._economy = economy_repo
        self._ledger = transactions_repo

    async def play(
        self,
        *,
        user_id: int,
        bet: int,
        rng: random.Random,
    ) -> RouletteResult:
        """Validate → debit → spin → (win) credit. One atomic stake debit.

        Step order matches legacy ``RussianRoulette.play`` (bot.py:14503)
        and the /duel service: cheap bound checks first, then the
        wallet existence/affordability gate, then the load-bearing
        atomic debit, then the spin, then the conditional payout credit.
        """
        # Step 1: cheap bound validators.
        if bet < MIN_BET:
            return RouletteResult(outcome=RouletteOutcome.BELOW_MIN_BET, bet=bet)
        if bet > MAX_BET:
            return RouletteResult(outcome=RouletteOutcome.ABOVE_MAX_BET, bet=bet)

        # Step 2: wallet existence + affordability pre-check (read-only).
        wallet = await self._economy.get(user_id)
        if wallet is None:
            return RouletteResult(outcome=RouletteOutcome.NO_WALLET, bet=bet)
        if wallet.balance < bet:
            return RouletteResult(
                outcome=RouletteOutcome.INSUFFICIENT_FUNDS,
                bet=bet,
                balance=wallet.balance,
            )

        # Step 3: debit the stake FIRST, atomically. The ``WHERE balance
        # >= amount`` guard inside ``debit`` closes the TOCTOU window the
        # affordability pre-check above cannot — a concurrent debit that
        # drained the wallet between the read and here collapses to a
        # ``None`` return and we refuse without minting.
        debited = await self._economy.debit(user_id, bet)
        if debited is None:
            # Race lost — re-read for the freshest balance to surface.
            fresh = await self._economy.get(user_id)
            return RouletteResult(
                outcome=RouletteOutcome.INSUFFICIENT_FUNDS,
                bet=bet,
                balance=fresh.balance if fresh is not None else None,
            )

        # #225: the ledger row is the companion to the wallet write, and
        # this service used to skip it entirely — /roulette moved coins
        # that ``/balance``'s weekly cashflow and the finances panel in
        # ``/profile`` could not see, and that no supply reconciliation
        # could account for. Legacy wrote both rows via
        # ``remove_coins``/``add_coins`` (bot.py:14520 and :14530).
        # Shape follows /duel: the stake names no counterparty because
        # the house is not a wallet.
        await self._ledger.record(
            from_id=user_id,
            to_id=None,
            amount=bet,
            reason="roulette stake",
            type="roulette_stake",
        )

        # Step 4: spin. 6 chambers; shots 1/2/3 lose, 4/5/6 win (50%).
        shot = rng.randint(1, DICE_FACES)
        won = shot not in LOSE_SHOTS
        balance = debited.balance

        # Step 5: on win, credit the gross payout (int(round(bet*1.89))).
        win_amount = 0
        if won:
            win_amount = int(round(bet * MULTIPLIER))
            credited = await self._economy.credit(user_id, win_amount)
            if credited is None:
                # #1560: this used to log and carry on, which left the
                # card announcing a win the wallet never received and
                # booked a ``games`` row with the unpaid profit in it.
                # Raising instead lets the caller's transaction roll the
                # stake debit back too, so the player keeps their bet and
                # nothing is recorded. Same edge, same posture, same
                # reasoning as ``StakeGamesService.settle`` — which
                # carries the long version of this note — and as
                # ``PvpService``.
                msg = "roulette payout credit failed (balance cap?)"
                _log.bind(uid=user_id, bet=bet, amount=win_amount).error(msg)
                raise RuntimeError(msg)
            balance = credited.balance
            # Below the raise on purpose: the row would otherwise book a
            # payout the wallet never received.
            await self._ledger.record(
                from_id=None,
                to_id=user_id,
                amount=win_amount,
                reason="roulette win",
                type="roulette_win",
            )

        # Step 6 (A-11): record the completed play — one ``games`` row +
        # games_played/won bump, on the SAME session as the debit/credit
        # above so the stats commit atomically with the wallet. ``profit``
        # is signed net P&L: +(payout − stake) on win, −stake on loss.
        profit = win_amount - bet if won else -bet
        awarded = await self._economy.record_game(
            user_id,
            game="roulette",
            bet=bet,
            won=won,
            profit=profit,
            result=json.dumps({"shot": shot, "multiplier": MULTIPLIER}),
        )

        return RouletteResult(
            outcome=RouletteOutcome.SUCCESS,
            won=won,
            shot=shot,
            bet=bet,
            win_amount=win_amount,
            balance=balance,
            awarded=awarded,
        )
