"""``PrivilegesRepo`` against a real SQLite ``economy.db`` copy.

Pinned behaviour:

* expired rows return None from ``get_active`` without being deleted
  (read-side stays read-only — bulk-cleanup is a separate method)
* ``expires_at <= 0`` is the "never expires" sentinel (legacy parity)
* group-scoped rows do NOT bleed into global lookups
* ``remove`` is idempotent and reports rowcount via a bool
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import UserPrivilege
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from tests.integration.repositories._session import build_session

RepoFixture = tuple[PrivilegesRepo, AsyncSession]

# #1950 gave ``grant_with_value`` a ``now`` + ``duration`` signature (it
# has to know where the window starts to be able to extend one). These
# tests only ever cared about the resulting expiry, so they pin one
# instant and derive each duration from the expiry they already used.
_GRANT_NOW = datetime(2024, 6, 1, tzinfo=UTC)


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield PrivilegesRepo(session), session


async def _seed(
    session: AsyncSession,
    *,
    user_id: int,
    priv_type: str,
    group_id: int = 0,
    expires_at: float = 0.0,
    value: str | None = None,
) -> None:
    session.add(
        UserPrivilege(
            user_id=user_id,
            privilege_type=priv_type,
            group_id=group_id,
            expires_at=expires_at,
            value=value,
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# get_active
# ---------------------------------------------------------------------------


async def test_get_active_returns_never_expiring_row(repo: RepoFixture) -> None:
    privileges_repo, session = repo
    await _seed(session, user_id=42, priv_type="double_daily")
    now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

    row = await privileges_repo.get_active(42, "double_daily", now=now)

    assert row is not None
    assert row.user_id == 42
    assert row.privilege_type == "double_daily"
    assert row.expires_at == 0.0


async def test_get_active_returns_none_for_expired(repo: RepoFixture) -> None:
    """Past-deadline row exists but is invisible to readers. The
    read-side intentionally does NOT delete — bulk cleanup is a
    separate method so /daily's hot path doesn't pay for a write
    every time a buster expires."""
    privileges_repo, session = repo
    past = (datetime(2024, 1, 1, tzinfo=UTC)).timestamp()
    await _seed(session, user_id=42, priv_type="double_daily", expires_at=past)

    now = datetime(2024, 6, 15, tzinfo=UTC)
    assert await privileges_repo.get_active(42, "double_daily", now=now) is None

    # Row still in DB — proving read-side is read-only.
    raw = await session.get(UserPrivilege, (42, "double_daily", 0))
    assert raw is not None


async def test_get_active_returns_row_just_before_expiry(repo: RepoFixture) -> None:
    """``expires_at <= now.timestamp()`` is the cutoff — strictly less
    than the deadline means the row is still active. Pinning the
    boundary so a 1-second tick doesn't flip behaviour twice."""
    privileges_repo, session = repo
    deadline = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
    await _seed(session, user_id=42, priv_type="double_daily", expires_at=deadline.timestamp())

    before = deadline - timedelta(seconds=1)
    assert await privileges_repo.get_active(42, "double_daily", now=before) is not None
    # AT the deadline → already expired (matches legacy ``<= time.time()``).
    assert await privileges_repo.get_active(42, "double_daily", now=deadline) is None


async def test_get_active_does_not_bleed_across_group_scopes(
    repo: RepoFixture,
) -> None:
    """A group-scoped grant (group_id > 0) must NOT satisfy a default
    global lookup (group_id=0). Different chats hold isolated state."""
    privileges_repo, session = repo
    await _seed(session, user_id=42, priv_type="color_nick", group_id=-1001)

    now = datetime(2024, 6, 15, tzinfo=UTC)
    # Default global lookup misses.
    assert await privileges_repo.get_active(42, "color_nick", now=now) is None
    # Explicit group-scoped lookup finds it.
    assert await privileges_repo.get_active(42, "color_nick", now=now, group_id=-1001) is not None


async def test_get_active_missing_returns_none(repo: RepoFixture) -> None:
    privileges_repo, _ = repo
    now = datetime(2024, 6, 15, tzinfo=UTC)
    assert await privileges_repo.get_active(99999, "double_daily", now=now) is None


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


async def test_remove_consumes_existing_row(repo: RepoFixture) -> None:
    privileges_repo, session = repo
    await _seed(session, user_id=42, priv_type="double_daily")

    deleted = await privileges_repo.remove(42, "double_daily")
    await session.commit()

    assert deleted is True
    now = datetime(2024, 6, 15, tzinfo=UTC)
    assert await privileges_repo.get_active(42, "double_daily", now=now) is None


