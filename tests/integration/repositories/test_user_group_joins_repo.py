"""Real-SQLite tests for :class:`UserGroupJoinsRepo` (RR-1 #3).

The interesting behaviour is entirely in the conflict path: this table
has a ``(user_id, chat_id)`` primary key, so every re-observation of a
membership hits an existing row, and what that write does — or refuses
to do — decides whether the profile card's since-join counter is stable
or quietly shrinks every time someone is seen again.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import Marriage, Relationship, UserGroupJoin
from telegram_invite_bot.repositories.user_group_joins_repo import UserGroupJoinsRepo
from tests.integration.repositories._session import build_session

_USER = 500
_CHAT = -100500
_FIRST = datetime(2024, 3, 1, 10, 0)
_LATER = datetime(2024, 9, 1, 10, 0)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, UsersBase, "users.db") as s:
        yield s


async def test_joined_at_is_none_for_an_unknown_membership(
    session: AsyncSession,
) -> None:
    assert await UserGroupJoinsRepo(session).joined_at(_USER, _CHAT) is None


async def test_record_join_then_read_it_back(session: AsyncSession) -> None:
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(_USER, _CHAT, joined_at=_FIRST, source="join_event", group_title="Чат")
    await session.commit()
    assert await repo.joined_at(_USER, _CHAT) == _FIRST


async def test_a_second_sighting_never_moves_the_join_date_forward(
    session: AsyncSession,
) -> None:
    """The whole point of the conflict clause.

    If a re-join overwrote ``joined_at``, every since-join counter would
    reset to near-zero for anyone who left and came back — the card would
    report a long-standing member as brand new. The liveness columns do
    move: the row is un-flagged as departed.
    """
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(_USER, _CHAT, joined_at=_FIRST, source="join_event")
    await session.commit()
    # Simulate a departure recorded between the two sightings.
    row = await session.get(UserGroupJoin, (_USER, _CHAT))
    assert row is not None
    row.left_at = datetime(2024, 6, 1)
    row.is_active = 0
    await session.commit()

    await repo.record_join(_USER, _CHAT, joined_at=_LATER, source="join_event")
    await session.commit()

    # ``record_join`` issues a Core INSERT ... ON CONFLICT, which the ORM
    # identity map knows nothing about — the ``session.get`` above left a
    # stale instance behind. Expire it so the assertions below read the
    # database rather than this session's memory of it.
    session.expire_all()
    stored = (await session.execute(select(UserGroupJoin))).scalars().all()
    assert len(stored) == 1  # one membership, not two rows
    assert stored[0].joined_at == _FIRST
    assert stored[0].last_seen == _LATER
    assert stored[0].left_at is None
    assert stored[0].is_active == 1


async def test_a_renamed_group_updates_the_stored_title(
    session: AsyncSession,
) -> None:
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(
        _USER, _CHAT, joined_at=_FIRST, source="join_event", group_title="Старое имя"
    )
    await repo.record_join(
        _USER, _CHAT, joined_at=_LATER, source="join_event", group_title="Новое имя"
    )
    await session.commit()
    session.expire_all()
    row = await session.get(UserGroupJoin, (_USER, _CHAT))
    assert row is not None
    assert row.group_title == "Новое имя"


async def test_a_title_less_sighting_does_not_blank_a_known_title(
    session: AsyncSession,
) -> None:
    """``group_title=None`` means "this caller doesn't have the title",
    not "the group has no name" — it must not erase what we already know."""
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(_USER, _CHAT, joined_at=_FIRST, source="join_event", group_title="Чат")
    await repo.record_join(_USER, _CHAT, joined_at=_LATER, source="join_event")
    await session.commit()
    session.expire_all()
    row = await session.get(UserGroupJoin, (_USER, _CHAT))
    assert row is not None
    assert row.group_title == "Чат"


async def test_joined_at_still_answers_for_a_departed_member(
    session: AsyncSession,
) -> None:
    """A stale ``is_active=0`` must not erase a date that isn't in doubt.

    Legacy filtered this read on ``is_active`` and so blanked the "in
    this group since" line for anyone whose rejoin it hadn't observed.
    """
    repo = UserGroupJoinsRepo(session)
    session.add(
        UserGroupJoin(
            user_id=_USER,
            chat_id=_CHAT,
            joined_at=_FIRST,
            source="observed_message",
            left_at=datetime(2024, 6, 1),
            is_active=0,
        )
    )
    await session.commit()
    assert await repo.joined_at(_USER, _CHAT) == _FIRST


async def test_memberships_are_scoped_per_chat(session: AsyncSession) -> None:
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(_USER, _CHAT, joined_at=_FIRST, source="join_event")
    await repo.record_join(_USER, -777, joined_at=_LATER, source="join_event")
    await session.commit()
    assert await repo.joined_at(_USER, _CHAT) == _FIRST
    assert await repo.joined_at(_USER, -777) == _LATER


async def test_mark_left_flags_the_row_without_deleting_it(
    session: AsyncSession,
) -> None:
    """Legacy's bot.py:44187-44191 in one assertion.

    Deleting instead of flagging would lose ``joined_at``, and the next
    ``record_join`` would file today's date as the join date — the card
    would then report a two-year member as having arrived this morning.
    """
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(_USER, _CHAT, joined_at=_FIRST, source="join_event")
    await session.commit()

    await repo.mark_left(_USER, _CHAT, left_at=_LATER)
    await session.commit()
    session.expire_all()

    row = await session.get(UserGroupJoin, (_USER, _CHAT))
    assert row is not None
    assert row.is_active == 0
    assert row.left_at == _LATER
    assert row.joined_at == _FIRST


async def test_mark_left_touches_only_the_named_membership(
    session: AsyncSession,
) -> None:
    """Both predicates carry weight, and dropping either is a disaster.

    Without ``chat_id`` one departure would declare the person gone from
    every group the bot serves; without ``user_id`` it would empty the
    group. This pins both halves at once.
    """
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(_USER, _CHAT, joined_at=_FIRST, source="join_event")
    await repo.record_join(_USER, -777, joined_at=_FIRST, source="join_event")
    await repo.record_join(_USER + 1, _CHAT, joined_at=_FIRST, source="join_event")
    await session.commit()

    await repo.mark_left(_USER, _CHAT, left_at=_LATER)
    await session.commit()
    session.expire_all()

    rows = {
        (r.user_id, r.chat_id): r
        for r in (await session.execute(select(UserGroupJoin))).scalars().all()
    }
    assert rows[(_USER, _CHAT)].is_active == 0
    assert rows[(_USER, -777)].is_active == 1
    assert rows[(_USER, -777)].left_at is None
    assert rows[(_USER + 1, _CHAT)].is_active == 1
    assert rows[(_USER + 1, _CHAT)].left_at is None


async def test_mark_left_invents_no_row_for_an_unseen_membership(
    session: AsyncSession,
) -> None:
    """A departure we never saw the arrival for says nothing about
    ``joined_at``; an inserted row would have to guess it, and the guess
    would poison the since-join counter this table exists to feed."""
    repo = UserGroupJoinsRepo(session)
    await repo.mark_left(_USER, _CHAT, left_at=_LATER)
    await session.commit()

    assert (await session.execute(select(UserGroupJoin))).scalars().all() == []


async def test_a_rejoin_after_mark_left_heals_the_flag(
    session: AsyncSession,
) -> None:
    """The two writers are each other's inverse and must round-trip: a
    departure flags the row, the next arrival un-flags it, and the join
    date never budges."""
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(_USER, _CHAT, joined_at=_FIRST, source="join_event")
    await session.commit()
    await repo.mark_left(_USER, _CHAT, left_at=_LATER)
    await session.commit()

    await repo.record_join(_USER, _CHAT, joined_at=_LATER, source="join_event")
    await session.commit()
    session.expire_all()

    row = await session.get(UserGroupJoin, (_USER, _CHAT))
    assert row is not None
    assert row.is_active == 1
    assert row.left_at is None
    assert row.joined_at == _FIRST


# ---------------------------------------------------------------------------
# #482: the read/write surface the departed-bonds sweeper stands on
# ---------------------------------------------------------------------------


async def test_list_departed_chats_is_empty_while_everyone_is_present(
    session: AsyncSession,
) -> None:
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(_USER, _CHAT, joined_at=_FIRST, source="join_event")
    await session.commit()

    assert await repo.list_departed_chats() == []


async def test_list_departed_chats_reports_each_chat_once(
    session: AsyncSession,
) -> None:
    """The sweeper iterates this list, so a duplicate would mean a chat
    swept twice in one pass — twice the Telegram probes for the same
    answer."""
    repo = UserGroupJoinsRepo(session)
    for uid in (_USER, _USER + 1):
        await repo.record_join(uid, _CHAT, joined_at=_FIRST, source="join_event")
        await repo.mark_left(uid, _CHAT, left_at=_LATER)
    await repo.record_join(_USER, -777, joined_at=_FIRST, source="join_event")
    await repo.mark_left(_USER, -777, left_at=_LATER)
    await session.commit()

    assert await repo.list_departed_chats() == [_CHAT, -777]


async def _depart(
    session: AsyncSession,
    *,
    user_id: int,
    left_at: datetime,
    chat_id: int = _CHAT,
) -> None:
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(user_id, chat_id, joined_at=_FIRST, source="join_event")
    await repo.mark_left(user_id, chat_id, left_at=left_at)


def _bond(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int = _CHAT,
    partner: int = 9000,
    model: type[Marriage] | type[Relationship] = Marriage,
    status: str | None = "active",
    reversed_pair: bool = False,
) -> None:
    first, second = (partner, user_id) if reversed_pair else (user_id, partner)
    session.add(
        model(
            chat_id=chat_id,
            user1_id=first,
            user2_id=second,
            created_at=_FIRST,
            experience=0,
            status=status,
        )
    )


async def test_candidates_honour_the_threshold(session: AsyncSession) -> None:
    """The whole point of the seven-day grace: someone who left an hour
    ago must not be swept, and the comparison is inclusive at the
    boundary so a departure exactly on the threshold is not stranded for
    another interval."""
    for uid, left in ((1, datetime(2024, 9, 1)), (2, _LATER), (3, datetime(2024, 12, 1))):
        await _depart(session, user_id=uid, left_at=left)
        _bond(session, user_id=uid, partner=9000 + uid)
    await session.commit()

    got = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(_CHAT, _LATER, limit=10)
    assert got == [1, 2]


async def test_candidates_are_scoped_to_one_chat(session: AsyncSession) -> None:
    for chat in (_CHAT, -777):
        await _depart(session, user_id=_USER, left_at=datetime(2024, 4, 1), chat_id=chat)
        _bond(session, user_id=_USER, chat_id=chat)
    await session.commit()

    got = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(-777, _LATER, limit=10)
    assert got == [_USER]


async def test_candidates_skip_present_members(session: AsyncSession) -> None:
    """A rejoin clears the flag, and a cleared flag must take the row out
    of the candidate set immediately — otherwise the sweeper spends a
    probe re-learning what the join event already told it."""
    repo = UserGroupJoinsRepo(session)
    await _depart(session, user_id=_USER, left_at=datetime(2024, 4, 1))
    _bond(session, user_id=_USER)
    await session.commit()
    await repo.record_join(_USER, _CHAT, joined_at=_LATER, source="join_event")
    await session.commit()

    assert await repo.list_departed_with_active_bonds(_CHAT, _LATER, limit=10) == []


async def test_candidates_take_the_longest_departed_first(session: AsyncSession) -> None:
    """``limit`` exists because every id costs a live ``get_chat_member``
    round-trip, so a truncated pass has to truncate somewhere useful.
    Ordering by ``left_at`` makes the cap drain the backlog in order
    instead of starving whoever has waited longest."""
    for uid, day in ((1, 5), (2, 3), (3, 1)):
        await _depart(session, user_id=uid, left_at=datetime(2024, 4, day))
        _bond(session, user_id=uid, partner=9000 + uid)
    await session.commit()

    got = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(_CHAT, _LATER, limit=2)
    assert got == [3, 2]


async def test_the_limit_applies_after_the_bond_filter(session: AsyncSession) -> None:
    """#2013 — the regression this query exists in one piece to prevent.

    A settled departure is permanent: :meth:`clear_departure` fires only
    for a member the probe found still present, so somebody who left
    years ago with nothing to dissolve keeps their row and keeps sorting
    to the front of ``left_at ASC`` forever. While the bond filter ran
    *after* this ``LIMIT``, every chat with more departures than the
    limit eventually filled its whole window with those rows, narrowed
    them to nothing, and stopped sweeping — silently, because the
    sweeper counts a chat as scanned only once past the narrowing, so
    even the pass log went quiet.

    Three settled departures older than the bonded one, a limit of
    three: the answer must be the bonded one, not an empty list.
    """
    for uid in (1, 2, 3):
        await _depart(session, user_id=uid, left_at=datetime(2024, 4, uid))
    await _depart(session, user_id=7, left_at=datetime(2024, 5, 1))
    _bond(session, user_id=7)
    await session.commit()

    got = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(_CHAT, _LATER, limit=3)
    assert got == [7]


async def test_a_departure_with_nothing_to_end_is_not_a_candidate(
    session: AsyncSession,
) -> None:
    """The narrowing is what makes the sweep affordable at all: every
    candidate costs one live ``get_chat_member`` before anything may be
    dissolved, and a probe spent on somebody with no bond buys nothing
    and would be re-spent on every pass forever."""
    await _depart(session, user_id=_USER, left_at=datetime(2024, 4, 1))
    await session.commit()

    got = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(_CHAT, _LATER, limit=10)
    assert got == []


async def test_the_second_half_of_a_pair_counts_too(session: AsyncSession) -> None:
    """A bond row names two people and the departed one may be either.
    Matching only ``user1_id`` would let half of all bonds outlive their
    owner's departure — and which half is an accident of who proposed."""
    await _depart(session, user_id=_USER, left_at=datetime(2024, 4, 1))
    _bond(session, user_id=_USER, reversed_pair=True)
    await session.commit()

    got = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(_CHAT, _LATER, limit=10)
    assert got == [_USER]


