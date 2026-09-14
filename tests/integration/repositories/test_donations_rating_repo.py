"""``DonationsRatingRepo`` — donations-rating write-side (L-38).

The ``in_rating`` column and ``rating_history`` table are NEW schema that
ships in migration ``0009_donations_rating_writeside`` but is NOT in the
ORM models this agent owns, so the fixture builds the tables with raw DDL
matching that migration (rather than ``Base.metadata.create_all``). This
also pins that the repo's raw SQL agrees with the migration's column shape.

Pins:

* ``set_in_rating`` flips the flag and reports whether the group row
  existed (no row → False, the "no donations to rank" signal).
* ``save_group_identity`` backfills the two display columns without ever
  blanking one it wasn't given, and never invents a row (RR-1 #9).
* ``recalc_positions`` densely ranks included groups by ``group_xp`` DESC
  and NULLs out excluded groups' positions; returns the included count.
* ``save_history_snapshot`` upserts today's ``(xp, position)`` and
  overwrites on a same-day re-run rather than duplicating.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.config.settings import AppEnv
from telegram_invite_bot.db.safety import install as install_safety
from telegram_invite_bot.repositories.donations_rating_repo import DonationsRatingRepo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


_DDL = (
    # Minimal groups_donations shape + the NEW in_rating column. The four
    # trailing columns are the ones ``record_donation`` seeds
    # (#1254): they are legacy-shared and carry prod's nullability, not
    # the ORM's.
    "CREATE TABLE groups_donations ("
    "  group_id INTEGER PRIMARY KEY,"
    "  group_name TEXT,"
    "  group_link TEXT,"
    "  group_xp INTEGER DEFAULT 0,"
    "  rating_position INTEGER,"
    "  in_rating INTEGER NOT NULL DEFAULT 1,"
    "  total_donations INTEGER DEFAULT 0,"
    "  members_count INTEGER DEFAULT 0,"
    "  last_donation TEXT,"
    "  created_at TEXT"
    ")",
    # Prod DDL verbatim (bot.py:5067): ``total_donated`` is NULLABLE with
    # a DEFAULT, which is what #1254 turns on.
    # ``GroupTopDonator.total_donated`` claims ``nullable=False`` and
    # disagrees with the live column;
    # reproducing prod rather than the model is the whole point here.
    "CREATE TABLE group_top_donators ("
    "  group_id INTEGER NOT NULL,"
    "  user_id INTEGER NOT NULL,"
    "  total_donated INTEGER DEFAULT 0,"
    "  last_donate TEXT,"
    "  PRIMARY KEY (group_id, user_id)"
    ")",
    "CREATE TABLE donations ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  user_id INTEGER NOT NULL,"
    "  group_id INTEGER NOT NULL,"
    "  amount INTEGER NOT NULL,"
    "  message TEXT,"
    "  created_at TEXT"
    ")",
    "CREATE TABLE rating_history ("
    "  group_id INTEGER NOT NULL,"
    "  date TEXT NOT NULL,"
    "  total_donations INTEGER NOT NULL,"
    "  position INTEGER,"
    "  PRIMARY KEY (group_id, date)"
    ")",
)


@pytest.fixture
async def session(tmp_path) -> AsyncIterator[AsyncSession]:  # noqa: ANN001
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    # Same PROD-pinned guard ``tests/integration/repositories/_session.py``
    # attaches, spelled out here because this fixture builds its tables from
    # raw migration DDL rather than ``Base.metadata.create_all`` and so
    # cannot use that helper. Its absence is why ``recalc_positions`` shipped
    # with a WHERE-less ``UPDATE groups_donations`` that ``db/safety.py``
    # refuses outright in production: every test here passed because a dev
    # engine only logs a warning.
    event.listens_for(engine.sync_engine, "before_cursor_execute")(install_safety(AppEnv.PROD))
    try:
        async with engine.begin() as conn:
            for stmt in _DDL:
                await conn.execute(text(stmt))
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as s:
            yield s
    finally:
        await engine.dispose()


async def _seed_group(session: AsyncSession, group_id: int, xp: int, *, in_rating: int = 1) -> None:
    await session.execute(
        text("INSERT INTO groups_donations (group_id, group_xp, in_rating) VALUES (:g, :xp, :ir)"),
        {"g": group_id, "xp": xp, "ir": in_rating},
    )
    await session.flush()


async def _position(session: AsyncSession, group_id: int) -> int | None:
    row = (
        await session.execute(
            text("SELECT rating_position FROM groups_donations WHERE group_id = :g"),
            {"g": group_id},
        )
    ).first()
    return None if row is None else row[0]


@pytest.mark.asyncio
async def test_set_in_rating_toggles_and_reports_existence(session: AsyncSession) -> None:
    await _seed_group(session, 100, 50)
    repo = DonationsRatingRepo(session)
    assert await repo.set_in_rating(100, included=False) is True
    row = (
        await session.execute(text("SELECT in_rating FROM groups_donations WHERE group_id = 100"))
    ).first()
    assert row is not None and row[0] == 0
    # No row for an undonated group → False.
    assert await repo.set_in_rating(999, included=False) is False


@pytest.mark.asyncio
async def test_recalc_ranks_included_densely(session: AsyncSession) -> None:
    await _seed_group(session, 1, 100)
    await _seed_group(session, 2, 300)
    await _seed_group(session, 3, 200)
    repo = DonationsRatingRepo(session)
    ranked = await repo.recalc_positions()
    assert ranked == 3
    assert await _position(session, 2) == 1  # 300
    assert await _position(session, 3) == 2  # 200
    assert await _position(session, 1) == 3  # 100


@pytest.mark.asyncio
async def test_recalc_skips_excluded_and_nulls_position(session: AsyncSession) -> None:
    await _seed_group(session, 1, 100)
    await _seed_group(session, 2, 300, in_rating=0)  # excluded
    await _seed_group(session, 3, 200)
    repo = DonationsRatingRepo(session)
    ranked = await repo.recalc_positions()
    assert ranked == 2  # only groups 1 and 3
    assert await _position(session, 3) == 1  # top of the *included* set
    assert await _position(session, 1) == 2
    assert await _position(session, 2) is None  # excluded → no slot


@pytest.mark.asyncio
async def test_recalc_skips_zero_xp_groups_the_board_never_shows(
    session: AsyncSession,
) -> None:
    """``/rating`` hides zero-xp groups, so ranking them hands out phantom slots.

    ``handlers/rating.py:162`` defines the visible set as xp > 0 AND included,
    and its own docstring says that predicate exists so "the count, the page
    and the drill-down can't drift".  ``recalc_positions`` kept a second copy
    of it with only the ``in_rating`` half, so a group that never received a
    donation still took a numbered slot the board never renders — prod row
    ``-1001111111111`` sat at position 3 with 0 xp.
    """
    await _seed_group(session, 1, 100)
    await _seed_group(session, 2, 0)  # included, but nothing donated
    await _seed_group(session, 3, 200)
    repo = DonationsRatingRepo(session)

    ranked = await repo.recalc_positions()

    assert ranked == 2  # only the two groups /rating actually lists
    assert await _position(session, 3) == 1
    assert await _position(session, 1) == 2  # the 0-xp row must not push this down
    assert await _position(session, 2) is None


@pytest.mark.asyncio
async def test_recalc_takes_back_the_slot_of_a_group_that_fell_to_zero(
    session: AsyncSession,
) -> None:
    """Only ``in_rating = 0`` used to clear a position, so a group whose xp
    was reset to zero kept its old number for good."""
    await _seed_group(session, 1, 100)
    repo = DonationsRatingRepo(session)
    assert await repo.recalc_positions() == 1
    assert await _position(session, 1) == 1

    await session.execute(text("UPDATE groups_donations SET group_xp = 0 WHERE group_id = 1"))

    assert await repo.recalc_positions() == 0
    assert await _position(session, 1) is None


@pytest.mark.asyncio
async def test_save_history_snapshot_upserts_same_day(session: AsyncSession) -> None:
    await _seed_group(session, 1, 100)
    repo = DonationsRatingRepo(session)
    await repo.recalc_positions()
    today = date(2026, 6, 10)
    assert await repo.save_history_snapshot(1, today=today) is True
    # Bump xp + re-snapshot the same day → one row, overwritten.
    await session.execute(text("UPDATE groups_donations SET group_xp = 500 WHERE group_id = 1"))
    await repo.recalc_positions()
    await repo.save_history_snapshot(1, today=today)
    rows = (
        await session.execute(
            text(
                "SELECT total_donations, position FROM rating_history "
                "WHERE group_id = 1 AND date = :d"
            ),
            {"d": today.strftime("%Y-%m-%d")},
        )
    ).all()
    assert len(rows) == 1
    assert rows[0][0] == 500


@pytest.mark.asyncio
async def test_save_history_snapshot_missing_group_is_false(session: AsyncSession) -> None:
    repo = DonationsRatingRepo(session)
    assert await repo.save_history_snapshot(404, today=date(2026, 6, 10)) is False


# ── RR-1 #9: save_group_identity ─────────────────────────────────────────


async def _identity(session: AsyncSession, group_id: int) -> tuple[str | None, str | None]:
    row = (
        await session.execute(
            text("SELECT group_link, group_name FROM groups_donations WHERE group_id = :g"),
            {"g": group_id},
        )
    ).first()
    assert row is not None
    return row[0], row[1]


async def test_save_group_identity_fills_both_columns(session: AsyncSession) -> None:
    await session.execute(
        text("INSERT INTO groups_donations (group_id, group_xp) VALUES (-100, 50)")
    )
    repo = DonationsRatingRepo(session)

    assert await repo.save_group_identity(-100, link="https://t.me/+new", title="Ком-клуб")

    assert await _identity(session, -100) == ("https://t.me/+new", "Ком-клуб")


async def test_save_group_identity_never_blanks_what_it_was_not_given(
    session: AsyncSession,
) -> None:
    """The common shape: the bot can read a group's title but has no right
    to mint a link. Legacy wrote ``group_link`` unconditionally here, so a
    lookup that came back empty wiped a link someone else got right."""
    await session.execute(
        text(
            "INSERT INTO groups_donations (group_id, group_xp, group_link, group_name) "
            "VALUES (-100, 50, 'https://t.me/+kept', 'Старое имя')"
        )
    )
    repo = DonationsRatingRepo(session)

    assert await repo.save_group_identity(-100, link=None, title="Новое имя")
    assert await _identity(session, -100) == ("https://t.me/+kept", "Новое имя")

    # Whitespace is not a value either — NULLIF sees it as empty.
    assert await repo.save_group_identity(-100, link="   ", title="   ")
    assert await _identity(session, -100) == ("https://t.me/+kept", "Новое имя")


async def test_save_group_identity_reports_a_missing_row_instead_of_creating_one(
    session: AsyncSession,
) -> None:
    """A group with no donations has no place on a donations leaderboard,
    so the UPDATE matching nothing is the answer, not an upsert."""
    repo = DonationsRatingRepo(session)

    assert not await repo.save_group_identity(-999, link="https://t.me/+x", title="Ниоткуда")

    count = (await session.execute(text("SELECT COUNT(*) FROM groups_donations"))).scalar_one()
    assert count == 0


async def _lifetime(session: AsyncSession, group_id: int, user_id: int) -> int | None:
    row = (
        await session.execute(
            text(
                "SELECT total_donated FROM group_top_donators "
                "WHERE group_id = :gid AND user_id = :uid"
            ),
            {"gid": group_id, "uid": user_id},
        )
    ).first()
    return None if row is None else row[0]


@pytest.mark.asyncio
async def test_a_null_lifetime_total_is_added_to_not_wiped(session: AsyncSession) -> None:
    """#1254: ``total_donated + excluded.total_donated`` is NULL when the
    stored value is NULL, and the column really can be NULL.

    Prod's live DDL is ``total_donated INTEGER DEFAULT 0`` — no NOT NULL —
    while ``GroupTopDonator.total_donated`` declares ``nullable=False``.
    The model is the one that is wrong about the database, so the upsert has
    to survive a row the model says cannot exist. Without the COALESCE it
    does not merely fail to add: it REPLACES a donator's lifetime counter
    with NULL, and the readers that ``int()`` the column
    (``donaters._fetch_top``, ``groupstats._fetch_top``) then
    raise TypeError and take ``/donaters`` and ``/groupstats`` down for
    the whole group.

    Prod carries no NULL row today, so this is a guard against a legacy
    write path or a hand-written INSERT creating one — not a live repair.
    The sibling ``groups_donations`` bump has always coalesced; this pins
    that the two writes in the same method finally agree.
    """
    await session.execute(
        text(
            "INSERT INTO group_top_donators (group_id, user_id, total_donated, last_donate) "
            "VALUES (:gid, :uid, NULL, '2026-01-01 00:00:00')"
        ),
        {"gid": -100, "uid": 7},
    )

    await DonationsRatingRepo(session).record_donation(
        group_id=-100,
        user_id=7,
        amount=250,
        message="from the shop",
        now=datetime(2026, 1, 2, 3, 4, 5),
    )

    assert await _lifetime(session, -100, 7) == 250


@pytest.mark.asyncio
async def test_a_second_donation_still_accumulates(session: AsyncSession) -> None:
    """The COALESCE must not turn the upsert into a plain overwrite: the
    ordinary case is a non-NULL row that keeps growing. Paired with the
    test above so a fix that reads ``excluded.total_donated`` alone — also
    green on the NULL row — cannot pass.
    """
    repo = DonationsRatingRepo(session)
    for amount in (250, 40):
        await repo.record_donation(
            group_id=-100,
            user_id=7,
            amount=amount,
            message="from the shop",
            now=datetime(2026, 1, 2, 3, 4, 5),
        )

    assert await _lifetime(session, -100, 7) == 290
