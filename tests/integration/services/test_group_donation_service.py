"""``GroupDonationService`` — the group slice of a purchase (#254).

This module exists because the service had no tests at all. It moves
real money on every commissionable purchase: it mints ``percent`` of the
price, hands most of it to the group owner and the rest to the
developer, then rewrites the rating board. The only coverage was
indirect, through handler e2e tests that assert on the reply card.

The pins here are deliberately about the LEDGER rather than the reply,
because that is where the bug lived: the owner slice and the developer
fee were both minted (``economy.credit``) yet written to
``economy.transactions`` as ``from_id=buyer_id`` — a debit against
someone whose wallet the call never touched.

``rating_history`` ships in migration ``0009_donations_rating_writeside``
and has no ORM model, so the fixture adds it with raw DDL after
``create_all`` — same split as
``tests/integration/repositories/test_donations_rating_repo.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.repositories.donations_rating_repo import DonationsRatingRepo
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.group_donation_service import (
    GroupDonationOutcome,
    GroupDonationService,
)

BUYER = 42
OWNER = 7
DEVELOPER = 999
GROUP = -100_500

_RATING_HISTORY_DDL = (
    "CREATE TABLE rating_history ("
    "  group_id INTEGER NOT NULL,"
    "  date TEXT NOT NULL,"
    "  total_donations INTEGER NOT NULL,"
    "  position INTEGER,"
    "  PRIMARY KEY (group_id, date)"
    ")"
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(EconomyBase.metadata.create_all)
            await conn.execute(text(_RATING_HISTORY_DDL))
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as s:
            yield s
    finally:
        await engine.dispose()


def _build_service(
    session: AsyncSession,
    *,
    percent: int = 15,
    developer_percent: int = 15,
    developer_id: int = DEVELOPER,
) -> GroupDonationService:
    return GroupDonationService(
        DonationsRatingRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
        percent=percent,
        developer_percent=developer_percent,
        developer_id=developer_id,
    )


async def _seed_wallet(session: AsyncSession, user_id: int, balance: int = 0) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
    await session.commit()


async def _balance(session: AsyncSession, user_id: int) -> int:
    row = (
        await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == user_id))
    ).scalar_one_or_none()
    return int(row) if row is not None else -1


async def _rows(session: AsyncSession) -> list[tuple[int | None, int | None, int, str]]:
    result = await session.execute(
        select(
            Transaction.from_id,
            Transaction.to_id,
            Transaction.amount,
            Transaction.type,
        ).order_by(Transaction.id)
    )
    return [(f, to, int(a), t) for f, to, a, t in result.all()]


async def test_route_splits_the_slice_between_owner_and_developer(
    session: AsyncSession,
) -> None:
    """1000 × 15% = 150 to the group; 15% of that (22) is the fee."""
    await _seed_wallet(session, OWNER)
    await _seed_wallet(session, DEVELOPER)
    service = _build_service(session)

    result = await service.route(group_id=GROUP, user_id=BUYER, price=1_000, owner_id=OWNER)
    await session.commit()

    assert result.outcome is GroupDonationOutcome.ROUTED
    assert result.amount == 150
    assert result.fee == 22
    assert result.to_owner == 128
    assert await _balance(session, OWNER) == 128
    assert await _balance(session, DEVELOPER) == 22


async def test_minted_payouts_are_not_written_as_the_buyer_debiting(
    session: AsyncSession,
) -> None:
    """The #254 guard.

    Both rows used to carry ``from_id=BUYER``. The coins are minted, so
    that made the ledger claim the buyer paid 150 coins on top of the
    1000 the shop had already burned — and ``window_stats`` / ``recent``
    both read ``from_id`` with no type filter, so he saw it.
    """
    await _seed_wallet(session, OWNER)
    await _seed_wallet(session, DEVELOPER)
    service = _build_service(session)

    await service.route(group_id=GROUP, user_id=BUYER, price=1_000, owner_id=OWNER)
    await session.commit()

    assert [(f, to, amount) for f, to, amount, _ in await _rows(session)] == [
        (None, OWNER, 128),
        (None, DEVELOPER, 22),
    ]

    ledger = TransactionsRepo(session)
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
    assert (await ledger.window_stats(BUYER, since=since)).sent == 0
    assert await ledger.recent(BUYER) == []


async def test_no_owner_skips_both_payouts_but_still_ranks_the_group(
    session: AsyncSession,
) -> None:
    """A group nobody owns still gets its xp — there is just no payee."""
    service = _build_service(session)

    result = await service.route(group_id=GROUP, user_id=BUYER, price=1_000, owner_id=None)
    await session.commit()

    assert result.outcome is GroupDonationOutcome.ROUTED_NO_OWNER
    assert result.amount == 150
    assert result.to_owner == 0
    assert await _rows(session) == []


async def test_zero_percent_disables_the_route_entirely(
    session: AsyncSession,
) -> None:
    service = _build_service(session, percent=0)

    result = await service.route(group_id=GROUP, user_id=BUYER, price=1_000, owner_id=OWNER)

    assert result.outcome is GroupDonationOutcome.DISABLED
    assert await _rows(session) == []


async def test_one_coin_slice_pays_the_fee_and_leaves_the_owner_nothing(
    session: AsyncSession,
) -> None:
    """The floor case the ``_credit`` docstring calls out.

    ``purchase_commission_amount`` floors at 1, so a 1-coin slice yields
    a 1-coin fee and 0 for the owner. The owner credit is a normal skip,
    not a failure — but the outcome must then report NO_OWNER, because
    nothing actually landed in his wallet.
    """
    await _seed_wallet(session, OWNER)
    await _seed_wallet(session, DEVELOPER)
    service = _build_service(session, percent=1)

    result = await service.route(group_id=GROUP, user_id=BUYER, price=1, owner_id=OWNER)
    await session.commit()

    assert result.amount == 1
    assert result.fee == 1
    assert result.owner_credited is False
    assert result.outcome is GroupDonationOutcome.ROUTED_NO_OWNER
    assert await _balance(session, OWNER) == 0
    assert [(f, to, amount) for f, to, amount, _ in await _rows(session)] == [(None, DEVELOPER, 1)]


async def test_route_failure_leaves_the_outer_transaction_usable(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure part-way through the slice must undo the whole slice.

    ``handlers/shop.py`` swallows any exception out of :meth:`route` so a
    bonus can never void a buy, and the purchase commits afterwards on
    the same session. Catching alone does not make the routing atomic:
    the writes that already landed ride that commit out. Measured
    without the savepoint, the owner keeps his 128-coin payout and the
    ledger keeps its rows while ``recalc_positions`` never ran — so the
    money moved but the board it was supposed to buy a place on is
    stale, and nothing in the receipt or the log says so. A deactivated
    transaction (a failed flush rather than a failed statement) is the
    worse shape of the same hole: there the caller's commit takes the
    purchase down with it. The SAVEPOINT closes both.

    Pinned here rather than in the handler test because the handler
    cannot see the difference: it swallows the exception either way.
    """
    await _seed_wallet(session, OWNER)
    donations = DonationsRatingRepo(session)
    service = GroupDonationService(
        donations,
        EconomyRepo(session),
        TransactionsRepo(session),
        percent=15,
        developer_percent=15,
        developer_id=DEVELOPER,
    )

    async def _boom() -> int:
        await session.execute(text("SELECT * FROM table_that_does_not_exist"))
        return 0

    monkeypatch.setattr(donations, "recalc_positions", _boom)

    with pytest.raises(OperationalError):
        await service.route(group_id=GROUP, user_id=BUYER, price=1_000, owner_id=OWNER)

    # This stands in for the middleware commit that carries the purchase.
    await session.commit()
    # Everything the slice wrote is gone; nothing else was.
    assert await _balance(session, OWNER) == 0
    assert await _rows(session) == []


