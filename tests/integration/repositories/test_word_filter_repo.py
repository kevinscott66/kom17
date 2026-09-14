"""Real-SQLite tests for :class:`WordFilterRepo` (L-52).

Invariants:

* ``add`` inserts a normalised (stripped, lower) row and returns True.
* ``add`` of a duplicate (same group+word, any case/whitespace) returns
  False and does not stack rows.
* ``add`` scopes by group: the same word in two groups is two rows.
* ``remove`` deletes a present word (case-insensitive) and returns True;
  returns False when the word is absent.
* ``list`` returns all words for a group, alphabetically, scoped.
* ``match`` is a case-insensitive substring match returning the stored
  word, or None when nothing matches.

RR-4 #43 added the id-carrying half, which the /groupadmin Words page
needs because a word cannot ride in ``callback_data``:

* ``list_entries`` returns (id, word) in the same order as ``list``.
* ``remove_by_id`` deletes by row id, returns the word it removed, and
  refuses an id belonging to another group — the group check is the
  whole security story of the per-word delete button.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.db.models.word_filters import WordFilter
from telegram_invite_bot.repositories.word_filter_repo import WordFilterRepo
from tests.integration.repositories._session import build_session

_GROUP = -100
_OTHER_GROUP = -200


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, ModerationBase, "moderation.db") as s:
        yield s


async def test_add_inserts_normalized(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    added = await repo.add(group_id=_GROUP, word="  BadWord  ", added_by=7)
    assert added is True

    rows = (
        (await session.execute(select(WordFilter).where(WordFilter.group_id == _GROUP)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].word == "badword"
    assert rows[0].added_by == 7


async def test_add_duplicate_is_noop(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    assert await repo.add(group_id=_GROUP, word="spam", added_by=1) is True
    # Same word, different case/whitespace → still a duplicate.
    assert await repo.add(group_id=_GROUP, word="  SPAM ", added_by=2) is False

    count = (
        await session.execute(
            select(func.count()).select_from(WordFilter).where(WordFilter.group_id == _GROUP)
        )
    ).scalar_one()
    assert count == 1


async def test_add_scoped_by_group(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    assert await repo.add(group_id=_GROUP, word="x", added_by=1) is True
    # Same word in a different group is a distinct row.
    assert await repo.add(group_id=_OTHER_GROUP, word="x", added_by=1) is True
    assert await repo.list(group_id=_GROUP) == ["x"]
    assert await repo.list(group_id=_OTHER_GROUP) == ["x"]


async def test_remove_present_and_absent(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    await repo.add(group_id=_GROUP, word="foo", added_by=1)
    # Case-insensitive removal.
    assert await repo.remove(group_id=_GROUP, word="FOO") is True
    assert await repo.list(group_id=_GROUP) == []
    # Removing again → not found.
    assert await repo.remove(group_id=_GROUP, word="foo") is False


async def test_remove_scoped_by_group(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    await repo.add(group_id=_GROUP, word="z", added_by=1)
    await repo.add(group_id=_OTHER_GROUP, word="z", added_by=1)
    assert await repo.remove(group_id=_GROUP, word="z") is True
    # The other group's copy survives.
    assert await repo.list(group_id=_OTHER_GROUP) == ["z"]


async def test_list_sorted_and_scoped(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    for w in ("gamma", "alpha", "beta"):
        await repo.add(group_id=_GROUP, word=w, added_by=1)
    await repo.add(group_id=_OTHER_GROUP, word="zzz", added_by=1)
    assert await repo.list(group_id=_GROUP) == ["alpha", "beta", "gamma"]


async def test_match_substring_case_insensitive(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    await repo.add(group_id=_GROUP, word="badword", added_by=1)
    # Substring, mixed case in the candidate text.
    assert await repo.match(group_id=_GROUP, text="this is a BadWord!") == "badword"


async def test_match_returns_none_when_no_hit(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    await repo.add(group_id=_GROUP, word="banned", added_by=1)
    assert await repo.match(group_id=_GROUP, text="perfectly fine text") is None


async def test_match_empty_group_is_none(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    assert await repo.match(group_id=_GROUP, text="anything") is None


# ---------------------------------------------------------------------------
# The id-carrying view (RR-4 #43)
# ---------------------------------------------------------------------------


async def test_list_entries_agrees_with_list(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    for w in ("gamma", "alpha", "beta"):
        await repo.add(group_id=_GROUP, word=w, added_by=1)
    await repo.add(group_id=_OTHER_GROUP, word="zzz", added_by=1)

    entries = await repo.list_entries(group_id=_GROUP)
    assert [e.word for e in entries] == ["alpha", "beta", "gamma"]
    # Insertion order was gamma, alpha, beta — so the ids are NOT sorted,
    # which is exactly why a positional index would be the wrong handle.
    assert sorted(e.id for e in entries) != [e.id for e in entries]
    assert all(e.id > 0 for e in entries)


async def test_remove_by_id_returns_the_word_it_deleted(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    await repo.add(group_id=_GROUP, word="spam", added_by=1)
    await repo.add(group_id=_GROUP, word="ham", added_by=1)
    target = next(e for e in await repo.list_entries(group_id=_GROUP) if e.word == "spam")

    assert await repo.remove_by_id(group_id=_GROUP, word_id=target.id) == "spam"
    assert await repo.list(group_id=_GROUP) == ["ham"]


async def test_remove_by_id_refuses_another_groups_row(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    await repo.add(group_id=_OTHER_GROUP, word="secret", added_by=1)
    victim = (await repo.list_entries(group_id=_OTHER_GROUP))[0]

    # A payload crafted by an admin of _GROUP against a row of
    # _OTHER_GROUP must not reach it: the button names an id, so the
    # ownership check is the only thing standing between the two groups.
    assert await repo.remove_by_id(group_id=_GROUP, word_id=victim.id) is None
    assert await repo.list(group_id=_OTHER_GROUP) == ["secret"]


async def test_remove_by_id_missing_row_is_none(session: AsyncSession) -> None:
    repo = WordFilterRepo(session)
    # Already-deleted row (double tap on a stale card) → no exception.
    assert await repo.remove_by_id(group_id=_GROUP, word_id=4321) is None
