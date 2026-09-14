"""``UsersRepo`` upsert semantics against a real SQLite file.

Schema is created via ``Base.metadata.create_all`` from our model. If
this drifts from the prod schema in ``docs/prod_schemas.sql`` the test
won't catch it — that's intentional, schema parity is a separate
concern handled by Alembic + the dump diff. What we ARE testing here:

* First touch inserts ``joined_date = last_seen = now`` and ``is_new=True``.
* Second touch preserves the original ``joined_date`` (legacy
  ``COALESCE`` semantics) and advances ``last_seen`` / ``last_active``.
* ``is_premium`` round-trips as a real bool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import User as UserRow
from telegram_invite_bot.repositories.users_repo import UsersRepo
from tests.integration.repositories._session import build_session

RepoFixture = tuple[UsersRepo, AsyncSession]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, UsersBase, "users.db") as session:
        yield UsersRepo(session), session


async def test_first_upsert_marks_user_new(repo: RepoFixture) -> None:
    users_repo, session = repo
    user = await users_repo.upsert_from_telegram(
        user_id=42,
        username="alice",
        first_name="Alice",
        last_name=None,
        language_code="en",
        is_premium=False,
    )
    await session.commit()
    assert user.is_new is True
    assert user.user_id == 42
    assert user.username == "alice"
    assert user.joined_date is not None
    assert user.last_seen == user.joined_date


async def test_second_upsert_preserves_joined_date(repo: RepoFixture) -> None:
    users_repo, session = repo
    first = await users_repo.upsert_from_telegram(
        user_id=42,
        username="alice",
        first_name="Alice",
        last_name=None,
        language_code="en",
        is_premium=False,
        now=datetime(2024, 1, 1, 12, 0, 0),
    )
    await session.commit()

    second = await users_repo.upsert_from_telegram(
        user_id=42,
        username="alice2",
        first_name="Alice",
        last_name="Smith",
        language_code="ru",
        is_premium=True,
        now=datetime(2024, 1, 1, 12, 0, 0) + timedelta(days=30),
    )
    await session.commit()

    assert second.is_new is False
    assert second.joined_date == first.joined_date  # preserved
    assert first.last_seen is not None
    assert second.last_seen is not None
    assert second.last_seen > first.last_seen  # advanced
    assert second.username == "alice2"
    assert second.is_premium is True


async def test_get_returns_none_for_missing(repo: RepoFixture) -> None:
    users_repo, _ = repo
    assert await users_repo.get(99999) is None


async def test_get_by_username_finds_user_case_insensitive(
    repo: RepoFixture,
) -> None:
    """Telegram usernames are case-preserving but case-insensitive at
    the protocol level — match legacy ``lower(username) = ?`` exactly.
    A user stored as ``Alice`` resolves for any of the casings the
    /send handler can deliver from raw user input.
    """
    users_repo, session = repo
    await users_repo.upsert_from_telegram(
        user_id=42,
        username="Alice",
        first_name="A",
        last_name=None,
        language_code="en",
        is_premium=False,
    )
    await session.commit()
    for variant in ("Alice", "alice", "ALICE", "aLiCe"):
        fetched = await users_repo.get_by_username(variant)
        assert fetched is not None, variant
        assert fetched.user_id == 42


async def test_get_by_username_strips_leading_at(repo: RepoFixture) -> None:
    """Parse layer already strips, but the repo is defensive — admin
    tooling and future callers may pass the raw ``@handle`` token.
    """
    users_repo, session = repo
    await users_repo.upsert_from_telegram(
        user_id=7,
        username="bob",
        first_name="B",
        last_name=None,
        language_code=None,
        is_premium=False,
    )
    await session.commit()
    fetched = await users_repo.get_by_username("@bob")
    assert fetched is not None
    assert fetched.user_id == 7


async def test_get_by_username_returns_none_for_missing(repo: RepoFixture) -> None:
    users_repo, _ = repo
    assert await users_repo.get_by_username("ghost") is None


async def test_get_by_username_empty_short_circuits_to_none(
    repo: RepoFixture,
) -> None:
    """Empty input → ``None`` without touching the DB. Guards against
    a future row with ``username = ''`` (degenerate, but legacy schema
    permits) being returned for ``/send @`` malformed input.
    """
    users_repo, _ = repo
    assert await users_repo.get_by_username("") is None
    assert await users_repo.get_by_username("@") is None
    assert await users_repo.get_by_username("   ") is None


async def test_get_by_username_skips_null_username_rows(
    repo: RepoFixture,
) -> None:
    """Users without a Telegram handle (legacy allows
    ``username IS NULL``) must never match — guards the SQL
    ``IS NOT NULL`` filter against a regression toward a bare
    ``lower(username) = ?`` query that would coerce NULL to ''.
    """
    users_repo, session = repo
    await users_repo.upsert_from_telegram(
        user_id=99,
        username=None,
        first_name="Nameless",
        last_name=None,
        language_code=None,
        is_premium=False,
    )
    await session.commit()
    assert await users_repo.get_by_username("") is None


async def test_get_by_username_prefers_the_most_recently_seen_row(
    repo: RepoFixture,
) -> None:
    """#702: a duplicate handle resolves to the account still in use.

    Telegram usernames are transferable, so two rows can legitimately
    carry the same one — the account that released it and the account
    that took it. Both legacy sites sort (``bot.py:19049`` and
    ``bot.py:41592`` both end ``ORDER BY last_seen DESC LIMIT 1``); a
    bare ``LIMIT 1`` lets SQLite return whichever row it reaches first,
    so ``/send``, ``/give`` and check creation could pay the abandoned
    account. Both insertion orders are exercised because a missing
    ``ORDER BY`` happens to pick the right row half the time.
    """
    users_repo, session = repo
    stale = datetime(2024, 1, 1, 12, 0, 0)
    for old_id, new_id in ((11, 12), (22, 21)):
        for user_id in (old_id, new_id):
            await users_repo.upsert_from_telegram(
                user_id=user_id,
                username=f"handle{old_id}",
                first_name="H",
                last_name=None,
                language_code=None,
                is_premium=False,
            )
        await session.execute(
            update(UserRow).where(UserRow.user_id == old_id).values(last_seen=stale)
        )
        await session.commit()
        fetched = await users_repo.get_by_username(f"handle{old_id}")
        assert fetched is not None
        assert fetched.user_id == new_id


async def test_get_by_username_falls_back_to_a_never_seen_row(
    repo: RepoFixture,
) -> None:
    """#702 corollary: NULL ``last_seen`` sorts last under SQLite DESC.

    That is the legacy behaviour too, and it only costs the row the
    match when a seen row also holds the handle — a lone never-seen row
    must still resolve, or the ordering fix would break lookups for
    users imported without a ``last_seen`` stamp.
    """
    users_repo, session = repo
    await users_repo.upsert_from_telegram(
        user_id=31,
        username="ghosted",
        first_name="G",
        last_name=None,
        language_code=None,
        is_premium=False,
    )
    await session.execute(update(UserRow).where(UserRow.user_id == 31).values(last_seen=None))
    await session.commit()
    fetched = await users_repo.get_by_username("ghosted")
    assert fetched is not None
    assert fetched.user_id == 31


async def test_get_returns_entity(repo: RepoFixture) -> None:
    users_repo, session = repo
    await users_repo.upsert_from_telegram(
        user_id=7,
        username=None,
        first_name="Bob",
        last_name=None,
        language_code=None,
        is_premium=False,
    )
    await session.commit()
    fetched = await users_repo.get(7)
    assert fetched is not None
    assert fetched.user_id == 7
    assert fetched.is_new is False
    assert fetched.language == "ru"  # no language_code → defaults to ru