async def test_a_null_status_bond_is_still_a_bond(session: AsyncSession) -> None:
    """Legacy migrated prod rows without a status; reading NULL as
    finished here would make the sweep skip exactly the oldest bonds."""
    await _depart(session, user_id=_USER, left_at=datetime(2024, 4, 1))
    _bond(session, user_id=_USER, status=None)
    await session.commit()

    got = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(_CHAT, _LATER, limit=10)
    assert got == [_USER]


async def test_finished_bonds_do_not_make_a_candidate(session: AsyncSession) -> None:
    await _depart(session, user_id=1, left_at=datetime(2024, 4, 1))
    _bond(session, user_id=1, status="divorced")
    await _depart(session, user_id=2, left_at=datetime(2024, 4, 2))
    _bond(session, user_id=2, model=Relationship, status="ended")
    await session.commit()

    got = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(_CHAT, _LATER, limit=10)
    assert got == []


async def test_a_bond_in_a_different_chat_does_not_qualify(session: AsyncSession) -> None:
    await _depart(session, user_id=_USER, left_at=datetime(2024, 4, 1))
    _bond(session, user_id=_USER, chat_id=-777)
    await session.commit()

    got = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(_CHAT, _LATER, limit=10)
    assert got == []


async def test_holding_both_kinds_of_bond_costs_one_probe(session: AsyncSession) -> None:
    """Why two EXISTS and not a join: a member can be married AND in a
    relationship in the same chat, and a join would hand the sweeper the
    same person twice — two Telegram round-trips for one answer, and two
    slots out of a budget of 25."""
    await _depart(session, user_id=_USER, left_at=datetime(2024, 4, 1))
    _bond(session, user_id=_USER)
    _bond(session, user_id=_USER, model=Relationship, partner=9001)
    await session.commit()

    got = await UserGroupJoinsRepo(session).list_departed_with_active_bonds(_CHAT, _LATER, limit=10)
    assert got == [_USER]


