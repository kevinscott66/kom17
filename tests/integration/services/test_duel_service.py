"""``DuelService.play`` — atomic escrow + payout for /duel (T-018).

Twin of :mod:`tests.integration.services.test_rps_service`. Pins the
flow contract: validation order, two-sided escrow atomicity, ledger
shape, conservation, concurrent race. Exhaustive over the
:class:`DuelServiceOutcome` enum.

The pure resolver matrix (higher/lower/tie) and payout math are
pinned in ``tests/unit/games/test_duel.py``; this file only exercises
:meth:`DuelService.play`.
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
from telegram_invite_bot.games.duel import DuelConfig, DuelOutcome
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.duel_service import (
    DuelService,
    DuelServiceOutcome,
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
    config: DuelConfig | None = None,
) -> DuelService:
    return DuelService(
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
    result = await session.execute(select(func.sum(EconomyUser.balance)))
    return int(result.scalar_one() or 0)


async def _ledger_delta(session: AsyncSession, user_id: int) -> int:
    """What the ledger CLAIMS happened to ``user_id``'s balance.

    Reads the rows exactly the way the two consumers do —
    ``TransactionsRepo.window_stats`` (received = rows with
    ``to_id``, sent = rows with ``from_id``) and ``recent()`` (the
    signed lines on the ``/profile`` finances panel). Neither filters
    on ``type``, so every row a game writes lands in a user's numbers.
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
    service = _build_service(session)
    result = await service.play(
        challenger_id=42,
        opponent_id=42,
        challenger_roll=4,
        opponent_roll=3,
        bet=100,
    )
    assert result.outcome is DuelServiceOutcome.SAME_PLAYER
    assert await _ledger_count(session) == 0


async def test_non_positive_bet_rejected(session: AsyncSession) -> None:
    service = _build_service(session)
    zero = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=0
    )
    assert zero.outcome is DuelServiceOutcome.NON_POSITIVE_BET
    negative = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=-10
    )
    assert negative.outcome is DuelServiceOutcome.NON_POSITIVE_BET


async def test_bet_below_min_rejected(session: AsyncSession) -> None:
    """Default ``min_bet`` is 10 (legacy DUEL_MIN_BET=10)."""
    service = _build_service(session)
    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=5
    )
    assert result.outcome is DuelServiceOutcome.INVALID_BET


async def test_bet_above_max_rejected(session: AsyncSession) -> None:
    """Default ``max_bet`` is 10_000 (legacy DUEL_MAX_BET=10000)."""
    service = _build_service(session)
    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=10_001
    )
    assert result.outcome is DuelServiceOutcome.INVALID_BET


# ---------------------------------------------------------------------------
# Wallet existence
# ---------------------------------------------------------------------------


async def test_missing_challenger_wallet(session: AsyncSession) -> None:
    await _seed_wallet(session, 99)
    service = _build_service(session)
    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=100
    )
    assert result.outcome is DuelServiceOutcome.NO_CHALLENGER_WALLET
    assert await _balance(session, 99) == 1_000


async def test_missing_opponent_wallet(session: AsyncSession) -> None:
    await _seed_wallet(session, 42, balance=500)
    service = _build_service(session)
    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=100
    )
    assert result.outcome is DuelServiceOutcome.NO_OPPONENT_WALLET
    assert await _balance(session, 42) == 500  # NOT debited


# ---------------------------------------------------------------------------
# Insufficient funds — Python pre-check
# ---------------------------------------------------------------------------


async def test_challenger_insufficient_funds_pre_check(session: AsyncSession) -> None:
    await _seed_wallet(session, 42, balance=50)
    await _seed_wallet(session, 99, balance=1_000)
    service = _build_service(session)
    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=100
    )
    assert result.outcome is DuelServiceOutcome.CHALLENGER_INSUFFICIENT_FUNDS
    assert await _balance(session, 42) == 50
    assert await _balance(session, 99) == 1_000


async def test_opponent_insufficient_funds_pre_check(session: AsyncSession) -> None:
    await _seed_wallet(session, 42, balance=1_000)
    await _seed_wallet(session, 99, balance=50)
    service = _build_service(session)
    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=100
    )
    assert result.outcome is DuelServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS
    assert await _balance(session, 42) == 1_000
    assert await _balance(session, 99) == 50