async def test_remove_missing_row_is_idempotent_no_op(repo: RepoFixture) -> None:
    """Consuming a buster that's already gone (race, double-call)
    must NOT error. Returns False so the caller can distinguish
    "I just consumed it" from "it wasn't there"."""
    privileges_repo, session = repo
    deleted = await privileges_repo.remove(99999, "double_daily")
    await session.commit()
    assert deleted is False


async def test_remove_does_not_touch_other_scopes(repo: RepoFixture) -> None:
    """A global ``remove(group_id=0)`` must NOT delete a group-scoped
    row — would silently drop a per-chat grant on a global cleanup."""
    privileges_repo, session = repo
    await _seed(session, user_id=42, priv_type="color_nick", group_id=-1001)

    deleted = await privileges_repo.remove(42, "color_nick")  # group_id=0 default
    await session.commit()
    assert deleted is False
    # Group-scoped row untouched.
    now = datetime(2024, 6, 15, tzinfo=UTC)
    assert await privileges_repo.get_active(42, "color_nick", now=now, group_id=-1001) is not None


# ---------------------------------------------------------------------------
# delete_expired
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# grant_buster (Stage 28 writer)
# ---------------------------------------------------------------------------


async def test_grant_buster_creates_row_when_absent(repo: RepoFixture) -> None:
    privileges_repo, session = repo
    expires_at = datetime(2024, 12, 31, tzinfo=UTC)

    await privileges_repo.grant_buster(
        user_id=42, privilege_type="double_daily", expires_at=expires_at
    )
    await session.commit()

    row = await session.get(UserPrivilege, (42, "double_daily", 0))
    assert row is not None
    assert row.expires_at == expires_at.timestamp()


async def test_grant_buster_does_not_shorten_existing_longer_ttl(
    repo: RepoFixture,
) -> None:
    """MAX semantics mirror VipRepo.grant_global: an extension that
    proposes a SHORTER expiry must leave the existing longer expiry
    intact. Today's planner ships only 3-day busters so the case is
    theoretical, but the SQL has to be right for the next privilege
    kind the planner learns about (xp_boost, mute_protection)."""
    privileges_repo, session = repo
    far = datetime(2025, 6, 1, tzinfo=UTC)
    near = datetime(2024, 7, 1, tzinfo=UTC)
    await _seed(session, user_id=42, priv_type="double_daily", expires_at=far.timestamp())

    await privileges_repo.grant_buster(user_id=42, privilege_type="double_daily", expires_at=near)
    await session.commit()

    row = await session.get(UserPrivilege, (42, "double_daily", 0))
    assert row is not None
    assert row.expires_at == far.timestamp()


async def test_grant_buster_extends_when_new_ttl_is_longer(repo: RepoFixture) -> None:
    privileges_repo, session = repo
    near = datetime(2024, 7, 1, tzinfo=UTC)
    far = datetime(2025, 6, 1, tzinfo=UTC)
    await _seed(session, user_id=42, priv_type="double_daily", expires_at=near.timestamp())

    await privileges_repo.grant_buster(user_id=42, privilege_type="double_daily", expires_at=far)
    await session.commit()

    row = await session.get(UserPrivilege, (42, "double_daily", 0))
    assert row is not None
    assert row.expires_at == far.timestamp()


# ---------------------------------------------------------------------------
# grant_with_value (Stage 30 writer for COLOR_NICK & future payload kinds)
# ---------------------------------------------------------------------------


async def test_grant_with_value_creates_row_when_absent(repo: RepoFixture) -> None:
    privileges_repo, session = repo
    expires_at = datetime(2024, 12, 31, tzinfo=UTC)

    await privileges_repo.grant_with_value(
        user_id=42,
        privilege_type="color_nick",
        value='{"color": "rainbow"}',
        now=_GRANT_NOW,
        duration=expires_at - _GRANT_NOW,
    )
    await session.commit()

    row = await session.get(UserPrivilege, (42, "color_nick", 0))
    assert row is not None
    assert row.expires_at == expires_at.timestamp()
    assert row.value == '{"color": "rainbow"}'


