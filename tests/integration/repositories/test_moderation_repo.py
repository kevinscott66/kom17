"""Real-SQLite tests for :class:`ModerationRepo`.

T-020 invariants:

* ``add_warning`` inserts a row with ``active=True`` and returns a PK.
* ``get_warning_count`` counts only active, non-expired rows.
* ``list_warnings`` returns rows ordered most-recent first.
* ``remove_last_warning`` soft-deletes the most-recent active warning.
* ``remove_last_warning`` returns ``False`` when no active warning exists.
* ``record_action`` appends to ``moderation_log`` without raising.
* Expired warnings are excluded from count and list.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.db.models.moderation import ModerationLog, Warning
from telegram_invite_bot.repositories.moderation_repo import ModerationRepo
from tests.integration.repositories._session import build_session


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, ModerationBase, "moderation.db") as s:
        yield s


# ---------------------------------------------------------------------------
# add_warning
# ---------------------------------------------------------------------------


async def test_add_warning_returns_pk(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    warning_id, count = await repo.add_warning(
        user_id=10,
        chat_id=-100,
        admin_id=1,
        reason="Test reason",
    )
    assert warning_id > 0
    # M-M-1: returns post-insert active count alongside the PK.
    assert count == 1


async def test_add_warning_inserts_row(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="spam")
    await session.flush()

    result = await session.execute(
        select(Warning).where(Warning.user_id == 10, Warning.chat_id == -100)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].active is True
    assert rows[0].reason == "spam"


async def test_add_warning_writes_audit_log(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="audit")
    await session.flush()

    result = await session.execute(select(ModerationLog).where(ModerationLog.action == "warn"))
    logs = result.scalars().all()
    assert len(logs) == 1
    assert logs[0].user_id == 10
    assert logs[0].admin_id == 1


# ---------------------------------------------------------------------------
# get_warning_count
# ---------------------------------------------------------------------------


async def test_get_warning_count_zero_initially(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    count = await repo.get_warning_count(user_id=99, chat_id=-100)
    assert count == 0


async def test_get_warning_count_increments(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="a")
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="b")
    await session.flush()
    count = await repo.get_warning_count(user_id=10, chat_id=-100)
    assert count == 2


async def test_get_warning_count_excludes_expired(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    # Insert an already-expired warning directly (expires in the past).
    session.add(
        Warning(
            user_id=10,
            chat_id=-100,
            admin_id=1,
            reason="old",
            date=datetime(2020, 1, 1),
            expires=datetime(2020, 1, 2),  # already expired
            active=True,
        )
    )
    await session.flush()
    count = await repo.get_warning_count(user_id=10, chat_id=-100)
    assert count == 0


async def test_get_warning_count_excludes_inactive(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    session.add(
        Warning(
            user_id=10,
            chat_id=-100,
            admin_id=1,
            reason="deactivated",
            date=datetime(2024, 1, 1),
            active=False,
        )
    )
    await session.flush()
    count = await repo.get_warning_count(user_id=10, chat_id=-100)
    assert count == 0


async def test_get_warning_count_scoped_to_chat(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="chat1")
    await repo.add_warning(user_id=10, chat_id=-200, admin_id=1, reason="chat2")
    await session.flush()
    assert await repo.get_warning_count(user_id=10, chat_id=-100) == 1
    assert await repo.get_warning_count(user_id=10, chat_id=-200) == 1


# ---------------------------------------------------------------------------
# list_warnings
# ---------------------------------------------------------------------------


async def test_list_warnings_empty(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    rows = await repo.list_warnings(user_id=10, chat_id=-100)
    assert rows == []


async def test_list_warnings_ordered_most_recent_first(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="first")
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="second")
    await session.flush()

    rows = await repo.list_warnings(user_id=10, chat_id=-100)
    assert len(rows) == 2
    # Most-recent first
    assert rows[0].reason == "second"
    assert rows[1].reason == "first"


async def test_list_warnings_excludes_inactive(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="active")
    await session.flush()
    # Soft-delete it
    await repo.remove_last_warning(user_id=10, chat_id=-100, admin_id=1)
    await session.flush()

    rows = await repo.list_warnings(user_id=10, chat_id=-100)
    assert rows == []


# ---------------------------------------------------------------------------
# remove_last_warning
# ---------------------------------------------------------------------------


async def test_remove_last_warning_returns_false_when_none(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    result = await repo.remove_last_warning(user_id=10, chat_id=-100, admin_id=1)
    assert result is False


async def test_remove_last_warning_happy_path(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="x")
    await session.flush()

    removed = await repo.remove_last_warning(user_id=10, chat_id=-100, admin_id=1)
    assert removed is True

    count = await repo.get_warning_count(user_id=10, chat_id=-100)
    assert count == 0


async def test_remove_last_warning_removes_most_recent(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="older")
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="newer")
    await session.flush()

    await repo.remove_last_warning(user_id=10, chat_id=-100, admin_id=1)
    await session.flush()

    rows = await repo.list_warnings(user_id=10, chat_id=-100)
    assert len(rows) == 1
    assert rows[0].reason == "older"


async def test_remove_last_warning_writes_audit_log(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    await repo.add_warning(user_id=10, chat_id=-100, admin_id=1, reason="x")
    await session.flush()
    await repo.remove_last_warning(user_id=10, chat_id=-100, admin_id=1)
    await session.flush()

    result = await session.execute(select(ModerationLog).where(ModerationLog.action == "unwarn"))
    logs = result.scalars().all()
    assert len(logs) == 1


# ---------------------------------------------------------------------------
# record_action
# ---------------------------------------------------------------------------


async def test_record_action_appends_log(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    await repo.record_action(
        action="ban",
        user_id=10,
        admin_id=1,
        chat_id=-100,
        reason="bad actor",
        details="permanent",
    )
    await session.flush()

    result = await session.execute(select(ModerationLog).where(ModerationLog.action == "ban"))
    logs = result.scalars().all()
    assert len(logs) == 1
    assert logs[0].reason == "bad actor"
    assert logs[0].details == "permanent"


async def test_record_action_multiple_types(session: AsyncSession) -> None:
    repo = ModerationRepo(session)
    for action in ("ban", "kick", "mute", "pin", "fine"):
        await repo.record_action(action=action, user_id=10, admin_id=1, chat_id=-100)
    await session.flush()

    result = await session.execute(select(ModerationLog))
    logs = result.scalars().all()
    assert len(logs) == 5
    actions = {r.action for r in logs}
    assert actions == {"ban", "kick", "mute", "pin", "fine"}


# ---------------------------------------------------------------------------
# recent_actions (RR-1 #3)
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 8, 1, 12, 0)


def _log(
    action: str,
    *,
    days_ago: int,
    chat_id: int = -100,
    user_id: int | None = 42,
) -> ModerationLog:
    return ModerationLog(
        action=action,
        user_id=user_id,
        admin_id=1,
        chat_id=chat_id,
        reason=f"{action} reason",
        date=_NOW - timedelta(days=days_ago),
    )


async def test_recent_actions_returns_newest_first_within_the_limit(
    session: AsyncSession,
) -> None:
    session.add_all(
        [
            _log("warn", days_ago=5),
            _log("mute", days_ago=1),
            _log("ban", days_ago=3),
            _log("kick", days_ago=10),
        ]
    )
    await session.commit()

    rows = await ModerationRepo(session).recent_actions(user_id=42, chat_id=-100, limit=3, now=_NOW)
    assert [r.action for r in rows] == ["mute", "ban", "warn"]


async def test_recent_actions_drops_entries_past_the_window(
    session: AsyncSession,
) -> None:
    """An old punishment stops following the user around.

    ``now`` is injected so the cutoff the query computes is the same
    instant the rows were seeded against — a second wall-clock read would
    make the boundary case a coin flip.
    """
    session.add_all([_log("ban", days_ago=89), _log("kick", days_ago=91)])
    await session.commit()

    rows = await ModerationRepo(session).recent_actions(user_id=42, chat_id=-100, days=90, now=_NOW)
    assert [r.action for r in rows] == ["ban"]


async def test_recent_actions_is_scoped_to_one_user_and_chat(
    session: AsyncSession,
) -> None:
    """Including the NULL-target rows ``/pin`` writes, which must never
    surface on somebody's profile card."""
    session.add_all(
        [
            _log("ban", days_ago=1),
            _log("mute", days_ago=1, chat_id=-999),
            _log("kick", days_ago=1, user_id=77),
            _log("pin", days_ago=1, user_id=None),
        ]
    )
    await session.commit()

    rows = await ModerationRepo(session).recent_actions(user_id=42, chat_id=-100, now=_NOW)
    assert [r.action for r in rows] == ["ban"]