# ---------------------------------------------------------------------------
# Headline pin: opponent escrow fails AFTER challenger escrow lands.
# Legacy ``Duel.finish`` (bot.py:15243-15252) does not compensate.
# ---------------------------------------------------------------------------


async def test_opponent_escrow_rollback_via_drained_opponent(
    session: AsyncSession,
) -> None:
    """SQL-guard race: opponent passes the Python pre-check but the
    SQL hold fails (rowcount=0). The compensating release of the
    challenger's hold is the rollback contract.
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)

    repo = EconomyRepo(session)
    real_hold = repo.hold
    drained = {"done": False}

    async def racing_hold(user_id: int, amount: int):  # type: ignore[no-untyped-def]
        if user_id == 99 and not drained["done"]:
            from sqlalchemy import update

            await session.execute(
                update(EconomyUser).where(EconomyUser.user_id == 99).values(balance=0)
            )
            drained["done"] = True
        return await real_hold(user_id, amount)

    repo.hold = racing_hold  # type: ignore[method-assign]
    service = DuelService(repo, TransactionsRepo(session))

    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=100
    )

    assert result.outcome is DuelServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS
    # Challenger held 100 then released 100 → net 0. Legacy DOES
    # NOT do this refund.
    assert await _balance(session, 42) == 500
    assert await _balance(session, 99) == 0  # drained externally
    assert await _ledger_count(session) == 0


# ---------------------------------------------------------------------------
# Success paths — three resolved outcomes
# ---------------------------------------------------------------------------


async def test_challenger_win_payout_and_ledger(session: AsyncSession) -> None:
    """6 > 1 → challenger wins. Four ledger rows, and the money supply
    SHRINKS by the R8 rake — the whole point of the house edge."""
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)
    supply_before = await _money_supply(session)

    service = _build_service(session)
    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=6, opponent_roll=1, bet=100
    )

    assert result.outcome is DuelServiceOutcome.SUCCESS_CHALLENGER_WIN
    assert result.round_result is not None
    assert result.round_result.outcome is DuelOutcome.CHALLENGER_WIN
    assert result.round_result.net_delta == 90
    assert result.round_result.payout == 190
    assert result.round_result.rake == 10
    assert result.challenger_balance == 590
    assert result.opponent_balance == 400
    assert await _balance(session, 42) == 590
    assert await _balance(session, 99) == 400
    # T-020/R8: coins actually LEAVE the ecosystem here. Legacy paid the
    # full 200 pot and left the supply flat, which is exactly what made
    # /duel free money for a patient player.
    assert await _money_supply(session) == supply_before - 10
    assert await _ledger_count(session) == 4

    rows = (await session.execute(select(Transaction).order_by(Transaction.type))).scalars().all()
    by_type: dict[str, list[Transaction]] = {}
    for row in rows:
        by_type.setdefault(row.type, []).append(row)
    assert set(by_type) == {"duel_stake", "duel_win", "duel_rake"}
    # One stake row per seat: both wallets really were debited.
    stake_rows = by_type["duel_stake"]
    assert len(stake_rows) == 2
    assert {r.from_id for r in stake_rows} == {42, 99}
    assert all(r.amount == 100 and r.to_id is None for r in stake_rows)
    (win_row,) = by_type["duel_win"]
    assert win_row.amount == 190
    # The pot pays the winner, not the loser's wallet. Charging the
    # loser here (the old shape) billed them 190 on top of their 100
    # stake in every aggregate that sums by ``from_id``.
    assert win_row.from_id is None
    assert win_row.to_id == 42
    (rake_row,) = by_type["duel_rake"]
    assert rake_row.amount == 10
    # Belongs to neither wallet: a per-user audit that sums by from_id
    # must not see the burn charged to the loser on top of their stake.
    assert rake_row.from_id is None
    assert rake_row.to_id is None


async def test_opponent_win_payout_and_ledger(session: AsyncSession) -> None:
    """2 < 5 → opponent wins. Symmetric to challenger-win."""
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)
    supply_before = await _money_supply(session)

    service = _build_service(session)
    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=2, opponent_roll=5, bet=100
    )

    assert result.outcome is DuelServiceOutcome.SUCCESS_OPPONENT_WIN
    assert result.round_result is not None
    assert result.round_result.outcome is DuelOutcome.OPPONENT_WIN
    assert result.challenger_balance == 400
    assert result.opponent_balance == 590
    assert result.round_result.rake == 10
    assert await _balance(session, 42) == 400
    assert await _balance(session, 99) == 590
    assert await _money_supply(session) == supply_before - 10
    assert await _ledger_count(session) == 4

    win_row = (
        await session.execute(select(Transaction).where(Transaction.type == "duel_win"))
    ).scalar_one()
    assert win_row.amount == 190
    assert win_row.from_id is None
    assert win_row.to_id == 99
    rake_row = (
        await session.execute(select(Transaction).where(Transaction.type == "duel_rake"))
    ).scalar_one()
    assert rake_row.amount == 10


async def test_tie_refunds_both_no_house_edge(session: AsyncSession) -> None:
    """Equal rolls → tie → both stakes refunded.

    Four rows: the two stake debits and the two refunds that cancel
    them. The refunds used to stand alone, which read as income out of
    nowhere on both seats' finances panel.
    """
    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)
    supply_before = await _money_supply(session)

    service = _build_service(session)
    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=4, bet=100
    )

    assert result.outcome is DuelServiceOutcome.SUCCESS_TIE
    assert result.round_result is not None
    assert result.round_result.outcome is DuelOutcome.TIE
    assert result.round_result.net_delta == 0
    assert result.challenger_balance == 500
    assert result.opponent_balance == 500
    assert await _balance(session, 42) == 500
    assert await _balance(session, 99) == 500
    assert await _money_supply(session) == supply_before
    assert await _ledger_count(session) == 4

    refund_rows = (
        (await session.execute(select(Transaction).where(Transaction.type == "duel_refund")))
        .scalars()
        .all()
    )
    assert len(refund_rows) == 2
    refunded_to = {row.to_id for row in refund_rows}
    assert refunded_to == {42, 99}
    stake_rows = (
        (await session.execute(select(Transaction).where(Transaction.type == "duel_stake")))
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
        challenger_id=42, opponent_id=99, challenger_roll=6, opponent_roll=1, bet=100
    )

    assert result.outcome is DuelServiceOutcome.SUCCESS_CHALLENGER_WIN
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
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=4, bet=100
    )

    assert result.outcome is DuelServiceOutcome.SUCCESS_TIE
    assert await _ledger_delta(session, 42) == 0
    assert await _ledger_delta(session, 99) == 0


# ---------------------------------------------------------------------------
# Concurrent race — asyncio.gather over the same challenger
# ---------------------------------------------------------------------------


async def test_concurrent_play_collapses_to_one_success(
    session: AsyncSession,
) -> None:
    """Two concurrent /duel plays from the same challenger — only enough
    for one bet. The SQL rowcount guard decides; one succeeds, the
    other returns CHALLENGER_INSUFFICIENT_FUNDS.
    """
    await _seed_wallet(session, 42, balance=100)
    await _seed_wallet(session, 99, balance=1_000)
    await _seed_wallet(session, 77, balance=1_000)

    service = _build_service(session)
    a, b = await asyncio.gather(
        service.play(challenger_id=42, opponent_id=99, challenger_roll=6, opponent_roll=1, bet=100),
        service.play(challenger_id=42, opponent_id=77, challenger_roll=6, opponent_roll=1, bet=100),
    )

    outcomes = {a.outcome, b.outcome}
    assert DuelServiceOutcome.CHALLENGER_INSUFFICIENT_FUNDS in outcomes
    assert DuelServiceOutcome.SUCCESS_CHALLENGER_WIN in outcomes
    # Exactly one round's worth of rows (2× stake + win + rake) — the
    # loser of the race must leave nothing behind.
    assert await _ledger_count(session) == 4


async def test_challenger_win_records_games_for_both_players(session: AsyncSession) -> None:
    """A-11: a resolved duel writes one game='duel' row per player +
    bumps counters. Winner profit +bet, loser −bet; only winner wins."""
    from telegram_invite_bot.db.models.economy import GameResult

    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)

    service = _build_service(session)
    await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=6, opponent_roll=1, bet=100
    )

    rows = (await session.execute(select(GameResult).order_by(GameResult.user_id))).scalars().all()
    assert len(rows) == 2
    assert all(r.game == "duel" and r.bet == 100 for r in rows)
    by_user = {r.user_id: r for r in rows}
    assert by_user[42].win is True and by_user[42].profit == 90  # payout 190 − stake 100
    assert by_user[99].win is False and by_user[99].profit == -100

    winner = await session.get(EconomyUser, 42)
    loser = await session.get(EconomyUser, 99)
    assert winner is not None and loser is not None
    assert winner.games_played == 1 and winner.games_won == 1
    assert loser.games_played == 1 and loser.games_won == 0


async def test_tie_records_games_zero_profit_no_win(session: AsyncSession) -> None:
    """A tie counts a play for both, a win for neither, profit 0."""
    from telegram_invite_bot.db.models.economy import GameResult

    await _seed_wallet(session, 42, balance=500)
    await _seed_wallet(session, 99, balance=500)

    service = _build_service(session)
    result = await service.play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=4, bet=100
    )
    assert result.outcome is DuelServiceOutcome.SUCCESS_TIE

    rows = (await session.execute(select(GameResult))).scalars().all()
    assert len(rows) == 2
    assert all(r.win is False and r.profit == 0 for r in rows)
    for uid in (42, 99):
        user = await session.get(EconomyUser, uid)
        assert user is not None
        assert user.games_played == 1 and user.games_won == 0


# ── #263: the payout guard raises, and says whose coins ──────────────


async def test_payout_failure_raises_runtime_error_not_assertion(
    session: AsyncSession,
) -> None:
    """A vanished wallet at payout time raises ``RuntimeError``.

    #263: this site used to be a bare ``assert credited is not None``.
    The deployed systemd unit runs without ``-O`` in production, so that
    assert *was* live — but
    a revert to it fails here two different ways, which is the point of
    pinning the exception type rather than just "something raised":

    * with asserts live, ``AssertionError`` is not ``RuntimeError``;
    * under ``-O``, the assert vanishes entirely and ``play`` walks off
      the end reading ``None.balance``.

    Neither is what a money path should do. ``credit`` returning ``None``
    here means the row went away between the escrow debit and the
    payout — an admin ``/reset`` landing in the gap — and the only
    correct response is to raise so the caller's rollback releases both
    stakes.
    """
    await _seed_wallet(session, 42, 1_000)
    await _seed_wallet(session, 99, 1_000)
    service = _build_service(session)

    async def _vanished(user_id: int, amount: int) -> None:
        return None

    service._economy.credit = _vanished  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="duel payout to challenger failed"):
        await service.play(
            challenger_id=42,
            opponent_id=99,
            challenger_roll=6,
            opponent_roll=1,
            bet=100,
        )

    # Raising (rather than returning an outcome) is what makes the
    # caller's rollback undo BOTH escrow debits. Nothing was committed,
    # so the money supply is untouched.
    await session.rollback()
    assert await _balance(session, 42) == 1_000
    assert await _balance(session, 99) == 1_000
    assert await _ledger_count(session) == 0


async def test_failed_compensating_release_raises_instead_of_burning_the_stake(
    session: AsyncSession,
) -> None:
    """#1561: the rollback release may not fail silently.

    The opponent's hold fails, so the branch above compensates the
    challenger. If that compensation itself returns ``None`` — the
    row gone under an admin ``/reset``, or the balance cap — the old
    code discarded the result, returned OPPONENT_INSUFFICIENT_FUNDS
    and let the caller commit. The challenger was then permanently
    short of one stake with no log line and no ledger row, on a path
    whose reply says nothing happened. Raising instead unwinds the
    hold through the caller's transaction.
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
    service = DuelService(repo, TransactionsRepo(session))

    with pytest.raises(RuntimeError, match="duel challenger stake release failed"):
        await service.play(
            challenger_id=42,
            opponent_id=99,
            challenger_roll=4,
            opponent_roll=3,
            bet=100,
        )

    # The caller's transaction is what hands the stake back now.
    await session.rollback()
    assert await _balance(session, 42) == 500
    assert await _ledger_count(session) == 0