async def test_a_failed_owner_payout_does_not_pay_the_developer_fee(
    session: AsyncSession,
) -> None:
    """#1401 — a commission is a cut of a payout, not a toll on the buy.

    The owner's wallet sits at the ``_MAX_AMOUNT`` ceiling, so the
    write-time cap in ``EconomyRepo.credit`` refuses his 128 coins and
    the service reports NO_OWNER. Before the fix the developer was
    credited his 22 anyway: the fee branch asked only whether a
    developer id was configured, so the ecosystem minted a commission on
    a payout that never happened, on a slice the owner never saw.

    Distinct from ``test_one_coin_slice_pays_the_fee…`` above, which is
    the OTHER falsy ``owner_credited``: there the payout is legitimately
    zero and the fee is still owed.
    """
    await _seed_wallet(session, OWNER, balance=10**15)
    await _seed_wallet(session, DEVELOPER)
    service = _build_service(session)

    result = await service.route(group_id=GROUP, user_id=BUYER, price=1_000, owner_id=OWNER)
    await session.commit()

    assert result.owner_credited is False
    assert result.outcome is GroupDonationOutcome.ROUTED_NO_OWNER
    # ``fee`` stays the COMPUTED split — the dataclass docstring's
    # contract — and is now the only trace that one was ever due.
    assert result.fee == 22
    assert result.to_owner == 0
    assert await _balance(session, DEVELOPER) == 0
    assert await _balance(session, OWNER) == 10**15
    # The xp still landed; only the coin half of the slice is absent.
    assert await _rows(session) == []
