"""``RpsService.play`` — atomic escrow + payout for /cpc.

The pure resolver (challenger/opponent/tie matrix, payout math) is
pinned in ``tests/unit/games/test_rps.py``. The EconomyRepo
primitives (race-safe debit, credit) are pinned in their own tests.
This file pins the *flow* — validation order, two-sided escrow
atomicity (the headline value over legacy), ledger row shape,
conservation, and the concurrent-race pin.

Outcome taxonomy is exhaustive — every :class:`RpsServiceOutcome`
member has at least one test pinning the trigger condition. Same
posture as :mod:`test_transfer_service`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.games.rps import RpsConfig, RpsMove, RpsOutcome
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.rps_service import (
    RpsService,
    RpsServiceOutcome,
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _build_service(
    session: AsyncSession,
    *,
    config: RpsConfig | None = None,
) -> RpsService:
    return RpsService(
        EconomyRepo(session),
        TransactionsRepo(session),
        config=config,
    )


async def _seed_wallet(session: AsyncSession, user_id: int, balance: int = 1_000) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
    await session.commit()


async def _balance(session: AsyncSession, user_id: int) -> int | None:
    result = await session.execute(
        select(EconomyUser.balance).where(EconomyUser.user_id == user_id)
    )
    row = result.scalar_one_or_none()
    return int(row) if row is not None else None


async def _ledger_count(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Transaction))
    return int(result.scalar_one())


async def _money_supply(session: AsyncSession) -> int:
    """Sum of all wallet balances — used for the conservation pin."""
    result = await session.execute(select(func.sum(EconomyUser.balance)))
    return int(result.scalar_one() or 0)


async def _ledger_delta(session: AsyncSession, user_id: int) -> int:
    """What the ledger CLAIMS happened to ``user_id``'s balance.

    Reads the rows exactly the way the two consumers do —
    ``TransactionsRepo.window_stats`` (received = rows with ``to_id``,
    sent = rows with ``from_id``) and ``recent()`` (the signed lines on
    the ``/profile`` finances panel). Neither filters on ``type``, so
    every row a game writes lands in a user's numbers.
    """
    rows = (await session.execute(select(Transaction))).scalars().all()
    delta = 0
    for row in rows:
        if row.to_id == user_id:
            delta += abs(int(row.amount))
        if row.from_id == user_id:
            delta -= abs(int(row.amount))
    return delta


# ---------------------------------------------------------------------------
# Validation short-circuits (no DB I/O)
# ---------------------------------------------------------------------------


async def test_same_player_rejected_before_db_read(session: AsyncSession) -> None:
    """Pinned because same-player is a typo-class error and the
    validator must short-circuit before either wallet read — no
    wallets seeded means a wrongly-ordered check would surface as
    NO_CHALLENGER_WALLET instead."""
    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=42,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=100,
    )
    assert result.outcome is RpsServiceOutcome.SAME_PLAYER
    assert await _ledger_count(session) == 0


async def test_non_positive_bet_rejected(session: AsyncSession) -> None:
    """Defensive — handler parser rejects, but direct service calls
    must not silently mint/burn via a negative ``bet`` flowing into
    ``EconomyRepo.debit``'s ``balance >= amount`` guard (which would
    invert to "always true" for amount < 0)."""
    service = _build_service(session)
    zero = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=0,
    )
    assert zero.outcome is RpsServiceOutcome.NON_POSITIVE_BET
    negative = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=-10,
    )
    assert negative.outcome is RpsServiceOutcome.NON_POSITIVE_BET


async def test_bet_below_min_rejected(session: AsyncSession) -> None:
    """``bet < config.min_bet`` (default 10) → INVALID_BET.
    Distinct outcome from NON_POSITIVE_BET so the metric can
    distinguish "user asked for too little" from "parser broke"."""
    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=5,  # below default min_bet=10
    )
    assert result.outcome is RpsServiceOutcome.INVALID_BET


async def test_bet_above_max_rejected(session: AsyncSession) -> None:
    """``bet > config.max_bet`` (10 000 since T-020/R9, down from
    legacy's ``cpc_max_bet = 100000`` at
    ``rock_paper_scissors.py:664-665``) → INVALID_BET.

    The rejection is step 1, BEFORE either stake is escrowed, which is
    what makes lowering the ceiling safe to deploy mid-match: an
    in-flight offer above the new bound fails clean with no coins
    moved."""
    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=10_001,
    )
    assert result.outcome is RpsServiceOutcome.INVALID_BET


# ---------------------------------------------------------------------------
# Wallet existence
# ---------------------------------------------------------------------------


async def test_missing_challenger_wallet(session: AsyncSession) -> None:
    """Opponent exists, challenger doesn't → NO_CHALLENGER_WALLET,
    no mutation. Pinned to ensure the opponent read doesn't fire
    BEFORE the challenger read (waste of a round-trip)."""
    await _seed_wallet(session, 99)
    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=100,
    )
    assert result.outcome is RpsServiceOutcome.NO_CHALLENGER_WALLET
    assert await _balance(session, 99) == 1_000  # untouched


async def test_missing_opponent_wallet(session: AsyncSession) -> None:
    """Challenger exists, opponent doesn't → NO_OPPONENT_WALLET,
    NO challenger mutation. Critical pin: legacy would have already
    escrowed the challenger via ``remove_coins`` and then crashed on
    the opponent half. Our service rejects BEFORE escrow."""
    await _seed_wallet(session, 42, balance=500)
    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=100,
    )
    assert result.outcome is RpsServiceOutcome.NO_OPPONENT_WALLET
    assert await _balance(session, 42) == 500  # NOT debited


# ---------------------------------------------------------------------------
# Insufficient funds — Python pre-check
# ---------------------------------------------------------------------------


async def test_challenger_insufficient_funds_pre_check(session: AsyncSession) -> None:
    await _seed_wallet(session, 42, balance=50)
    await _seed_wallet(session, 99, balance=1_000)
    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=100,
    )
    assert result.outcome is RpsServiceOutcome.CHALLENGER_INSUFFICIENT_FUNDS
    assert await _balance(session, 42) == 50  # no escrow
    assert await _balance(session, 99) == 1_000  # no escrow


async def test_opponent_insufficient_funds_pre_check(session: AsyncSession) -> None:
    await _seed_wallet(session, 42, balance=1_000)
    await _seed_wallet(session, 99, balance=50)
    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=100,
    )
    assert result.outcome is RpsServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS
    assert await _balance(session, 42) == 1_000  # no escrow
    assert await _balance(session, 99) == 50  # no escrow


# ---------------------------------------------------------------------------
# THE HEADLINE PIN: opponent escrow fails AFTER challenger escrow lands
# ---------------------------------------------------------------------------


async def test_opponent_escrow_failure_rolls_back_challenger_debit(
    session: AsyncSession,
) -> None:
    """Race pin: challenger has exactly the bet, opponent has bet-1.

    The Python pre-check at step 3 will catch the opponent case
    BEFORE the challenger debit lands — so this test actually pins
    the cheaper path. The truly load-bearing rollback branch is
    covered by ``test_opponent_escrow_rollback_via_drained_opponent``
    which forces the SQL guard to fire by draining the opponent
    between pre-check and debit; here we just pin that the simple
    case never debits either wallet.

    Legacy ``rock_paper_scissors.py:539-541`` does NOT do rollback
    on the analogous failure — it logs an error and pays the winner
    out of thin air. This is the new guarantee of the pipeline.
    """
    await _seed_wallet(session, 42, balance=100)  # exactly the bet
    await _seed_wallet(session, 99, balance=99)  # one short
    supply_before = await _money_supply(session)

    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.SCISSORS,
        bet=100,
    )

    assert result.outcome is RpsServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS
    # Challenger's balance MUST NOT have decreased — this is the
    # property legacy violates.
    assert await _balance(session, 42) == 100
    assert await _balance(session, 99) == 99
    # Conservation:
    assert await _money_supply(session) == supply_before
    assert await _ledger_count(session) == 0


async def test_opponent_escrow_rollback_via_drained_opponent(
    session: AsyncSession,
) -> None:
    """SQL-guard race: opponent passes the Python pre-check but the
    SQL hold fails (rowcount=0). We simulate the concurrent drain
    by hand-debiting the opponent between the two reads.

    The compensating ``release`` after the failed second hold is the
    rollback contract — pin that the challenger's balance is whole
    and conservation holds. This is the gap-vs-legacy
    (``rock_paper_scissors.py:539-541`` never refunds).
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)

    # Monkey-patch EconomyRepo to drain opponent between the read
    # and the hold — simulates a concurrent withdrawal that lands
    # after step 3's pre-check passed but before step 4's hold.
    repo = EconomyRepo(session)
    real_hold = repo.hold
    drained = {"done": False}

    async def racing_hold(user_id: int, amount: int):  # type: ignore[no-untyped-def]
        if user_id == 99 and not drained["done"]:
            # First call to hold for the opponent: drain them
            # right before the actual hold runs. Use raw UPDATE
            # so we don't recurse.
            from sqlalchemy import update

            await session.execute(
                update(EconomyUser).where(EconomyUser.user_id == 99).values(balance=0)
            )
            drained["done"] = True
        return await real_hold(user_id, amount)

    repo.hold = racing_hold  # type: ignore[method-assign]
    service = RpsService(repo, TransactionsRepo(session))

    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=100,
    )

    assert result.outcome is RpsServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS
    # Challenger held 100 then released 100 → net 0. Pin: legacy
    # does NOT do this refund.
    assert await _balance(session, 42) == 500
    # Opponent was drained to 0 by the simulated race — that's the
    # external write, not ours.
    assert await _balance(session, 99) == 0
    assert await _ledger_count(session) == 0


