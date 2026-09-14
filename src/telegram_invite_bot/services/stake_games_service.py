"""Stake-based ``/roll`` and ``/flip`` settlement (L-17).

The wallet flow for the two legacy single-player bet games:

* **Dice stake** — legacy ``DiceGame.play`` (bot.py:14605): debit the
  stake, roll, and on a correct 1-6 guess credit ``bet × 5.7`` gross
  (``DICE_MULTIPLIER``; legacy's ``dice_multiplier`` was 6, bot.py:2575
  → see the house-edge note on the constant). Net win = ``+4.7×bet``;
  net loss = ``−bet``. Limits 10–10 000 (``dice_min_bet`` /
  ``dice_max_bet``, bot.py:2573-2574).
* **Coin stake** — legacy ``FlipGame.play`` (bot.py:14687): debit the
  stake, flip (``random.random() < 0.5`` → орёл, bot.py:14694), and on
  a correct орёл/решка call credit ``bet × 1.9`` gross
  (``FLIP_MULTIPLIER``; legacy's was 2, bot.py:2578). Net win =
  ``+0.9×bet``; net loss = ``−bet``. Limits 10–10 000 (bot.py:2576-2577).

Both multipliers deliberately break legacy parity, and it is the one
place in this module where that is the point: at 6× and 2× the two
games returned exactly what they took, so a bot with time farmed them
for free and a lucky player drained the owner's float with no
counterweight. Every payout here is a coin the owner has to honour at
/withdraw, so the games carry the same ~5% edge /roulette has.

#1566 — DO NOT "restore parity" by putting the legacy constants back.
``DICE_MULTIPLIER`` is 5.7, not bot.py's 6, and ``FLIP_MULTIPLIER`` is
1.9, not bot.py's 2, on the owner's explicit instruction: the house
margin across the whole ecosystem — exchanges, markets and games — is
his, and every one of these numbers is load-bearing for it. A diff
that rounds them back to the legacy values looks like a faithfulness
fix and is a revenue change. The same guard applies to
``games/pot.py``'s ``PVP_PAYOUT_MULTIPLIER``.

Shape copied from :class:`~telegram_invite_bot.services.roulette_service.
RouletteService` — the closest sibling (single player, one stake, one
conditional payout):

1. :meth:`StakeGamesService.validate` — cheap read-only pre-check
   (bounds, wallet existence, affordability) the handler runs BEFORE
   the Telegram dice animation, mirroring legacy ``validate_bet``
   running before ``send_dice`` (bot.py:17356).
2. :meth:`StakeGamesService.settle` — the money flow: **atomic stake
   debit first** (``EconomyRepo.debit`` with its ``WHERE balance >=
   amount`` guard, closing the TOCTOU the pre-check cannot), then on a
   win a **checked** gross-payout credit, then
   :meth:`EconomyRepo.record_game` (A-11 write-side: one ``games`` row
   + ``games_played``/``games_won`` bump + achievement awarding) on
   the SAME session so stats and wallet commit atomically.

The game OUTCOME (the roll value / coin side) is computed by the
caller, not here: ``/roll`` takes the roll from Telegram's server-side
dice animation (legacy parity, bot.py:17362-17366) and ``/flip`` draws
via :func:`telegram_invite_bot.handlers.games._flip_side` (the single
RNG source the vanity path already uses). Settlement only needs
``won`` — keeping the RNG out of this module means the free and stake
paths of each game cannot drift apart.

DEFERRED (mirrors the /roulette port's altitude):

* No group-treasury / commission split — legacy's stake games had
  none either (``DiceGame.play``/``FlipGame.play`` touch only the
  player's wallet + ``games``).
* No ``AUTO_DELETE_GAMES`` message-TTL scheduling (chat-noise only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.games.limits import MAX_BET as _MAX_BET
from telegram_invite_bot.games.limits import MIN_BET as _MIN_BET

_log = logger.bind(component="services.stake_games")

if TYPE_CHECKING:
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo


# Legacy settings defaults (bot.py:2573-2578). Same min/max for both
# games in legacy's shipped config; kept as separate constants because
# they were separate settings knobs (an operator could diverge them).
#
# T-020/R9: the values now come from the shared ecosystem ceiling. The
# four names stay so handlers and tests keep their per-game vocabulary,
# but there is one number behind them — diverging two games' ceilings
# is a decision that should be made in :mod:`~telegram_invite_bot.games.limits`,
# not by editing one literal here and forgetting the other five.
DICE_MIN_BET = _MIN_BET
DICE_MAX_BET = _MAX_BET
# Gross payout multiplier (T-020/R7). Legacy paid bet×6 on a 1-in-6
# guess — expected return 6/6 = 1.0, a ZERO-edge game that a patient
# player farms for free and a lucky one drains. 5.7 puts the expected
# return at 5.7/6 = 0.95, the same ~5% edge /roulette has carried since
# A-10 (MULTIPLIER = 1.89 on a 50% chance).
DICE_MULTIPLIER = 5.7
FLIP_MIN_BET = _MIN_BET
FLIP_MAX_BET = _MAX_BET
# Same story on the 50/50 coin: legacy's bet×2 was zero-edge
# (bot.py:2578). 1.9 → expected return 1.9/2 = 0.95.
FLIP_MULTIPLIER = 1.9


class StakeOutcome(StrEnum):
    """Mutually-exclusive verdicts the stake handlers branch on.

    Same taxonomy as :class:`RouletteOutcome` — one ``SUCCESS`` (the
    play settled; win OR loss rides on the result), the rest are
    rejections with zero economic side effects.
    """

    SUCCESS = "success"
    BELOW_MIN_BET = "below_min_bet"
    ABOVE_MAX_BET = "above_max_bet"
    NO_WALLET = "no_wallet"
    INSUFFICIENT_FUNDS = "insufficient_funds"


@dataclass(frozen=True, slots=True)
class StakeResult:
    """What :meth:`validate` / :meth:`settle` produced.

    On ``SUCCESS`` the ``balance`` is the wallet-write return value
    (not a re-read) so the receipt is race-faithful — same posture as
    :class:`RouletteResult`. ``balance`` is also populated on the
    ``INSUFFICIENT_FUNDS`` rejection so the handler's refusal can show
    the current balance (legacy parity, bot.py:14598/14681-14683).
    """

    outcome: StakeOutcome
    won: bool = False
    bet: int = 0
    # Gross payout credited on a win (int(round(bet × multiplier)));
    # 0 on a loss. Always a whole coin — balances are integers.
    payout: int = 0
    balance: int | None = None
    # A-12: achievement ids newly unlocked by this play (SUCCESS only).
    awarded: list[str] = field(default_factory=list)


class StakeGamesService:
    """Single-player stake settlement over ``economy.users.balance``.

    Pure-ish: the only side effects are the economy writes (stake
    debit, conditional payout credit, the A-11 games-row record) and
    their ledger rows. The game outcome is decided by the caller and
    passed in as ``won``.
    """

    def __init__(
        self,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
    ) -> None:
        self._economy = economy_repo
        self._ledger = transactions_repo

    async def validate(
        self,
        *,
        user_id: int,
        bet: int,
        min_bet: int,
        max_bet: int,
    ) -> StakeResult | None:
        """Read-only pre-check: bounds → wallet existence → affordability.

        Returns a rejection :class:`StakeResult`, or ``None`` when the
        play may proceed. Mirrors legacy ``validate_bet`` (bot.py:14577/
        14676) which ran before the dice animation — so an invalid bet
        never triggers a ``send_dice`` the result can't honour. The
        affordability answer here is advisory; :meth:`settle` re-checks
        atomically at debit time.
        """
        if bet < min_bet:
            return StakeResult(outcome=StakeOutcome.BELOW_MIN_BET, bet=bet)
        if bet > max_bet:
            return StakeResult(outcome=StakeOutcome.ABOVE_MAX_BET, bet=bet)
        wallet = await self._economy.get(user_id)
        if wallet is None:
            return StakeResult(outcome=StakeOutcome.NO_WALLET, bet=bet)
        if wallet.balance < bet:
            return StakeResult(
                outcome=StakeOutcome.INSUFFICIENT_FUNDS,
                bet=bet,
                balance=wallet.balance,
            )
        return None

    async def settle(
        self,
        *,
        user_id: int,
        bet: int,
        game: str,
        won: bool,
        multiplier: float,
        detail: dict[str, float | str],
    ) -> StakeResult:
        """Atomic stake debit → (win) checked payout credit → games row.

        Step order matches legacy ``DiceGame.play`` / ``FlipGame.play``
        (debit via ``remove_coins`` first, payout via ``add_coins`` only
        on a win) and the /roulette service. ``detail`` is stored
        verbatim as the ``games.result`` JSON blob (legacy ``details``).
        """
        # Atomic stake debit. The ``WHERE balance >= amount`` guard in
        # ``debit`` is the real affordability check — a concurrent spend
        # that drained the wallet after ``validate`` collapses this to
        # ``None`` and we refuse without minting.
        debited = await self._economy.debit(user_id, bet)
        if debited is None:
            fresh = await self._economy.get(user_id)
            return StakeResult(
                outcome=StakeOutcome.INSUFFICIENT_FUNDS,
                bet=bet,
                balance=fresh.balance if fresh is not None else None,
            )
        balance = debited.balance

        # #225: book the stake. This service used to move coins with no
        # ledger row at all, so /roll and /flip were invisible to
        # ``/balance``'s weekly cashflow and to any supply audit —
        # legacy wrote both rows (bot.py:14623/14635 for dice,
        # :14692/14698 for the coin flip). ``game`` is already the
        # ledger-friendly short name the ``games`` table stores
        # ("dice" / "flip"), so the row type follows /duel's
        # ``<game>_stake`` / ``<game>_win`` shape without a second
        # naming table to keep in sync.
        await self._ledger.record(
            from_id=user_id,
            to_id=None,
            amount=bet,
            reason=f"{game} stake",
            type=f"{game}_stake",
        )

        # On a win, credit the GROSS payout (legacy ``add_coins(user_id,
        # bet * MULTIPLIER)``, bot.py:14635/14698) — the stake was
        # already taken, so net P&L is bet×(multiplier−1). Rounded to a
        # whole coin the same way /roulette rounds its 1.89, so the two
        # casino services cannot drift on the tie case.
        payout = 0
        if won:
            payout = int(round(bet * multiplier))
            credited = await self._economy.credit(user_id, payout)
            if credited is None:
                # #1560: this used to log and carry on. ``credit``
                # returns None only on the (improbable) balance-cap
                # overflow, or if the wallet row went away mid-play —
                # either way the payout did NOT land, and everything
                # below went on reporting a win regardless: SUCCESS with
                # ``won=True`` and the full ``payout``, which the card
                # renders as "you won N" directly above a balance one
                # stake LOWER than before the game, plus a ``games`` row
                # booking ``profit = payout - bet`` the wallet never
                # received — skewing /duel_stats and every leaderboard
                # that aggregates ``games.profit``, permanently.
                #
                # Raising is the posture ``PvpService`` already took on
                # this same edge, so the three casino services now agree.
                # ``settle`` runs inside one transaction the caller
                # commits, so the exception rolls the stake debit back
                # with it: the player keeps their bet, no ``games`` row
                # and no ledger row are written, and ops get the amounts
                # from the line below. A ``PAYOUT_REJECTED`` outcome was
                # the alternative, but it would still have to hand the
                # stake back by itself — which is what the transaction
                # already does for free.
                msg = f"{game} payout credit failed (balance cap?)"
                _log.bind(uid=user_id, game=game, bet=bet, amount=payout).error(msg)
                raise RuntimeError(msg)
            balance = credited.balance
            # Below the raise on purpose: booking a payout the wallet
            # never received is exactly the desync the ledger exists to
            # prevent.
            await self._ledger.record(
                from_id=None,
                to_id=user_id,
                amount=payout,
                reason=f"{game} win",
                type=f"{game}_win",
            )

        # A-11 write-side: one ``games`` row + games_played/won bump +
        # achievement awarding, on the SAME session as the debit/credit
        # so stats commit atomically with the wallet. ``profit`` is the
        # signed net P&L (legacy convention: +bet×(mult−1) / −bet).
        profit = payout - bet if won else -bet
        awarded = await self._economy.record_game(
            user_id,
            game=game,
            bet=bet,
            won=won,
            profit=profit,
            result=json.dumps(detail, ensure_ascii=False),
        )

        return StakeResult(
            outcome=StakeOutcome.SUCCESS,
            won=won,
            bet=bet,
            payout=payout,
            balance=balance,
            awarded=awarded,
        )
