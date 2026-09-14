"""``/cpc`` (rock-paper-scissors) — Stage 33 service over EconomyRepo.

Composes the pure :func:`telegram_invite_bot.games.rps.resolve_round`
resolver (Stage 32) with the race-safe wallet primitives from
:class:`EconomyRepo` and the append-only ledger writer from
:class:`TransactionsRepo`. The /cpc handler (Stage 34) will be thin
glue on top of this — same shape as TransferService is to /send.

Atomicity contract — the legacy gap we close
--------------------------------------------
Legacy ``rock_paper_scissors.py:539-547`` is the canonical bug pattern
this service exists to fix:

    if not remove_coins(loser_id, bet, "Проигрыш в КНБ"):
        logger.error("CPC: не удалось снять монеты с проигравшего ...")
    add_coins(winner_id, bet * 2, "Выигрыш в КНБ ...")

Two distinct writes against two unrelated wallet rows, NOT in a single
transaction. If ``remove_coins(loser, bet)`` fails (insufficient
funds — possible if the loser drained their wallet between the
challenge-accept and the choice timer), legacy *still calls*
``add_coins(winner, bet*2)`` — the warning gets logged and the winner
gets paid out of thin air. Money is created. Symmetrically on the
draw path (``:546-547``): two ``add_coins`` calls without a paired
escrow, so a missing wallet on one side leaves the other side
silently double-credited if anything funny happened to its read.

This service inverts the model: BOTH stakes go into escrow FIRST
(two race-safe holds in the same SQL transaction); only if both
holds succeed do we resolve the round and pay out. If the second
escrow fails, the first one is rolled back via a compensating
``release`` — see step 5 of :meth:`play`, which is where the whole
sequence lives; there is no separate post-validation method. The flow
runs inside the per-update session opened by
:class:`EconomyMiddleware`, so a crash mid-flow rolls back via the
middleware's ``except`` branch and no partial state lands.

#1559: escrow is ``EconomyRepo.hold``/``release`` — the balance
column only. A rolled-back escrow or a draw therefore leaves
``total_spent``/``total_earned`` exactly where they were; only a
decided round settles both holds as a genuine spend.

PvP only — legacy posture pinned in Stage 32
--------------------------------------------
The resolver module docstring (``games/rps.py``) already pins that
legacy is PvP only with no bot opponent. Two real user_ids are
required; ``challenger_id == opponent_id`` is rejected via
:attr:`RpsServiceOutcome.SAME_PLAYER`.

Ledger row count — one row per seat, plus the R8 rake row
---------------------------------------------------------
Legacy writes two ``save_game_result`` rows on every terminal state
(``rock_paper_scissors.py:543-544`` on win/loss,
``:548-550`` on draw) — one per participant — so the per-user game
history (`save_game_result` keys on user_id) shows both seats of the
match. ``save_game_result`` is the *game-history* table though, not
``transactions``. The ``transactions`` writes in legacy come from
``remove_coins``/``add_coins`` internally (each one writes a row).
That means legacy net-writes:

* Win/loss path: 1 ``remove_coins`` row (loser → 0) + 1 ``add_coins``
  row (0 → winner, amount = bet*2). Two ledger rows total.
* Draw path: 2 ``add_coins`` rows (refunds). Two ledger rows total.

The new pipeline writes ONE row per *wallet move*, via
``TransactionsRepo.record``. Both readers —
``TransactionsRepo.window_stats`` (received = rows naming the user in
``to_id``, sent = rows naming them in ``from_id``) and ``recent()``
(the signed lines of the ``/profile`` finances panel) — sum every row
regardless of ``type``, so a row that names a wallet it did not
actually move shows up as money the user never spent or earned.

* Both seats are escrowed up front, so both get an ``rps_stake`` row
  (``from_id=seat, to_id=None, amount=bet``). ``to_id`` is NULL
  because the coins go into the pot, not into the other player's
  wallet.
* CHALLENGER_WIN / OPPONENT_WIN then adds ``from_id=None,
  to_id=winner, amount=payout, type='rps_win'`` — the pot paying out.
  ``payout`` is ``int(bet * 1.9)`` since T-020/R8, where legacy handed
  over the full ``bet * 2`` pot. Naming the loser as the sender here
  (the old shape) billed them the whole pot on TOP of their stake row.
* TIE: two ``rps_refund`` rows, one per participant — they cancel the
  two stake rows, so a draw correctly sums to zero on both seats.

A decided round also writes ``rps_rake`` for the slice of the pot the
house edge burns (T-020/R8). It is deliberately ``from_id=None,
to_id=None``: it belongs to no wallet, so it stays invisible to the
per-user audit while an operator totalling ``WHERE type='rps_rake'``
can see exactly what the games earned.

The per-user audit (``SELECT * FROM transactions WHERE from_id=:uid
OR to_id=:uid``) still shows both participants' seat in every match —
the stake rows guarantee that even for the loser of a decided round.

Deferred to Stage 34
--------------------
* The /cpc handler + FSM for the challenge-accept / choose-move
  dance.
* Timeout coercion (legacy ``cleanup_choose_timeouts`` at ``:254``
  substitutes "rock" for a no-show choice) — the service takes
  already-resolved moves, so timeout policy lives in the handler.
* "Already in a game" guard (legacy ``:99``) — also handler / FSM
  concern; the service is stateless across calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn

from loguru import logger

from telegram_invite_bot.games.rps import (
    RpsConfig,
    RpsMove,
    RpsOutcome,
    RpsRoundResult,
    resolve_round,
)

if TYPE_CHECKING:
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo


_log = logger.bind(component="services.rps")


def _fail(msg: str, **fields: object) -> NoReturn:
    """Log a broken money-path invariant, then raise (#263).

    Twin of ``duel_service._fail``, for the same reason: the three payout
    sites below were bare ``assert``s, and the deployed systemd unit runs
    without ``-O`` in production, so they were live
    crashes that left no record of whose coins were mid-flight before the
    caller's rollback wiped the session.

    Still a raise, not a returned outcome: ``play`` runs inside one
    transaction the CALLER commits, and both stakes are already escrowed
    a few lines above — only an exception releases them.
    """
    _log.bind(**fields).error(msg)
    raise RuntimeError(msg)


class RpsServiceOutcome(StrEnum):
    """Mutually-exclusive verdicts a /cpc handler branches on.

    Three SUCCESS_* variants surface the resolved round; the rest
    are validation / wallet-shape rejections that prevent any
    economic side-effect. StrEnum so log lines render the name
    directly, matching :class:`TransferOutcome` posture.
    """

    SUCCESS_CHALLENGER_WIN = "success_challenger_win"
    SUCCESS_OPPONENT_WIN = "success_opponent_win"
    SUCCESS_TIE = "success_tie"

    INVALID_BET = "invalid_bet"
    """Bet outside ``[config.min_bet, config.max_bet]``. Distinct
    from NON_POSITIVE_BET so a future "min raised to 50" admin
    change can be debugged from the metric — non-positive is a
    handler-parser bug, out-of-range is a user choosing too
    little/much."""

    NON_POSITIVE_BET = "non_positive_bet"
    """``bet <= 0``. Defensive — the handler parser rejects, but a
    direct service call (tests, future internal flows) must not
    silently mint or burn coins via a negative bet flowing into
    EconomyRepo.hold (which would interpret -N as a credit due to
    the inverted ``balance >= amount`` guard)."""

    SAME_PLAYER = "same_player"
    """``challenger_id == opponent_id``. Legacy rejects this in the
    ``/cpc`` command handler (``rock_paper_scissors.py:351-352``), not
    in ``CPCManager.create``, which carries no same-player guard at
    all. Mirrored here at the service boundary so a future PvE port
    stays explicit about needing a different code path."""

    CHALLENGER_INSUFFICIENT_FUNDS = "challenger_insufficient_funds"
    OPPONENT_INSUFFICIENT_FUNDS = "opponent_insufficient_funds"

    NO_CHALLENGER_WALLET = "no_challenger_wallet"
    NO_OPPONENT_WALLET = "no_opponent_wallet"


@dataclass(frozen=True, slots=True)
class RpsServiceResult:
    """What a :meth:`RpsService.play` call produced.

    On any SUCCESS_* outcome, ``round_result``, ``challenger_balance``
    and ``opponent_balance`` are populated with post-resolution
    values; on rejection outcomes they are ``None``. The
    post-resolution balances come from the wallet writes themselves
    (not a re-read) so the values are race-faithful — see
    :class:`TransferService`'s ``sender_balance`` posture.
    """

    outcome: RpsServiceOutcome
    round_result: RpsRoundResult | None = None
    challenger_balance: int | None = None
    opponent_balance: int | None = None


class RpsService:
    """Atomic /cpc round resolution over the shared economy session.

    Constructed per-update by :class:`EconomyMiddleware`. The repos
    it depends on share that session, so every wallet UPDATE and
    every ledger INSERT here lands in a single transaction the
    middleware commits (or rolls back) at the end of dispatch.
    """

    def __init__(
        self,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        *,
        config: RpsConfig | None = None,
    ) -> None:
        self._economy = economy_repo
        self._ledger = transactions_repo
        self._config = config or RpsConfig()

    async def play(
        self,
        *,
        challenger_id: int,
        opponent_id: int,
        challenger_move: RpsMove,
        opponent_move: RpsMove,
        bet: int,
    ) -> RpsServiceResult:
        """Resolve a single round with atomic escrow + payout.

        Steps, in order (the order is load-bearing):

        1. Cheap validators — short-circuit before any DB I/O:

           a. ``same_player`` (challenger_id == opponent_id) →
              :attr:`RpsServiceOutcome.SAME_PLAYER`. Cheapest check,
              and same posture as TransferService rejecting
              self-transfer first.
           b. ``non_positive`` (bet <= 0) →
              :attr:`RpsServiceOutcome.NON_POSITIVE_BET`. Done
              before the bounds check so a -50 bet doesn't
              accidentally render as "outside [10, 100000]" (it
              IS, but the more meaningful rejection is the sign).
           c. ``out_of_bounds`` (bet < min OR bet > max) →
              :attr:`RpsServiceOutcome.INVALID_BET`.

        2. Wallet existence reads — both wallets read up front so
           the more useful NO_*_WALLET surfaces before any escrow
           lands. NO_CHALLENGER_WALLET is checked first so the
           cheap path stays "no challenger? fail fast".

        3. Python-level affordability pre-check on both wallets.
           Surfaces ``*_INSUFFICIENT_FUNDS`` without taking a
           wallet UPDATE. Race-protected by step 4's SQL guard, so
           a concurrent drain between this pre-check and the
           escrow collapses to the same outcome.

        4. **Atomic escrow** — hold both stakes. Done as two
           sequential ``EconomyRepo.hold`` calls under the shared
           session. The race-safety story:

           * ``hold`` uses ``UPDATE ... WHERE balance >= :bet``
             plus rowcount, so a hold that LOST the race
             (concurrent drainer) returns ``None`` without
             mutating the row.
           * If the FIRST hold (challenger) returns None →
             nothing was written → return
             CHALLENGER_INSUFFICIENT_FUNDS, no rollback needed.
           * If the FIRST succeeded but the SECOND (opponent)
             returns None → **the challenger's stake must be
             released before we return**. We do this via
             ``EconomyRepo.release(challenger_id, bet)`` in the
             same session. This is the explicit rollback legacy
             ``:539-541`` does NOT do.

           Note: the outer middleware transaction would ALSO roll
           the first hold back if we raised here, but raising
           would lose the typed outcome (it'd surface as a 500
           in the handler). A compensating release is the cleaner
           contract — we still return a typed result and the
           wallet is whole.

        5. **Resolve** — call the Stage 32 pure :func:`resolve_round`
           with both moves and the bet. No I/O. Produces
           :class:`RpsRoundResult` with the per-seat ``payout`` /
           ``net_delta``.

        6. **Payout** — credit winner / release both, all in the
           same session as the escrow:

           * CHALLENGER_WIN: ``credit(challenger, int(bet * 1.9))``
             — stake refund + most of the opponent's stake. Legacy
             ``:541`` credited the whole ``bet * 2`` pot; T-020/R8
             burns the remainder as the house cut.
           * OPPONENT_WIN: symmetric.
           * TIE: ``release(challenger, bet) + release(opponent,
             bet)`` — matches legacy ``:546-547``.
           * A decided round then settles both holds as a genuine
             spend via ``bump_totals`` (#1559); a tie settles
             nothing, so the lifetime counters never move.

        7. **Ledger writes** — one per wallet move: both stakes, the
           payout (or the two refunds), plus the R8 rake row on a
           decided round (rationale pinned in
           the module docstring). The flush
           inside ``TransactionsRepo.record`` surfaces constraint
           failures here rather than at the middleware's commit.

        Returns a fully-populated :class:`RpsServiceResult`. The
        post-resolution balances come from the wallet-write return
        values (hold + subsequent credit return the row's
        post-write state) so the receipt never has to re-read.
        """
        cfg = self._config

        # Step 1: cheap validators — order matters, pinned by tests.
        if challenger_id == opponent_id:
            return RpsServiceResult(outcome=RpsServiceOutcome.SAME_PLAYER)
        if bet <= 0:
            return RpsServiceResult(outcome=RpsServiceOutcome.NON_POSITIVE_BET)
        if bet < cfg.min_bet or bet > cfg.max_bet:
            return RpsServiceResult(outcome=RpsServiceOutcome.INVALID_BET)

        # Step 2: wallet existence.
        challenger = await self._economy.get(challenger_id)
        if challenger is None:
            return RpsServiceResult(outcome=RpsServiceOutcome.NO_CHALLENGER_WALLET)
        opponent = await self._economy.get(opponent_id)
        if opponent is None:
            return RpsServiceResult(outcome=RpsServiceOutcome.NO_OPPONENT_WALLET)

        # Step 3: Python-level affordability pre-check.
        if challenger.balance < bet:
            return RpsServiceResult(outcome=RpsServiceOutcome.CHALLENGER_INSUFFICIENT_FUNDS)
        if opponent.balance < bet:
            return RpsServiceResult(outcome=RpsServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS)

        # Step 4: atomic escrow with compensating rollback.
        escrowed_challenger = await self._economy.hold(challenger_id, bet)
        if escrowed_challenger is None:
            # Lost the race vs a concurrent drain of the challenger
            # between step 3 and now. No write landed; just surface.
            return RpsServiceResult(outcome=RpsServiceOutcome.CHALLENGER_INSUFFICIENT_FUNDS)

        escrowed_opponent = await self._economy.hold(opponent_id, bet)
        if escrowed_opponent is None:
            # Critical branch: the challenger's stake IS already
            # held. Release it here so the wallet is whole. This is
            # the explicit rollback step legacy ``:539-541`` does NOT
            # do (legacy logs and continues, paying the winner from
            # thin air on the analogous failure). The release lands in
            # the same session as the hold, so both lines roll back
            # together if the outer transaction itself aborts later.
            #
            # #1561: the result is checked like every other mutator in
            # this file. It used to carry an inline money-guard waiver
            # reading "compensating refund", which was exactly backwards
            # — a compensation is the one write that may not fail
            # silently. A swallowed ``None`` here left the challenger
            # permanently short of one stake with no log line, no
            # ``transactions`` row (this path never reaches
            # ``_write_ledger``) and a reply saying nothing happened.
            # Raising unwinds the hold through the caller's transaction,
            # which is the same compensation by a safer route. Twin of
            # ``duel_service.play``.
            released = await self._economy.release(challenger_id, bet)
            if released is None:
                _fail(
                    "rps challenger stake release failed",
                    challenger_id=challenger_id,
                    opponent_id=opponent_id,
                    bet=bet,
                    amount=bet,
                )
            return RpsServiceResult(outcome=RpsServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS)

        # Step 5: resolve the round (pure, no I/O).
        round_result = resolve_round(
            challenger_move,
            opponent_move,
            bet=bet,
            payout_multiplier=cfg.payout_multiplier,
        )

        # Step 6: payout. Track post-payout balances from the
        # wallet-write return values — see TransferService for the
        # same posture (avoid a re-read race vs concurrent credits).
        challenger_post = escrowed_challenger.balance
        opponent_post = escrowed_opponent.balance

        if round_result.outcome is RpsOutcome.CHALLENGER_WIN:
            payout = round_result.payout  # int(bet * 1.9) since R8
            credited = await self._economy.credit(challenger_id, payout)
            if credited is None:
                # The wallet existed a few lines up (we held it), so
                # this is the "row vanished mid-flow" edge — an admin
                # /reset landing between the hold and the credit. #263:
                # this used to be a bare ``assert`` whose comment claimed
                # it only "kept the type checker honest". It did not: the
                # prod unit runs without ``-O``, so the statement was a
                # live crash, and a mute one.
                _fail(
                    "rps payout to challenger failed",
                    challenger_id=challenger_id,
                    opponent_id=opponent_id,
                    bet=bet,
                    amount=payout,
                )
            challenger_post = credited.balance
            outcome = RpsServiceOutcome.SUCCESS_CHALLENGER_WIN
        elif round_result.outcome is RpsOutcome.OPPONENT_WIN:
            payout = round_result.payout
            credited = await self._economy.credit(opponent_id, payout)
            if credited is None:
                _fail(
                    "rps payout to opponent failed",
                    challenger_id=challenger_id,
                    opponent_id=opponent_id,
                    bet=bet,
                    amount=payout,
                )
            opponent_post = credited.balance
            outcome = RpsServiceOutcome.SUCCESS_OPPONENT_WIN
        else:
            # TIE — refund both stakes, no house edge (legacy
            # ``:546-547``). The resolver's ``payout`` field is the
            # per-player refund amount on TIE (==bet).
            refund = round_result.payout
            refunded_c = await self._economy.release(challenger_id, refund)
            refunded_o = await self._economy.release(opponent_id, refund)
            if refunded_c is None or refunded_o is None:
                _fail(
                    "rps tie refund failed",
                    challenger_id=challenger_id,
                    opponent_id=opponent_id,
                    bet=bet,
                    amount=refund,
                )
            challenger_post = refunded_c.balance
            opponent_post = refunded_o.balance
            outcome = RpsServiceOutcome.SUCCESS_TIE

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
        if outcome is not RpsServiceOutcome.SUCCESS_TIE:
            for settled_id in (challenger_id, opponent_id):
                if await self._economy.bump_totals(settled_id, spent=bet) is None:
                    _log.bind(user_id=settled_id, bet=bet).error(
                        "rps stake settlement failed; lifetime counters not moved"
                    )

        # Step 6b (A-11): record one ``games`` row per player + bump
        # games_played/won, on the shared session so the stats commit
        # atomically with the payout. Signed profit: winner
        # +(payout − stake), loser −stake, tie 0. Both seats count a
        # play; only the winner counts a win. ``game='rps'`` mirrors the
        # ``rps_*`` ledger naming.
        if outcome is RpsServiceOutcome.SUCCESS_CHALLENGER_WIN:
            c_won, c_profit, o_won, o_profit = True, round_result.payout - bet, False, -bet
        elif outcome is RpsServiceOutcome.SUCCESS_OPPONENT_WIN:
            c_won, c_profit, o_won, o_profit = False, -bet, True, round_result.payout - bet
        else:  # tie — both stakes refunded, neither wins
            c_won, c_profit, o_won, o_profit = False, 0, False, 0
        await self._economy.record_game(
            challenger_id, game="rps", bet=bet, won=c_won, profit=c_profit
        )
        await self._economy.record_game(
            opponent_id, game="rps", bet=bet, won=o_won, profit=o_profit
        )

        # Step 7: ledger rows. One per seat (mirroring legacy's two
        # ``remove_coins``/``add_coins`` writes) plus the R8 rake row
        # on a decided round — rationale in the module docstring.
        await self._write_ledger(
            round_result=round_result,
            challenger_id=challenger_id,
            opponent_id=opponent_id,
        )

        return RpsServiceResult(
            outcome=outcome,
            round_result=round_result,
            challenger_balance=challenger_post,
            opponent_balance=opponent_post,
        )

    async def _write_ledger(
        self,
        *,
        round_result: RpsRoundResult,
        challenger_id: int,
        opponent_id: int,
    ) -> None:
        """Append the audit trail for a resolved round.

        One row per wallet move — see the module docstring. Both seats
        were escrowed at the hold, so both get an ``rps_stake`` row; the
        winner then gets an ``rps_win`` row credited FROM the pot
        (``from_id=None``), or on a tie both get an ``rps_refund`` row
        that cancels their stake. Reasons embed the opposing user_id
        so a per-user audit shows who they played without joining on a
        separate session table.

        Both the payout and the rake row carry ``from_id=None``: those
        coins come out of the pot, not out of the loser's wallet,
        whose own stake row already books the full amount it lost.
        Naming the loser there would charge them the whole pot on top
        of their stake in every aggregate that sums by ``from_id``
        without filtering on type — which is what
        ``TransactionsRepo.window_stats`` and ``recent()`` both do.
        """
        bet = round_result.bet
        # Both stakes, always — including on a tie, where the refunds
        # below cancel them out. Booking only one side would leave the
        # refund looking like income out of nowhere.
        await self._ledger.record(
            from_id=challenger_id,
            to_id=None,
            amount=bet,
            reason=f"rps vs {opponent_id}",
            type="rps_stake",
        )
        await self._ledger.record(
            from_id=opponent_id,
            to_id=None,
            amount=bet,
            reason=f"rps vs {challenger_id}",
            type="rps_stake",
        )
        if round_result.outcome is RpsOutcome.CHALLENGER_WIN:
            await self._ledger.record(
                from_id=None,
                to_id=challenger_id,
                amount=round_result.payout,
                reason=f"rps vs {opponent_id}",
                type="rps_win",
            )
        elif round_result.outcome is RpsOutcome.OPPONENT_WIN:
            await self._ledger.record(
                from_id=None,
                to_id=opponent_id,
                amount=round_result.payout,
                reason=f"rps vs {challenger_id}",
                type="rps_win",
            )
        else:
            # TIE — two refund rows, one per participant.
            await self._ledger.record(
                from_id=None,
                to_id=challenger_id,
                amount=bet,
                reason=f"rps draw vs {opponent_id}",
                type="rps_refund",
            )
            await self._ledger.record(
                from_id=None,
                to_id=opponent_id,
                amount=bet,
                reason=f"rps draw vs {challenger_id}",
                type="rps_refund",
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
                reason=f"rps rake {challenger_id} vs {opponent_id}",
                type="rps_rake",
            )