# ---------------------------------------------------------------------------
# Success paths — three resolved outcomes
# ---------------------------------------------------------------------------


async def test_challenger_win_payout_and_ledger(session: AsyncSession) -> None:
    """ROCK beats SCISSORS — challenger collects the pot minus the R8
    rake, opponent loses their stake. Four ledger rows (2× rps_stake +
    rps_win + rps_rake) — see module docstring rationale."""
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)
    supply_before = await _money_supply(session)

    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.SCISSORS,
        bet=100,
    )

    assert result.outcome is RpsServiceOutcome.SUCCESS_CHALLENGER_WIN
    assert result.round_result is not None
    assert result.round_result.outcome is RpsOutcome.CHALLENGER_WIN
    assert result.round_result.net_delta == 90
    assert result.round_result.payout == 190
    assert result.round_result.rake == 10
    assert result.challenger_balance == 590  # 500 - 100 + 190
    assert result.opponent_balance == 400  # 500 - 100
    assert await _balance(session, 42) == 590
    assert await _balance(session, 99) == 400
    # T-020/R8 — the supply SHRINKS by the rake. Legacy conserved it
    # exactly, which is what made /cpc a zero-edge game the owner had
    # to bankroll at /withdraw.
    assert await _money_supply(session) == supply_before - 10
    assert await _ledger_count(session) == 4

    rows = (await session.execute(select(Transaction).order_by(Transaction.type))).scalars().all()
    by_type: dict[str, list[Transaction]] = {}
    for row in rows:
        by_type.setdefault(row.type, []).append(row)
    assert set(by_type) == {"rps_stake", "rps_win", "rps_rake"}
    # One stake row per seat: both wallets really were debited.
    stake_rows = by_type["rps_stake"]
    assert len(stake_rows) == 2
    assert {r.from_id for r in stake_rows} == {42, 99}
    assert all(r.amount == 100 and r.to_id is None for r in stake_rows)
    (win_row,) = by_type["rps_win"]
    assert win_row.amount == 190
    # The pot pays the winner, not the loser's wallet. Charging the
    # loser here (the old shape) billed them 190 on top of their 100
    # stake in every aggregate that sums by ``from_id``.
    assert win_row.from_id is None
    assert win_row.to_id == 42
    (rake_row,) = by_type["rps_rake"]
    assert rake_row.amount == 10
    # Belongs to neither wallet: a per-user audit that sums by from_id
    # must not see the burn charged to the loser on top of their stake.
    assert rake_row.from_id is None
    assert rake_row.to_id is None