async def test_grant_with_value_replaces_existing_row_even_if_shorter(
    repo: RepoFixture,
) -> None:
    """REPLACE (not MAX) semantics — pinning the divergence from
    :meth:`grant_buster`. A fresh 7-day color_nick on top of an
    existing 30-day one SHORTENS the active window: the user just
    activated a different color, the bot must surface the new color
    even though the old window outlived it. Matches legacy
    ``set_privilege`` (``bot.py:13272``)'s unconditional UPSERT
    posture exactly."""
    privileges_repo, session = repo
    far = datetime(2025, 6, 1, tzinfo=UTC)
    near = datetime(2024, 7, 1, tzinfo=UTC)
    await _seed(
        session,
        user_id=42,
        priv_type="color_nick",
        expires_at=far.timestamp(),
        value='{"color": "red"}',
    )

    await privileges_repo.grant_with_value(
        user_id=42,
        privilege_type="color_nick",
        value='{"color": "blue"}',
        now=_GRANT_NOW,
        duration=near - _GRANT_NOW,
    )
    await session.commit()

    row = await session.get(UserPrivilege, (42, "color_nick", 0))
    assert row is not None
    # REPLACE: the new (shorter) expiry won — opposite of grant_buster's MAX.
    assert row.expires_at == near.timestamp()
    # And the new color payload won too — that's the whole point of
    # value-carrying replace over presence-only MAX.
    assert row.value == '{"color": "blue"}'


async def test_grant_with_value_does_not_bleed_across_group_scopes(
    repo: RepoFixture,
) -> None:
    """Default group_id=0 (global) write must NOT collide with a
    group-scoped row — different PK slot under the composite key."""
    privileges_repo, session = repo
    await _seed(
        session,
        user_id=42,
        priv_type="color_nick",
        group_id=-1001,
        expires_at=datetime(2025, 1, 1, tzinfo=UTC).timestamp(),
        value='{"color": "red"}',
    )

    await privileges_repo.grant_with_value(
        user_id=42,
        privilege_type="color_nick",
        value='{"color": "blue"}',
        now=_GRANT_NOW,
        duration=datetime(2024, 7, 1, tzinfo=UTC) - _GRANT_NOW,
    )
    await session.commit()

    # Global row freshly written.
    global_row = await session.get(UserPrivilege, (42, "color_nick", 0))
    assert global_row is not None
    assert global_row.value == '{"color": "blue"}'
    # Group-scoped row untouched.
    group_row = await session.get(UserPrivilege, (42, "color_nick", -1001))
    assert group_row is not None
    assert group_row.value == '{"color": "red"}'


async def test_delete_expired_prunes_past_deadline_only(repo: RepoFixture) -> None:
    """Bulk cleanup: removes expired rows, leaves active and
    never-expires rows alone. Returns the count deleted."""
    privileges_repo, session = repo
    past = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    future = datetime(2024, 12, 31, tzinfo=UTC).timestamp()

    await _seed(session, user_id=1, priv_type="double_daily", expires_at=past)
    await _seed(session, user_id=2, priv_type="color_nick", expires_at=future)
    await _seed(session, user_id=3, priv_type="legend")  # never expires

    now = datetime(2024, 6, 15, tzinfo=UTC)
    deleted = await privileges_repo.delete_expired(now=now)
    await session.commit()

    assert deleted == 1
    # Active + never-expires survive.
    assert await privileges_repo.get_active(2, "color_nick", now=now) is not None
    assert await privileges_repo.get_active(3, "legend", now=now) is not None


async def test_naive_now_does_not_extend_an_expired_grant(
    repo: RepoFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A naive ``now`` must not buy the grant three extra hours.

    The regression: the message-reward path passed ``db_now()`` (naive
    UTC) here. ``.timestamp()`` on a naive value reads the wall clock in
    the HOST's zone, so on the MSK production host the deadline landed
    10 800 s in the past and an expired xp_boost kept doubling earnings.
    ``unix_ts`` coerces the frame and logs, so the row still reads as
    expired. ``TZ`` is pinned so this also fails on a UTC CI box.
    """
    privileges_repo, session = repo
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        aware = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
        # Lapsed one second ago, in real time.
        await _seed(
            session,
            user_id=42,
            priv_type="xp_boost",
            expires_at=(aware - timedelta(seconds=1)).timestamp(),
        )
        naive = aware.replace(tzinfo=None)

        records: list[str] = []
        handler_id = logger.add(records.append, level="ERROR", format="{message}")
        try:
            assert await privileges_repo.get_active(42, "xp_boost", now=naive) is None
        finally:
            logger.remove(handler_id)
        assert len(records) == 1, records
        assert "PrivilegesRepo.get_active" in records[0]

        # And an actually-live grant is still returned through the guard.
        await privileges_repo.remove(42, "xp_boost")
        await session.commit()
        await _seed(
            session,
            user_id=42,
            priv_type="xp_boost",
            expires_at=(aware + timedelta(hours=1)).timestamp(),
        )
        assert await privileges_repo.get_active(42, "xp_boost", now=naive) is not None
    finally:
        monkeypatch.undo()
        time.tzset()
