"""#1950: a repeat grant of the SAME privilege payload must ADD, not replace.

``PrivilegesRepo.grant_with_value`` used to be an unconditional UPSERT,
mirroring legacy ``PrivilegeManager.set_privilege``. That is right when
the payload changes — the user picked a different color and expects to
see it — but wrong when it does not: redeeming a second identical item
is a repeat PURCHASE of duration, and replacing the window turned it
into a partial refund of itself.

Measured against the live catalog: prod sells ONE row per
payload-carrying type (``🔇 Защита от мута``, 24 h, 1500; ``⚡ Ускорение``,
60 min / x2, 2500), so the payload always matches and every repeat
redemption used to lose whatever was left of the first window. A user
who redeems the second ``⚡ Ускорение`` ten minutes into the first paid
2500 for ten minutes.

This is #192's shape (VIP made additive because MAX turned a repeat
purchase into a paid no-op) and #1903's (a second ``double_daily``
burned for nothing), one branch over. The three fixes differ only in
what the schema can express: VIP has a scalar deadline, so it adds; the
buster is presence-only with ONE expiry and a once-a-day spender, so it
refuses without consuming; these rows have a payload, so they add when
it matches and replace when it does not.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import UserPrivilege
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from tests.integration.repositories._session import build_session

RepoFixture = tuple[PrivilegesRepo, AsyncSession]

# Naive LOCAL, matching the column's contract (see ``grant_with_value``
# and ``delete_expired``): the epoch arithmetic happens inside SQLite.
_T0 = datetime(2026, 9, 9, 12, 0, 0)
_USER = 42


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield PrivilegesRepo(session), session


async def _row(session: AsyncSession, priv: str, group_id: int = 0) -> UserPrivilege:
    row = await session.get(UserPrivilege, (_USER, priv, group_id))
    assert row is not None
    return row


async def test_a_second_mute_protection_adds_its_full_window(repo: RepoFixture) -> None:
    """Two 24 h items are 48 h of cover, not 25."""
    privileges_repo, session = repo
    day = timedelta(hours=24)

    await privileges_repo.grant_with_value(
        user_id=_USER, privilege_type="mute_protection", value="{}", now=_T0, duration=day
    )
    await session.commit()
    granted = await privileges_repo.grant_with_value(
        user_id=_USER,
        privilege_type="mute_protection",
        value="{}",
        now=_T0 + timedelta(hours=1),
        duration=day,
    )
    await session.commit()

    expected = _T0 + day + day
    assert granted == expected
    assert (await _row(session, "mute_protection")).expires_at == expected.timestamp()


async def test_a_second_xp_boost_of_the_same_multiplier_adds(repo: RepoFixture) -> None:
    """The prod case: 2500 coins used to buy ten minutes."""
    privileges_repo, session = repo
    hour = timedelta(minutes=60)
    payload = '{"multiplier": 2}'

    await privileges_repo.grant_with_value(
        user_id=_USER, privilege_type="xp_boost", value=payload, now=_T0, duration=hour
    )
    await session.commit()
    await privileges_repo.grant_with_value(
        user_id=_USER,
        privilege_type="xp_boost",
        value=payload,
        now=_T0 + timedelta(minutes=10),
        duration=hour,
    )
    await session.commit()

    assert (await _row(session, "xp_boost")).expires_at == (_T0 + hour + hour).timestamp()


async def test_a_different_payload_still_replaces(repo: RepoFixture) -> None:
    """The half that must NOT change: a new color wins even if shorter.

    Keeping the longer window here would leave the user looking at the
    color they just replaced — the reason REPLACE was chosen in the
    first place, and still the right answer for a payload that carries
    a user-visible choice.
    """
    privileges_repo, session = repo

    await privileges_repo.grant_with_value(
        user_id=_USER,
        privilege_type="color_nick",
        value='{"color": "red"}',
        now=_T0,
        duration=timedelta(days=30),
    )
    await session.commit()
    await privileges_repo.grant_with_value(
        user_id=_USER,
        privilege_type="color_nick",
        value='{"color": "blue"}',
        now=_T0,
        duration=timedelta(days=7),
    )
    await session.commit()

    row = await _row(session, "color_nick")
    assert row.value == '{"color": "blue"}'
    assert row.expires_at == (_T0 + timedelta(days=7)).timestamp()


async def test_an_expired_window_restarts_from_now(repo: RepoFixture) -> None:
    """Adding onto a stale expiry would land the new window in the past.

    The ``MAX(existing, now)`` clamp is what stops that — the same clamp
    :meth:`VipRepo.grant_global` carries for the same reason.
    """
    privileges_repo, session = repo
    day = timedelta(hours=24)

    await privileges_repo.grant_with_value(
        user_id=_USER, privilege_type="mute_protection", value="{}", now=_T0, duration=day
    )
    await session.commit()
    # A month later — the first window lapsed long ago.
    much_later = _T0 + timedelta(days=30)
    granted = await privileges_repo.grant_with_value(
        user_id=_USER,
        privilege_type="mute_protection",
        value="{}",
        now=much_later,
        duration=day,
    )
    await session.commit()

    assert granted == much_later + day


async def test_a_never_expiring_row_stays_never_expiring(repo: RepoFixture) -> None:
    """``expires_at <= 0`` is legacy's "no deadline" sentinel.

    Adding a duration to it would DOWNGRADE an unlimited grant to a
    timed one — the single input where "add" takes something away. This
    pipeline never writes the sentinel, but legacy did, so a row from
    before the port can still carry it.
    """
    privileges_repo, session = repo
    session.add(
        UserPrivilege(
            user_id=_USER,
            privilege_type="mute_protection",
            group_id=0,
            expires_at=0.0,
            value="{}",
        )
    )
    await session.commit()

    granted = await privileges_repo.grant_with_value(
        user_id=_USER,
        privilege_type="mute_protection",
        value="{}",
        now=_T0,
        duration=timedelta(hours=24),
    )
    await session.commit()

    assert (await _row(session, "mute_protection")).expires_at == 0.0
    assert granted == datetime.fromtimestamp(0.0)  # noqa: DTZ006 — naive local, as stored


async def test_extending_does_not_reach_across_group_scopes(repo: RepoFixture) -> None:
    """A global grant must not be extended by (or extend) a chat-scoped one."""
    privileges_repo, session = repo
    day = timedelta(hours=24)

    await privileges_repo.grant_with_value(
        user_id=_USER,
        privilege_type="mute_protection",
        value="{}",
        now=_T0,
        duration=day,
        group_id=-1001,
    )
    await session.commit()
    granted = await privileges_repo.grant_with_value(
        user_id=_USER, privilege_type="mute_protection", value="{}", now=_T0, duration=day
    )
    await session.commit()

    # Fresh global row: one day, not two.
    assert granted == _T0 + day
    assert (await _row(session, "mute_protection", -1001)).expires_at == (_T0 + day).timestamp()