async def test_opponent_win_payout_and_ledger(session: AsyncSession) -> None:
    """PAPER beats ROCK — opponent gains +bet net, challenger loses
    bet. Symmetric to the challenger-win test."""
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)
    supply_before = await _money_supply(session)

    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.PAPER,
        bet=100,
    )

    assert result.outcome is RpsServiceOutcome.SUCCESS_OPPONENT_WIN
    assert result.round_result is not None
    assert result.round_result.outcome is RpsOutcome.OPPONENT_WIN
    assert result.challenger_balance == 400
    assert result.opponent_balance == 590
    assert result.round_result.rake == 10
    assert await _balance(session, 42) == 400
    assert await _balance(session, 99) == 590
    assert await _money_supply(session) == supply_before - 10
    assert await _ledger_count(session) == 4

    win_row = (
        await session.execute(select(Transaction).where(Transaction.type == "rps_win"))
    ).scalar_one()
    assert win_row.amount == 190
    assert win_row.from_id is None
    assert win_row.to_id == 99
    rake_row = (
        await session.execute(select(Transaction).where(Transaction.type == "rps_rake"))
    ).scalar_one()
    assert rake_row.amount == 10


async def test_tie_refunds_both_no_house_edge(session: AsyncSession) -> None:
    """Both pick ROCK — both stakes return.

    Four rows: the two ``rps_stake`` debits and the two ``rps_refund``
    credits that cancel them. The refunds used to stand alone, which
    read as income out of nowhere on both seats' finances panel.
    Conservation pin: total money supply unchanged (legacy
    ``rock_paper_scissors.py:546-547`` has no house edge).
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)
    supply_before = await _money_supply(session)

    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.ROCK,
        bet=100,
    )

    assert result.outcome is RpsServiceOutcome.SUCCESS_TIE
    assert result.round_result is not None
    assert result.round_result.outcome is RpsOutcome.TIE
    assert result.round_result.net_delta == 0
    # Both balances back to the original 500.
    assert result.challenger_balance == 500
    assert result.opponent_balance == 500
    assert await _balance(session, 42) == 500
    assert await _balance(session, 99) == 500
    assert await _money_supply(session) == supply_before
    assert await _ledger_count(session) == 4

    refund_rows = (
        (await session.execute(select(Transaction).where(Transaction.type == "rps_refund")))
        .scalars()
        .all()
    )
    assert len(refund_rows) == 2
    refunded_to = {row.to_id for row in refund_rows}
    assert refunded_to == {42, 99}
    stake_rows = (
        (await session.execute(select(Transaction).where(Transaction.type == "rps_stake")))
        .scalars()
        .all()
    )
    assert {row.from_id for row in stake_rows} == {42, 99}
    assert all(row.amount == 100 and row.to_id is None for row in stake_rows)


# ---------------------------------------------------------------------------
# Ledger ↔ wallet agreement
# ---------------------------------------------------------------------------


async def test_ledger_matches_the_real_wallet_move_on_a_decided_round(
    session: AsyncSession,
) -> None:
    """Each seat's signed rows sum to what its wallet actually did.

    This is the property the old row shape broke: the payout row named
    the loser as the sender, so on top of their 100-coin stake the
    ledger charged them the whole 190 pot — a 290-coin "spend" for a
    100-coin loss, visible in ``/profile`` and the weekly totals.
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)

    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.SCISSORS,
        bet=100,
    )

    assert result.outcome is RpsServiceOutcome.SUCCESS_CHALLENGER_WIN
    assert await _balance(session, 42) == 590
    assert await _balance(session, 99) == 400
    assert await _ledger_delta(session, 42) == 90
    assert await _ledger_delta(session, 99) == -100