async def test_clear_departure_heals_the_flag_without_moving_the_join_date(
    session: AsyncSession,
) -> None:
    """The sweeper's answer to "they are still here after all".

    Legacy DELETEd its ``user_chat_left`` row (bot.py:21601); our row also
    carries ``joined_at``, so the flag is cleared and the row kept. That
    is the whole difference, and this pins it: deleting instead would
    reset the since-join counter for someone who never actually left.
    """
    repo = UserGroupJoinsRepo(session)
    await repo.record_join(_USER, _CHAT, joined_at=_FIRST, source="join_event")
    await repo.mark_left(_USER, _CHAT, left_at=_LATER)
    await session.commit()

    await repo.clear_departure(_USER, _CHAT)
    await session.commit()
    session.expire_all()

    row = await session.get(UserGroupJoin, (_USER, _CHAT))
    assert row is not None
    assert row.is_active == 1
    assert row.left_at is None
    assert row.joined_at == _FIRST


async def test_clear_departure_invents_no_row(session: AsyncSession) -> None:
    await UserGroupJoinsRepo(session).clear_departure(_USER, _CHAT)
    await session.commit()

    assert (await session.execute(select(UserGroupJoin))).scalars().all() == []


async def test_clear_departure_leaves_other_memberships_alone(
    session: AsyncSession,
) -> None:
    repo = UserGroupJoinsRepo(session)
    for chat in (_CHAT, -777):
        await repo.record_join(_USER, chat, joined_at=_FIRST, source="join_event")
        await repo.mark_left(_USER, chat, left_at=_LATER)
    await session.commit()

    await repo.clear_departure(_USER, _CHAT)
    await session.commit()
    session.expire_all()

    other = await session.get(UserGroupJoin, (_USER, -777))
    assert other is not None
    assert other.is_active == 0
    assert other.left_at == _LATER
