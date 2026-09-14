"""#1949: three constraints prod has and ``create_all`` did not.

Read off the live ``economy.db``, prod declares
``check_claims.check_id REFERENCES checks(id)`` and
``pvp_escrow.offer_id REFERENCES pvp_offers(id) ON DELETE CASCADE``.
Neither table is created by a migration — both predate the port, so on
prod they carry legacy's own DDL — and neither foreign key was declared
on the models, so every database built by ``create_all`` (tests, a fresh
deploy, a rebuild from a dump) was laxer than the one production runs
on. ``db/models/pvp.py`` even said so in a comment, and named a third
divergence in the same breath — prod declares ``pvp_escrow.created_at``
NOT NULL — closing with "a write that passes in CI can still be rejected
there".

That is the wrong direction for a guard to point. A test suite is worth
having because it fails on what production would reject; a schema that
accepts an orphan row locally proves nothing about the row prod would
refuse. The models now declare both, and this file pins the behaviour
rather than the spelling — an assertion on ``__table_args__`` would pass
on a database where SQLite never enforces it.

Which is the second half of the guarantee: SQLite defaults
``foreign_keys`` to OFF, so the declaration only bites because
:func:`db.pragma.apply_pragmas` turns it on for every connection. These
tests go through that same function for exactly that reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.economy import Check, CheckClaim, EconomyBase
from telegram_invite_bot.db.models.pvp import PvpEscrow, PvpOffer
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.pragma import apply_pragmas

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """An economy schema built by ``create_all``, pragmas and all."""
    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(connection: Any, _record: Any) -> None:
        apply_pragmas(connection, DBName.ECONOMY)

    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as opened:
        yield opened
    await engine.dispose()


def _check(check_id: int) -> Check:
    return Check(
        id=check_id,
        code=f"CODE{check_id}",
        creator_id=1,
        type="fixed",
        total_amount=100,
        remaining_amount=100,
        fixed_amount=50,
        created_at=_NOW,
    )


def _offer(offer_id: int) -> PvpOffer:
    return PvpOffer(
        id=offer_id,
        game="coin",
        status="pending",
        chat_id=-1001,
        creator_id=7001,
        bet=50,
        params_json="{}",
        created_at=_NOW,
    )


async def test_a_claim_against_a_check_that_does_not_exist_is_refused(
    session: AsyncSession,
) -> None:
    """The row prod would reject must not be accepted here either."""
    session.add(CheckClaim(check_id=4242, user_id=555, amount=50, claimed_at=_NOW))

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        await session.flush()


async def test_a_claim_against_a_real_check_still_writes(
    session: AsyncSession,
) -> None:
    """The control: the constraint refuses orphans, not claims."""
    session.add(_check(1))
    await session.flush()
    session.add(CheckClaim(check_id=1, user_id=555, amount=50, claimed_at=_NOW))
    await session.commit()

    rows = (await session.execute(sa.text("SELECT check_id FROM check_claims"))).all()
    assert [row[0] for row in rows] == [1]


async def test_a_hold_against_an_offer_that_does_not_exist_is_refused(
    session: AsyncSession,
) -> None:
    """``status='held'`` is meaningless without the offer it funds."""
    session.add(PvpEscrow(offer_id=4242, user_id=7001, amount=50, status="held", created_at=_NOW))

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        await session.flush()


async def test_deleting_an_offer_takes_its_holds_with_it(
    session: AsyncSession,
) -> None:
    """``ON DELETE CASCADE``, as prod declares it.

    Without the cascade a deleted offer leaves both seats' holds behind
    as rows nothing can ever release or refund — and
    ``EconomyBase.metadata`` had no cascade to apply.
    """
    session.add(_offer(1))
    await session.flush()
    session.add_all(
        [
            PvpEscrow(offer_id=1, user_id=7001, amount=50, status="held", created_at=_NOW),
            PvpEscrow(offer_id=1, user_id=8002, amount=50, status="held", created_at=_NOW),
        ]
    )
    await session.commit()

    await session.execute(sa.text("DELETE FROM pvp_offers WHERE id = 1"))
    await session.commit()

    left = (await session.execute(sa.text("SELECT COUNT(*) FROM pvp_escrow"))).scalar_one()
    assert left == 0


async def test_a_hold_with_no_timestamp_is_refused(session: AsyncSession) -> None:
    """The third divergence the model comment named.

    Both writers (``PvpRepo.hold_escrow`` and ``create_offer``) already
    pass ``now``, so this constrains nothing they do — it stops a
    future writer from passing CI on a row prod would reject.
    """
    session.add(_offer(1))
    await session.flush()
    session.add(PvpEscrow(offer_id=1, user_id=7001, amount=50, status="held"))

    with pytest.raises(IntegrityError, match="NOT NULL"):
        await session.flush()