async def test_ledger_matches_the_real_wallet_move_on_a_tie(
    session: AsyncSession,
) -> None:
    """A draw moves no money, so it must sum to zero on both seats.

    The refund rows alone read as +bet income from nowhere; they are
    only honest paired with the stake rows they reverse.
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)

    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.ROCK,
        bet=100,
    )

    assert result.outcome is RpsServiceOutcome.SUCCESS_TIE
    assert await _ledger_delta(session, 42) == 0
    assert await _ledger_delta(session, 99) == 0


# ---------------------------------------------------------------------------
# Concurrent race — asyncio.gather over the same challenger
# ---------------------------------------------------------------------------


async def test_concurrent_play_collapses_to_one_success(
    session: AsyncSession,
) -> None:
    """Two concurrent /cpc plays from the same challenger with two
    different opponents — challenger has only enough for ONE bet.
    Exactly one game must succeed; the other must return
    CHALLENGER_INSUFFICIENT_FUNDS via either the Python pre-check
    OR the SQL race guard (both collapse to the same outcome).

    aiosqlite runs statements serially on its background thread,
    so ``asyncio.gather`` interleaves at await points but the SQL
    UPDATEs hit the DB sequentially — the rowcount guard on the
    escrow hold is the race-decider.
    """
    await _seed_wallet(session, 42, balance=100)  # only one bet's worth
    await _seed_wallet(session, 99, balance=1_000)
    await _seed_wallet(session, 77, balance=1_000)

    service = _build_service(session)
    a, b = await asyncio.gather(
        service.play(
            challenger_id=42,
            opponent_id=99,
            challenger_move=RpsMove.ROCK,
            opponent_move=RpsMove.SCISSORS,
            bet=100,
        ),
        service.play(
            challenger_id=42,
            opponent_id=77,
            challenger_move=RpsMove.ROCK,
            opponent_move=RpsMove.SCISSORS,
            bet=100,
        ),
    )

    outcomes = {a.outcome, b.outcome}
    assert RpsServiceOutcome.CHALLENGER_INSUFFICIENT_FUNDS in outcomes
    assert RpsServiceOutcome.SUCCESS_CHALLENGER_WIN in outcomes

    # Exactly one settlement's worth of rows (2× stake + win + rake)
    # for the single successful game.
    assert await _ledger_count(session) == 4


# ── #263: the payout guard raises, and says whose coins ──────────────


async def test_payout_failure_raises_runtime_error_not_assertion(
    session: AsyncSession,
) -> None:
    """A vanished wallet at payout time raises ``RuntimeError``.

    Twin of the /duel case — see
    ``tests.integration.services.test_duel_service`` for the full
    argument. Briefly: this was a bare ``assert``, the prod unit runs
    without ``-O`` so it was a live crash, and a revert fails here
    whether or not asserts are enabled (``AssertionError`` is not
    ``RuntimeError``; under ``-O`` nothing raises at all and the read of
    ``None.balance`` blows up somewhere less legible).
    """
    await _seed_wallet(session, 42, 1_000)
    await _seed_wallet(session, 99, 1_000)
    service = _build_service(session)

    async def _vanished(user_id: int, amount: int) -> None:
        return None

    service._economy.credit = _vanished  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="rps payout to challenger failed"):
        await service.play(
            challenger_id=42,
            opponent_id=99,
            challenger_move=RpsMove.ROCK,
            opponent_move=RpsMove.SCISSORS,
            bet=100,
        )

    await session.rollback()
    assert await _balance(session, 42) == 1_000
    assert await _balance(session, 99) == 1_000
    assert await _ledger_count(session) == 0


async def test_failed_compensating_release_raises_instead_of_burning_the_stake(
    session: AsyncSession,
) -> None:
    """#1561: twin of the /duel pin — the rollback release may not
    fail silently.

    The opponent's hold fails, so the branch above compensates the
    challenger. If that compensation itself returns ``None``, the old
    code discarded the result and returned
    OPPONENT_INSUFFICIENT_FUNDS, leaving the challenger permanently
    short of one stake with no log line and no ledger row — this path
    never reaches ``_write_ledger``. Raising instead unwinds the hold
    through the caller's transaction.
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)

    repo = EconomyRepo(session)
    real_hold = repo.hold

    async def refusing_opponent_hold(user_id: int, amount: int):  # type: ignore[no-untyped-def]
        if user_id == 99:
            return None
        return await real_hold(user_id, amount)

    async def refusing_release(user_id: int, amount: int):  # type: ignore[no-untyped-def]
        return None

    repo.hold = refusing_opponent_hold  # type: ignore[method-assign]
    repo.release = refusing_release  # type: ignore[method-assign]
    service = RpsService(repo, TransactionsRepo(session))

    with pytest.raises(RuntimeError, match="rps challenger stake release failed"):
        await service.play(
            challenger_id=42,
            opponent_id=99,
            challenger_move=RpsMove.ROCK,
            opponent_move=RpsMove.PAPER,
            bet=100,
        )

    await session.rollback()
    assert await _balance(session, 42) == 500
    assert await _ledger_count(session) == 0
