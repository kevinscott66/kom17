"""``/duel`` service — atomic escrow + payout over EconomyRepo (T-018).

Twin of :class:`telegram_invite_bot.services.rps_service.RpsService`.
The contract is identical: validate cheaply, HOLD both stakes
atomically (with a compensating release on second-hold failure),
resolve, payout, write the ledger rows. The headline value over
legacy ``Duel.finish`` (bot.py:15243-15252) is the SAME no-mint
guarantee — legacy calls ``transfer_coins(loser, winner, bet*2,
...)`` without a separate debit step, so a wallet drained between
``finish()`` and the underlying ``remove_coins`` would silently mint.
The new pipeline escrows both seats up front; if either fails, the
other is released and the typed outcome surfaces.

#1559: the escrow legs are ``EconomyRepo.hold``/``release``, which
move the balance column and nothing else, so a rolled-back escrow or
a tie leaves ``total_spent``/``total_earned`` untouched. Only a
decided round settles the two holds as a genuine spend.

ADR 0009 (no-escrow at challenge creation): nothing in this service
runs at /duel challenge time. ``play`` is called only after both
sides have committed rolls — the e2e tests pin that challenger
balance is untouched until both rolls land.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn

from loguru import logger

from telegram_invite_bot.games.duel import (
    DuelConfig,
    DuelOutcome,
    DuelRoundResult,
    resolve_round,
)

if TYPE_CHECKING:
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo

_log = logger.bind(component="services.duel")


def _fail(msg: str, **fields: object) -> NoReturn:
    """Log a broken money-path invariant, then raise (#263).

    The three payout sites below used to be bare ``assert``s. The
    deployed systemd unit runs without ``-O``, so those asserts were
    live
    crashes rather than no-ops — but they crashed mute, with nothing
    written down about whose coins were in flight, and the caller's
    rollback then erased the evidence.

    Raising stays right. ``play`` runs inside one transaction the
    CALLER commits, and both stakes were held a few lines above;
    only an exception undoes those two holds. Returning an outcome
    here would commit the escrow and skip the payout — the exact
    shape of "the pot vanished". What changes is that the breach is
    now on record before the unwind.

    ``NoReturn`` is load-bearing, not decoration: it is what lets the
    type checker treat the guarded local as non-``None`` on the line
    after the call.
    """
    _log.bind(**fields).error(msg)
    raise RuntimeError(msg)


class DuelServiceOutcome(StrEnum):
    """Mutually-exclusive verdicts a /duel handler branches on.

    Mirrors :class:`RpsServiceOutcome` — three SUCCESS_* variants for
    the resolved round, the rest are validation rejections that
    prevent any economic side-effect.
    """

    SUCCESS_CHALLENGER_WIN = "success_challenger_win"
    SUCCESS_OPPONENT_WIN = "success_opponent_win"
    SUCCESS_TIE = "success_tie"

    INVALID_BET = "invalid_bet"
    NON_POSITIVE_BET = "non_positive_bet"
    SAME_PLAYER = "same_player"

    CHALLENGER_INSUFFICIENT_FUNDS = "challenger_insufficient_funds"
    OPPONENT_INSUFFICIENT_FUNDS = "opponent_insufficient_funds"

    NO_CHALLENGER_WALLET = "no_challenger_wallet"
    NO_OPPONENT_WALLET = "no_opponent_wallet"


@dataclass(frozen=True, slots=True)
class DuelServiceResult:
    """What :meth:`DuelService.play` produced.

    On any SUCCESS_* outcome the per-seat post-balances come from the
    wallet-write return values (not a re-read) so the receipt is
    race-faithful — same posture as :class:`RpsServiceResult`.
    """

    outcome: DuelServiceOutcome
    round_result: DuelRoundResult | None = None
    challenger_balance: int | None = None
    opponent_balance: int | None = None


class DuelService:
    """Atomic /duel round resolution over the shared economy session."""

    def __init__(
        self,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        *,
        config: DuelConfig | None = None,
    ) -> None:
        self._economy = economy_repo
        self._ledger = transactions_repo
        self._config = config or DuelConfig()

    async def play(
        self,
        *,
        challenger_id: int,
        opponent_id: int,
        challenger_roll: int,
        opponent_roll: int,
        bet: int,
    ) -> DuelServiceResult:
        """Resolve one duel round with atomic escrow + payout.

        Step order matches :meth:`RpsService.play` (pinned by tests):

        1. Cheap validators — same_player, non-positive, out-of-bounds.
        2. Wallet existence — challenger first, then opponent.
        3. Python-level affordability pre-check.
        4. Atomic escrow (``hold``) with a compensating ``release``
           on second-hold failure (the no-mint guarantee).
        5. Resolve via pure :func:`resolve_round`.
        6. Payout — credit winner / release both on tie, then settle
           both holds as a spend on a decided round (#1559).
        7. Ledger rows — one per wallet move: both ``duel_stake``
           debits, then ``duel_win`` + ``duel_rake`` on a decided
           round or two ``duel_refund`` credits on a tie.
        """
        cfg = self._config

        # Step 1: cheap validators.
        if challenger_id == opponent_id:
            return DuelServiceResult(outcome=DuelServiceOutcome.SAME_PLAYER)
        if bet <= 0:
            return DuelServiceResult(outcome=DuelServiceOutcome.NON_POSITIVE_BET)
        if bet < cfg.min_bet or bet > cfg.max_bet:
            return DuelServiceResult(outcome=DuelServiceOutcome.INVALID_BET)

        # Step 2: wallet existence.
        challenger = await self._economy.get(challenger_id)
        if challenger is None:
            return DuelServiceResult(outcome=DuelServiceOutcome.NO_CHALLENGER_WALLET)
        opponent = await self._economy.get(opponent_id)
        if opponent is None:
            return DuelServiceResult(outcome=DuelServiceOutcome.NO_OPPONENT_WALLET)

        # Step 3: Python-level affordability pre-check.
        if challenger.balance < bet:
            return DuelServiceResult(outcome=DuelServiceOutcome.CHALLENGER_INSUFFICIENT_FUNDS)
        if opponent.balance < bet:
            return DuelServiceResult(outcome=DuelServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS)

        # Step 4: atomic escrow with compensating rollback.
        escrowed_challenger = await self._economy.hold(challenger_id, bet)
        if escrowed_challenger is None:
            return DuelServiceResult(outcome=DuelServiceOutcome.CHALLENGER_INSUFFICIENT_FUNDS)

        escrowed_opponent = await self._economy.hold(opponent_id, bet)
        if escrowed_opponent is None:
            # Release the challenger's already-held stake — the explicit
            # rollback legacy ``Duel.finish`` does NOT do.
            #
            # #1561: the result is checked like every other mutator in
            # this file. It used to carry an inline money-guard waiver
            # reading "compensating refund", which was exactly backwards
            # — a compensation is the one write that may not fail
            # silently. ``release`` returns ``None`` on the same edge the
            # payout sites below raise for (row gone under an admin
            # ``/reset``, or the balance cap), and this path writes no
            # ledger row at all, so a swallowed ``None`` left the
            # challenger permanently short of one stake with nothing on
            # record: no log line, no ``transactions`` row, and a reply
            # telling them nothing happened.
            #
            # Raising is the right unwind, for the reason ``_fail``
            # documents: ``play`` runs inside one transaction the caller
            # commits, so the exception rolls the challenger's hold back
            # anyway — the compensation this branch exists for, done by
            # the transaction instead of by hand.
            released = await self._economy.release(challenger_id, bet)
            if released is None:
                _fail(
                    "duel challenger stake release failed",
                    challenger_id=challenger_id,
                    opponent_id=opponent_id,
                    bet=bet,
                    amount=bet,
                )
            return DuelServiceResult(outcome=DuelServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS)

        # Step 5: resolve (pure).
        round_result = resolve_round(
            challenger_roll,
            opponent_roll,
            bet=bet,
            payout_multiplier=cfg.payout_multiplier,
        )

        # Step 6: payout.
        challenger_post = escrowed_challenger.balance
        opponent_post = escrowed_opponent.balance

        if round_result.outcome is DuelOutcome.CHALLENGER_WIN:
            payout = round_result.payout
            credited = await self._economy.credit(challenger_id, payout)
            if credited is None:
                _fail(
                    "duel payout to challenger failed",
                    challenger_id=challenger_id,
                    opponent_id=opponent_id,
                    bet=bet,
                    amount=payout,
                )
            challenger_post = credited.balance
            outcome = DuelServiceOutcome.SUCCESS_CHALLENGER_WIN
        elif round_result.outcome is DuelOutcome.OPPONENT_WIN:
            payout = round_result.payout
            credited = await self._economy.credit(opponent_id, payout)
            if credited is None:
                _fail(
                    "duel payout to opponent failed",
                    challenger_id=challenger_id,
                    opponent_id=opponent_id,
                    bet=bet,
                    amount=payout,
                )
            opponent_post = credited.balance
            outcome = DuelServiceOutcome.SUCCESS_OPPONENT_WIN
        else:
            refund = round_result.payout
            refunded_c = await self._economy.release(challenger_id, refund)
            refunded_o = await self._economy.release(opponent_id, refund)
            if refunded_c is None or refunded_o is None:
                _fail(
                    "duel tie refund failed",
                    challenger_id=challenger_id,
                    opponent_id=opponent_id,
                    bet=bet,
                    amount=refund,
                )
            challenger_post = refunded_c.balance
            opponent_post = refunded_o.balance
            outcome = DuelServiceOutcome.SUCCESS_TIE

        # #1559: the two stakes were HELD, not spent. The escrow legs
        # above are hold/release so the compensating rollback and the
        # tie refund leave the lifetime counters exactly where they
        # were — a draw is not income and a rolled-back escrow is not
        # a purchase. A DECIDED round is where the pot is genuinely
        # consumed, so both seats settle their hold as a real spend
        # here; the winner's ``credit`` above already booked the
        # payout as income. Net counters for a decided round are
        # therefore identical to the old debit-at-escrow code.
        # ``bump_totals`` moves no coins, which is why it sits outside
        # the ``test_money_call_sites`` sweep; a failure is
        # bookkeeping-only, so it is logged rather than raised.
        if outcome is not DuelServiceOutcome.SUCCESS_TIE:
            for settled_id in (challenger_id, opponent_id):
                if await self._economy.bump_totals(settled_id, spent=bet) is None:
                    _log.bind(user_id=settled_id, bet=bet).error(
                        "duel stake settlement failed; lifetime counters not moved"
                    )

        # Step 6b (A-11): record one ``games`` row per player + bump
        # games_played/won, on the shared session so the stats commit
        # atomically with the payout above. Signed profit: winner
        # +(payout − stake), loser −stake, tie 0 (both refunded). Both
        # seats count a play; only the winner counts a win. ``game='duel'``
        # is the string ``/duel_stats`` filters on.
        if outcome is DuelServiceOutcome.SUCCESS_CHALLENGER_WIN:
            c_won, c_profit, o_won, o_profit = True, round_result.payout - bet, False, -bet
        elif outcome is DuelServiceOutcome.SUCCESS_OPPONENT_WIN:
            c_won, c_profit, o_won, o_profit = False, -bet, True, round_result.payout - bet
        else:  # tie — both stakes refunded, neither wins
            c_won, c_profit, o_won, o_profit = False, 0, False, 0
        await self._economy.record_game(
            challenger_id, game="duel", bet=bet, won=c_won, profit=c_profit
        )
        await self._economy.record_game(
            opponent_id, game="duel", bet=bet, won=o_won, profit=o_profit
        )

        # Step 7: ledger rows.
        await self._write_ledger(
            round_result=round_result,
            challenger_id=challenger_id,
            opponent_id=opponent_id,
        )

        return DuelServiceResult(
            outcome=outcome,
            round_result=round_result,
            challenger_balance=challenger_post,
            opponent_balance=opponent_post,
        )

    async def _write_ledger(
        self,
        *,
        round_result: DuelRoundResult,
        challenger_id: int,
        opponent_id: int,
    ) -> None:
        """Four ledger rows per round, matching the RPS/PvP pattern.

        The invariant every row here serves: **a seat's signed ledger
        rows sum to what actually left or entered its wallet.**
        ``from_id`` means "this wallet was debited this amount",
        ``to_id`` means "this wallet was credited it" — that is what
        ``TransactionsRepo.window_stats`` and the ``/profile`` finances
        panel read, and neither filters on type.

        So: two ``duel_stake`` rows (one per seat — both stakes leave
        their wallets at escrow, whatever the round then does with the
        pot), then either one ``duel_win`` row crediting the winner the
        gross payout or two ``duel_refund`` rows on a tie, plus a
        ``duel_rake`` row for the burned slice of the pot (T-020/R8).

        The payout and rake rows carry a NULL counterparty on purpose:
        the pot is not a wallet. The loser never sends the payout —
        they send their stake, which their own ``duel_stake`` row
        already books; naming them on the payout row (as this did
        before) charged them the whole pot on top of their stake and
        made the loser's weekly "spent" read ~3× the real loss.
        """
        bet = round_result.bet
        # Both stakes, always — including on a tie, where the refunds
        # below cancel them out. Booking only one side would leave the
        # refund looking like income out of nowhere.
        await self._ledger.record(
            from_id=challenger_id,
            to_id=None,
            amount=bet,
            reason=f"duel vs {opponent_id}",
            type="duel_stake",
        )
        await self._ledger.record(
            from_id=opponent_id,
            to_id=None,
            amount=bet,
            reason=f"duel vs {challenger_id}",
            type="duel_stake",
        )
        if round_result.outcome is DuelOutcome.CHALLENGER_WIN:
            await self._ledger.record(
                from_id=None,
                to_id=challenger_id,
                amount=round_result.payout,
                reason=f"duel vs {opponent_id}",
                type="duel_win",
            )
        elif round_result.outcome is DuelOutcome.OPPONENT_WIN:
            await self._ledger.record(
                from_id=None,
                to_id=opponent_id,
                amount=round_result.payout,
                reason=f"duel vs {challenger_id}",
                type="duel_win",
            )
        else:
            await self._ledger.record(
                from_id=None,
                to_id=challenger_id,
                amount=bet,
                reason=f"duel draw vs {opponent_id}",
                type="duel_refund",
            )
            await self._ledger.record(
                from_id=None,
                to_id=opponent_id,
                amount=bet,
                reason=f"duel draw vs {challenger_id}",
                type="duel_refund",
            )

        # T-020/R8 — the house cut. Gated purely on the amount rather
        # than on the outcome: a tie carries ``rake == 0`` (both stakes
        # refunded) and so writes no row, and a hypothetical zero-rake
        # config (2× multiplier restored) writes no noise row either.
        if round_result.rake > 0:
            await self._ledger.record(
                from_id=None,
                to_id=None,
                amount=round_result.rake,
                reason=f"duel rake {challenger_id} vs {opponent_id}",
                type="duel_rake",
            )
