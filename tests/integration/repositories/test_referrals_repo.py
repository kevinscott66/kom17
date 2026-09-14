"""``ReferralsRepo`` — referral graph reads + chain depth (L-37).

Pins:

* ``fetch_invitees`` returns direct invitees ordered by ``user_id`` with
  their wallet balances; non-invitees don't leak in.
* ``fetch_earnings`` sums only ``type='referral'`` credits to the caller —
  other transaction kinds are excluded; empty → 0 (not None).
* ``fetch_inviter`` returns the caller's own inviter; NULL and the legacy
  ``0`` sentinel both normalise to None.
* ``count_second_level`` counts users invited by the caller's invitees
  (the structural second ring); 0 when there's no second level.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import pytest

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.repositories.referrals_repo import ReferralsRepo
from tests.integration.repositories._session import build_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


_CALLER = 1000


@pytest.fixture
async def session(tmp_path) -> AsyncIterator[AsyncSession]:  # noqa: ANN001
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


async def _add_user(
    session: AsyncSession, uid: int, *, balance: int, referred_by: int | None
) -> None:
    session.add(EconomyUser(user_id=uid, balance=balance, referred_by=referred_by))
    await session.flush()


@pytest.mark.asyncio
async def test_fetch_invitees_ordered_with_balances(session: AsyncSession) -> None:
    await _add_user(session, _CALLER, balance=0, referred_by=None)
    await _add_user(session, 30, balance=5, referred_by=_CALLER)
    await _add_user(session, 10, balance=7, referred_by=_CALLER)
    await _add_user(session, 99, balance=1, referred_by=12345)  # someone else's invitee
    repo = ReferralsRepo(session)
    invitees = await repo.fetch_invitees(_CALLER)
    assert [(i.user_id, i.balance) for i in invitees] == [(10, 7), (30, 5)]


@pytest.mark.asyncio
async def test_fetch_earnings_sums_only_referral_type(session: AsyncSession) -> None:
    session.add_all(
        [
            Transaction(to_id=_CALLER, amount=25, type="referral", date=datetime(2026, 1, 1)),
            Transaction(to_id=_CALLER, amount=15, type="referral", date=datetime(2026, 1, 1)),
            Transaction(to_id=_CALLER, amount=999, type="transfer", date=datetime(2026, 1, 1)),
            Transaction(to_id=42, amount=5, type="referral", date=datetime(2026, 1, 1)),
        ]
    )
    await session.flush()
    repo = ReferralsRepo(session)
    assert await repo.fetch_earnings(_CALLER) == 40


@pytest.mark.asyncio
async def test_fetch_earnings_empty_is_zero(session: AsyncSession) -> None:
    repo = ReferralsRepo(session)
    assert await repo.fetch_earnings(_CALLER) == 0


@pytest.mark.asyncio
async def test_fetch_inviter_returns_parent(session: AsyncSession) -> None:
    await _add_user(session, _CALLER, balance=0, referred_by=777)
    repo = ReferralsRepo(session)
    assert await repo.fetch_inviter(_CALLER) == 777


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel", [None, 0])
async def test_fetch_inviter_null_and_zero_normalise_to_none(
    session: AsyncSession, sentinel: int | None
) -> None:
    await _add_user(session, _CALLER, balance=0, referred_by=sentinel)
    repo = ReferralsRepo(session)
    assert await repo.fetch_inviter(_CALLER) is None


@pytest.mark.asyncio
async def test_count_second_level(session: AsyncSession) -> None:
    # caller -> [A, B];  A -> [C, D];  B -> [E].  Second ring = {C, D, E} = 3.
    await _add_user(session, _CALLER, balance=0, referred_by=None)
    await _add_user(session, 1, balance=0, referred_by=_CALLER)  # A
    await _add_user(session, 2, balance=0, referred_by=_CALLER)  # B
    await _add_user(session, 3, balance=0, referred_by=1)  # C
    await _add_user(session, 4, balance=0, referred_by=1)  # D
    await _add_user(session, 5, balance=0, referred_by=2)  # E
    repo = ReferralsRepo(session)
    assert await repo.count_second_level(_CALLER) == 3


@pytest.mark.asyncio
async def test_count_second_level_zero_when_no_grandchildren(session: AsyncSession) -> None:
    await _add_user(session, _CALLER, balance=0, referred_by=None)
    await _add_user(session, 1, balance=0, referred_by=_CALLER)
    repo = ReferralsRepo(session)
    assert await repo.count_second_level(_CALLER) == 0


@pytest.mark.asyncio
async def test_count_invitees_matches_the_list_without_fetching_it(
    session: AsyncSession,
) -> None:
    """The profile social panel prints only the number, so it counts in SQL.
    The count must agree with ``fetch_invitees`` — including the exclusion
    of second-ring users, which are invited by an invitee, not by us.
    """
    await _add_user(session, _CALLER, balance=0, referred_by=None)
    await _add_user(session, 1, balance=10, referred_by=_CALLER)
    await _add_user(session, 2, balance=20, referred_by=_CALLER)
    await _add_user(session, 3, balance=30, referred_by=1)  # second ring
    repo = ReferralsRepo(session)
    assert await repo.count_invitees(_CALLER) == 2
    assert await repo.count_invitees(_CALLER) == len(await repo.fetch_invitees(_CALLER))


@pytest.mark.asyncio
async def test_count_invitees_zero_for_a_leaf_user(session: AsyncSession) -> None:
    await _add_user(session, _CALLER, balance=0, referred_by=None)
    assert await ReferralsRepo(session).count_invitees(_CALLER) == 0
