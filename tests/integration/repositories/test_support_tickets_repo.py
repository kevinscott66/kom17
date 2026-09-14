"""``SupportTicketsRepo`` — real-SQLite tests for all methods (T-022 extension).

Covers the Stage-14 ``create_open_ticket`` plus the five new methods
added in T-022:

* ``list_by_user`` — ordering (newest first) + limit
* ``get_by_id`` — found / not-found branches
* ``list_open`` — status filter, newest-first, limit
* ``add_admin_reply`` — sets all four answer fields atomically and
  returns ``(ReplyOutcome, row)``: ``OK`` with the updated row,
  ``NOT_FOUND`` with ``None``, ``NOT_OPEN`` with the untouched row
* ``mark_closed`` — sets status + closed_at; idempotent; returns False
  for a missing ticket

The ``create_open_ticket`` contract (originally in Stage 14) is kept as-is
because the repo file was re-written and we must not regress it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.support import SupportTicket
from telegram_invite_bot.repositories.support_tickets_repo import (
    ReplyOutcome,
    SupportTicketsRepo,
)
from tests.integration.repositories._session import build_session

RepoFixture = tuple[SupportTicketsRepo, AsyncSession]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, UsersBase, "users.db") as session:
        yield SupportTicketsRepo(session), session


# ── Stage-14 parity ──────────────────────────────────────────────────────────


async def test_create_open_ticket_persists_row(repo: RepoFixture) -> None:
    support_repo, session = repo
    now = datetime(2024, 1, 1, 12, 0, 0)

    tid = await support_repo.create_open_ticket(
        user_id=42,
        username="alice",
        first_name="Alice",
        text="нашёл баг в /balance",
        now=now,
    )
    assert tid > 0  # autoincrement populated by flush

    rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == 42
    assert row.username == "alice"
    assert row.first_name == "Alice"
    assert row.text == "нашёл баг в /balance"
    assert row.status == "open"
    assert row.created_at == now
    # Admin-reply columns must be NULL — the panel uses NULLs as the
    # "not answered yet" flag.
    assert row.answered_at is None
    assert row.answered_by is None
    assert row.closed_at is None


async def test_create_open_ticket_accepts_blank_handle(repo: RepoFixture) -> None:
    """Users without a @username should still produce a writable row."""
    support_repo, _ = repo
    tid = await support_repo.create_open_ticket(
        user_id=7,
        username=None,
        first_name="",
        text="nope",
    )
    assert tid > 0


async def test_create_open_ticket_default_now_is_utc_naive(
    repo: RepoFixture,
) -> None:
    """Repo populates ``created_at`` itself when caller omits ``now``.

    SQLite ``TIMESTAMP`` is naive — passing tz-aware datetimes would
    raise on some configurations. The repo strips tzinfo, so handlers
    don't have to think about it.
    """
    support_repo, session = repo
    tid = await support_repo.create_open_ticket(user_id=1, username="x", first_name="X", text="t")
    row = await session.get(SupportTicket, tid)
    assert row is not None
    assert row.created_at is not None
    assert row.created_at.tzinfo is None


# ── T-022: list_by_user ───────────────────────────────────────────────────────


async def test_list_by_user_returns_newest_first(repo: RepoFixture) -> None:
    """``list_by_user`` orders results by id DESC (newest first).

    We insert three tickets for user 10 with explicit timestamps so the
    expected ordering is unambiguous. The repo uses id DESC not
    created_at DESC — ids are autoincrement so they always track
    insertion order correctly even when the caller supplies the same
    ``now`` for multiple rows.
    """
    support_repo, _ = repo
    now = datetime(2024, 6, 1)
    t1 = await support_repo.create_open_ticket(
        user_id=10, username=None, first_name="A", text="first", now=now
    )
    t2 = await support_repo.create_open_ticket(
        user_id=10, username=None, first_name="A", text="second", now=now
    )
    t3 = await support_repo.create_open_ticket(
        user_id=10, username=None, first_name="A", text="third", now=now
    )

    rows = await support_repo.list_by_user(10)
    ids = [r.id for r in rows]
    assert ids == [t3, t2, t1]  # newest (highest id) first


async def test_list_by_user_respects_limit(repo: RepoFixture) -> None:
    """``limit`` parameter caps the returned count even when more exist."""
    support_repo, _ = repo
    for i in range(5):
        await support_repo.create_open_ticket(
            user_id=20, username=None, first_name="B", text=f"msg{i}"
        )

    rows = await support_repo.list_by_user(20, limit=3)
    assert len(rows) == 3


async def test_list_by_user_excludes_other_users(repo: RepoFixture) -> None:
    """Tickets belonging to a different user_id must not appear."""
    support_repo, _ = repo
    await support_repo.create_open_ticket(user_id=30, username=None, first_name="C", text="mine")
    await support_repo.create_open_ticket(user_id=99, username=None, first_name="X", text="theirs")

    rows = await support_repo.list_by_user(30)
    assert all(r.user_id == 30 for r in rows)
    assert len(rows) == 1


async def test_list_by_user_empty_for_unknown_user(repo: RepoFixture) -> None:
    support_repo, _ = repo
    rows = await support_repo.list_by_user(9999)
    assert rows == []


# ── T-022: get_by_id ─────────────────────────────────────────────────────────


async def test_get_by_id_returns_ticket(repo: RepoFixture) -> None:
    support_repo, _ = repo
    tid = await support_repo.create_open_ticket(
        user_id=5, username="bob", first_name="Bob", text="hello"
    )
    row = await support_repo.get_by_id(tid)
    assert row is not None
    assert row.id == tid
    assert row.user_id == 5


async def test_get_by_id_returns_none_for_missing(repo: RepoFixture) -> None:
    support_repo, _ = repo
    result = await support_repo.get_by_id(99999)
    assert result is None


# ── T-022: list_open ─────────────────────────────────────────────────────────


async def test_list_open_returns_only_open_tickets(repo: RepoFixture) -> None:
    """Tickets with status != 'open' must not appear in the listing."""
    support_repo, _ = repo
    open_id = await support_repo.create_open_ticket(
        user_id=1, username=None, first_name="A", text="open one"
    )
    closed_id = await support_repo.create_open_ticket(
        user_id=2, username=None, first_name="B", text="closed one"
    )
    await support_repo.mark_closed(closed_id)

    rows = await support_repo.list_open()
    ids = {r.id for r in rows}
    assert open_id in ids
    assert closed_id not in ids


async def test_list_open_newest_first(repo: RepoFixture) -> None:
    support_repo, _ = repo
    t1 = await support_repo.create_open_ticket(user_id=1, username=None, first_name="A", text="a")
    t2 = await support_repo.create_open_ticket(user_id=2, username=None, first_name="B", text="b")
    t3 = await support_repo.create_open_ticket(user_id=3, username=None, first_name="C", text="c")

    rows = await support_repo.list_open()
    ids = [r.id for r in rows]
    assert ids == [t3, t2, t1]


async def test_list_open_respects_limit(repo: RepoFixture) -> None:
    support_repo, _ = repo
    for i in range(5):
        await support_repo.create_open_ticket(
            user_id=i, username=None, first_name="X", text=f"t{i}"
        )

    rows = await support_repo.list_open(limit=2)
    assert len(rows) == 2


# ── T-022: add_admin_reply ───────────────────────────────────────────────────


async def test_add_admin_reply_sets_all_fields(repo: RepoFixture) -> None:
    """``add_admin_reply`` must set answer, answered_by, answered_at, and
    flip status to 'answered' atomically (all in one flush).
    """
    support_repo, session = repo
    tid = await support_repo.create_open_ticket(
        user_id=7, username=None, first_name="D", text="question"
    )
    ts = datetime(2024, 6, 15, 10, 30)

    outcome, ticket = await support_repo.add_admin_reply(
        tid, admin_id=999, answer="Here is your answer", now=ts
    )

    assert outcome is ReplyOutcome.OK
    assert ticket is not None
    assert ticket.id == tid
    assert ticket.answer == "Here is your answer"
    assert ticket.answered_by == 999
    assert ticket.answered_at == ts
    assert ticket.status == "answered"

    # Verify persistence — fetch fresh from the session to prove it
    # was flushed, not just mutated in the Python object.
    await session.refresh(ticket)
    assert ticket.status == "answered"


async def test_add_admin_reply_returns_none_for_missing(repo: RepoFixture) -> None:
    support_repo, _ = repo
    outcome, ticket = await support_repo.add_admin_reply(88888, admin_id=1, answer="no ticket")
    assert outcome is ReplyOutcome.NOT_FOUND
    assert ticket is None


async def test_add_admin_reply_refuses_a_second_answer(repo: RepoFixture) -> None:
    """#565: only an ``open`` ticket may be answered.

    The row holds a single ``answer`` column and nothing archives the
    previous one, so a second ``/ticket_reply`` on the same id used to
    overwrite the first answer, its author and its timestamp with no way
    back. Legacy guarded against exactly this
    (``if not ticket or ticket.status != "open": return False`` at
    bot.py:35600-35604); the port now reports the refusal instead of
    discarding it the way legacy did (bot.py:35844).
    """
    support_repo, _ = repo
    tid = await support_repo.create_open_ticket(user_id=11, username=None, first_name="G", text="q")
    first_ts = datetime(2024, 6, 15, 10, 30)
    outcome, _ = await support_repo.add_admin_reply(tid, admin_id=100, answer="first", now=first_ts)
    assert outcome is ReplyOutcome.OK

    outcome, ticket = await support_repo.add_admin_reply(
        tid, admin_id=200, answer="second", now=datetime(2024, 6, 16, 9, 0)
    )

    assert outcome is ReplyOutcome.NOT_OPEN
    # NOT_OPEN always carries the row, and the row is untouched.
    assert ticket is not None
    assert (ticket.answer, ticket.answered_by, ticket.answered_at) == (
        "first",
        100,
        first_ts,
    )
    assert ticket.status == "answered"


async def test_add_admin_reply_default_now_is_utc_naive(repo: RepoFixture) -> None:
    """``answered_at`` auto-set to UTC-naive datetime when caller omits ``now``."""
    support_repo, _ = repo
    tid = await support_repo.create_open_ticket(user_id=8, username=None, first_name="E", text="q")
    outcome, ticket = await support_repo.add_admin_reply(tid, admin_id=1, answer="a")
    assert outcome is ReplyOutcome.OK
    assert ticket is not None
    assert ticket.answered_at is not None
    assert ticket.answered_at.tzinfo is None


# ── T-022: mark_closed ───────────────────────────────────────────────────────


async def test_mark_closed_sets_status_and_timestamp(repo: RepoFixture) -> None:
    support_repo, session = repo
    tid = await support_repo.create_open_ticket(user_id=9, username=None, first_name="F", text="r")
    ts = datetime(2024, 7, 1, 8, 0)

    result = await support_repo.mark_closed(tid, now=ts)
    assert result is True

    row = await session.get(SupportTicket, tid)
    assert row is not None
    assert row.status == "closed"
    assert row.closed_at == ts


async def test_mark_closed_idempotent(repo: RepoFixture) -> None:
    """Calling mark_closed on an already-closed ticket is a no-op that
    still returns True (does not crash or return False).
    """
    support_repo, _ = repo
    tid = await support_repo.create_open_ticket(user_id=10, username=None, first_name="G", text="x")
    ts1 = datetime(2024, 7, 1)
    ts2 = datetime(2024, 7, 2)

    assert await support_repo.mark_closed(tid, now=ts1)
    assert await support_repo.mark_closed(tid, now=ts2)  # idempotent: returns True again


async def test_mark_closed_returns_false_for_missing(repo: RepoFixture) -> None:
    support_repo, _ = repo
    result = await support_repo.mark_closed(77777)
    assert result is False


# ── R15: a failed insert must leave the session usable ───────────────────────


async def test_failed_insert_leaves_the_session_usable(repo: RepoFixture) -> None:
    """The raise is the whole contract — it must not also break the caller.

    All three ``/feedback`` / ``/support`` call sites catch this
    exception, reply "couldn't save" and return, leaving
    :class:`SessionMiddleware` to commit whatever else the update wrote
    (a ``last_seen`` touch, a nickname, an AI-quota bump). Without the
    SAVEPOINT the failed flush deactivates the transaction and that
    commit raises ``PendingRollbackError``, so the unrelated writes are
    lost and an unhandled error surfaces after the user was already
    told, reassuringly, that only the ticket failed.
    """
    support_repo, session = repo

    # An unrelated users-DB write from the same update, made before the
    # ticket attempt — this is what the middleware would lose.
    session.add(SupportTicket(user_id=7, text="earlier", status="open"))
    await session.flush()

    # ``user_id`` is the table's only NOT NULL column — force a genuine
    # flush failure rather than a hand-raised exception that never
    # reached the connection.
    with pytest.raises(IntegrityError):
        await support_repo.create_open_ticket(
            user_id=None,  # type: ignore[arg-type]
            username="bob",
            first_name="Bob",
            text="will not save",
        )

    # The session still works, and the commit lands.
    assert (
        await support_repo.create_open_ticket(
            user_id=2, username="carol", first_name="Carol", text="after the failure"
        )
        > 0
    )
    await session.commit()

    rows = (await session.execute(select(SupportTicket))).scalars().all()
    assert sorted(r.user_id for r in rows) == [2, 7], (
        "a failed ticket insert took unrelated writes from the same update down with it"
    )
