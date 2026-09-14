"""#1559 — PvP escrow must not move the lifetime counters.

Every escrow leg of ``/pvp_coin``, ``/pvp_dice``, ``/duel`` and ``/cpc``
used to be ``EconomyRepo.debit`` / ``EconomyRepo.credit``, both of which
move ``total_spent`` / ``total_earned`` on top of the balance column. A
stake that is only *parked* is neither a purchase nor income, so a
``create -> cancel`` round trip, an expiry, a dice tie and the
compensating rollback all inflated both lifetime counters for free — no
counterparty, no cost, repeatable at the rate of the global token
bucket.

The escrow legs are now ``hold`` / ``release`` (balance column only) and
a DECIDED round settles both seats' holds as a genuine spend via
``bump_totals``. This module pins both halves: the counters stay frozen
across every non-decided path, and they move exactly once — at
settlement — on a decided one.

``tests/regression/test_money_call_sites.py`` cannot catch this class:
it has no opinion about WHICH primitive a call site picked, only that
its result is checked and ledgered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.models.pvp import PvpEscrow, PvpOffer  # noqa: F401 — register tables
from telegram_invite_bot.games.rps import RpsMove
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.duel_service import DuelService, DuelServiceOutcome
from telegram_invite_bot.services.pvp_service import PvpAcceptOutcome, PvpCreateOutcome, PvpService
from telegram_invite_bot.services.rps_service import RpsService, RpsServiceOutcome
from telegram_invite_bot.utils.time import db_now

pytestmark = pytest.mark.asyncio

BET = 100


class _Rng:
    """Deterministic stand-in for ``random``: fixed coin side + dice rolls."""

    def __init__(self, *, choice: str = "heads", rolls: list[int] | None = None) -> None:
        self._choice = choice
        self._rolls = list(rolls or [])

    def choice(self, _seq: object) -> str:
        return self._choice

    def randint(self, _a: int, _b: int) -> int:
        return self._rolls.pop(0)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed(session: AsyncSession, user_id: int, balance: int = 1_000) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
    await session.commit()


async def _totals(session: AsyncSession, user_id: int) -> tuple[int, int]:
    """Return ``(total_spent, total_earned)`` straight off the wallet row."""
    row = (
        await session.execute(
            select(EconomyUser.total_spent, EconomyUser.total_earned).where(
                EconomyUser.user_id == user_id
            )
        )
    ).one()
    return int(row[0]), int(row[1])


async def _balance(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == user_id))
        ).scalar_one()
    )


def _pvp(session: AsyncSession) -> PvpService:
    return PvpService(EconomyRepo(session), TransactionsRepo(session), session)


def _duel(session: AsyncSession) -> DuelService:
    return DuelService(EconomyRepo(session), TransactionsRepo(session))


def _rps(session: AsyncSession) -> RpsService:
    return RpsService(EconomyRepo(session), TransactionsRepo(session))


# ---------------------------------------------------------------------------
# /pvp_coin, /pvp_dice — the non-decided paths must be counter-neutral
# ---------------------------------------------------------------------------


async def test_pvp_create_then_cancel_leaves_counters_frozen(session: AsyncSession) -> None:
    """The headline exploit: create + cancel, no counterparty, no cost."""
    await _seed(session, 1)
    svc = _pvp(session)
    created = await svc.create_offer(
        creator_id=1, game="coin", bet=BET, side="heads", chat_id=-100, now=db_now()
    )
    assert created.outcome is PvpCreateOutcome.OK
    assert created.offer_id is not None
    assert await _totals(session, 1) == (0, 0)  # the hold itself moves nothing

    assert await svc.cancel(offer_id=created.offer_id, creator_id=1) is True
    assert await _balance(session, 1) == 1_000
    assert await _totals(session, 1) == (0, 0)


async def test_pvp_repeated_create_cancel_cannot_inflate_counters(
    session: AsyncSession,
) -> None:
    """The loop the report described — five round trips, still (0, 0)."""
    await _seed(session, 1)
    svc = _pvp(session)
    for _ in range(5):
        created = await svc.create_offer(
            creator_id=1, game="dice", bet=BET, side=None, chat_id=-100, now=db_now()
        )
        assert created.offer_id is not None
        assert await svc.cancel(offer_id=created.offer_id, creator_id=1) is True
    assert await _balance(session, 1) == 1_000
    assert await _totals(session, 1) == (0, 0)


async def test_pvp_expire_leaves_counters_frozen(session: AsyncSession) -> None:
    """A stale offer swept by the cleaner is not a purchase either."""
    await _seed(session, 1)
    svc = _pvp(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=BET, side=None, chat_id=-100, now=db_now()
    )
    assert created.offer_id is not None

    expired = await svc.expire(
        offer_id=created.offer_id,
        cutoff=db_now() + timedelta(minutes=30),
        now=db_now(),
    )
    assert expired is not None
    assert await _balance(session, 1) == 1_000
    assert await _totals(session, 1) == (0, 0)


async def test_pvp_dice_tie_leaves_both_counters_frozen(session: AsyncSession) -> None:
    """A tie refunds both stakes in full — nothing was consumed."""
    await _seed(session, 1)
    await _seed(session, 2)
    svc = _pvp(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=BET, side=None, chat_id=-100, now=db_now()
    )
    assert created.offer_id is not None
    res = await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(rolls=[3, 3]),  # type: ignore[arg-type]
    )
    assert res.outcome is PvpAcceptOutcome.SUCCESS
    assert res.winner_id is None
    assert await _totals(session, 1) == (0, 0)
    assert await _totals(session, 2) == (0, 0)


async def test_pvp_decided_game_settles_both_stakes(session: AsyncSession) -> None:
    """A decided game IS a spend for both seats, and income for the winner.

    Net counters are identical to what the old debit-at-escrow code
    produced for this branch — the change is confined to the paths
    where no game was actually played.
    """
    await _seed(session, 1)
    await _seed(session, 2)
    svc = _pvp(session)
    created = await svc.create_offer(
        creator_id=1, game="coin", bet=BET, side="heads", chat_id=-100, now=db_now()
    )
    assert created.offer_id is not None
    res = await svc.accept_and_resolve(
        offer_id=created.offer_id,
        opponent_id=2,
        chat_id=-100,
        now=db_now(),
        rng=_Rng(choice="heads"),  # type: ignore[arg-type]
    )
    assert res.outcome is PvpAcceptOutcome.SUCCESS
    assert res.winner_id == 1
    assert await _totals(session, 1) == (BET, res.payout)
    assert await _totals(session, 2) == (BET, 0)


# ---------------------------------------------------------------------------
# /duel
# ---------------------------------------------------------------------------


async def test_duel_tie_leaves_counters_frozen(session: AsyncSession) -> None:
    await _seed(session, 42)
    await _seed(session, 99)
    res = await _duel(session).play(
        challenger_id=42, opponent_id=99, challenger_roll=3, opponent_roll=3, bet=BET
    )
    assert res.outcome is DuelServiceOutcome.SUCCESS_TIE
    assert await _totals(session, 42) == (0, 0)
    assert await _totals(session, 99) == (0, 0)


async def test_duel_decided_round_settles_both_stakes(session: AsyncSession) -> None:
    await _seed(session, 42)
    await _seed(session, 99)
    res = await _duel(session).play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=BET
    )
    assert res.outcome is DuelServiceOutcome.SUCCESS_CHALLENGER_WIN
    assert res.round_result is not None
    assert await _totals(session, 42) == (BET, res.round_result.payout)
    assert await _totals(session, 99) == (BET, 0)


async def test_duel_escrow_rollback_leaves_counters_frozen(session: AsyncSession) -> None:
    """The compensating release must not look like a purchase.

    Same drained-opponent race as
    ``test_duel_service.test_opponent_escrow_rollback_via_drained_opponent``:
    the challenger is held, the opponent's SQL hold loses the guard, and
    the challenger's hold is released again. Held-then-released is a
    no-op for the lifetime counters.
    """
    await _seed(session, 42, balance=500)
    await _seed(session, 99, balance=500)

    repo = EconomyRepo(session)
    real_hold = repo.hold
    drained = {"done": False}

    async def racing_hold(user_id: int, amount: int):  # type: ignore[no-untyped-def]
        if user_id == 99 and not drained["done"]:
            await session.execute(
                update(EconomyUser).where(EconomyUser.user_id == 99).values(balance=0)
            )
            drained["done"] = True
        return await real_hold(user_id, amount)

    repo.hold = racing_hold  # type: ignore[method-assign]
    res = await DuelService(repo, TransactionsRepo(session)).play(
        challenger_id=42, opponent_id=99, challenger_roll=4, opponent_roll=3, bet=BET
    )

    assert res.outcome is DuelServiceOutcome.OPPONENT_INSUFFICIENT_FUNDS
    assert await _totals(session, 42) == (0, 0)
    assert await _totals(session, 99) == (0, 0)


# ---------------------------------------------------------------------------
# /cpc
# ---------------------------------------------------------------------------


async def test_rps_tie_leaves_counters_frozen(session: AsyncSession) -> None:
    await _seed(session, 42)
    await _seed(session, 99)
    res = await _rps(session).play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.ROCK,
        bet=BET,
    )
    assert res.outcome is RpsServiceOutcome.SUCCESS_TIE
    assert await _totals(session, 42) == (0, 0)
    assert await _totals(session, 99) == (0, 0)


async def test_rps_decided_round_settles_both_stakes(session: AsyncSession) -> None:
    await _seed(session, 42)
    await _seed(session, 99)
    res = await _rps(session).play(
        challenger_id=42,
        opponent_id=99,
        challenger_move=RpsMove.ROCK,
        opponent_move=RpsMove.SCISSORS,
        bet=BET,
    )
    assert res.outcome is RpsServiceOutcome.SUCCESS_CHALLENGER_WIN
    assert res.round_result is not None
    assert await _totals(session, 42) == (BET, res.round_result.payout)
    assert await _totals(session, 99) == (BET, 0)
