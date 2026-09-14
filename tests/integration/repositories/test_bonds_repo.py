"""Real-SQLite tests for :class:`MarriagesRepo`, :class:`RelationshipsRepo`,
and :class:`BondsWriteRepo`.

Read-only repo invariants (Stage 19):
* The ``status IS NULL OR status = 'active'`` filter — legacy migrated
  some prod rows without setting ``status`` (bot.py:22990 / 23486).
* ``ORDER BY experience DESC, created_at ASC`` — tie-breaker on
  identical XP must be deterministic.
* OUTER join semantics: a marriage whose participant never registered
  via ``/start`` returns a row with ``user1_name=None`` so the handler
  can fall back to "Пользователь".

Write-side invariants (Stage T-019):
* ``propose_marriage`` inserts a row and returns a PK.
* ``get_latest_proposal_for`` returns most-recent by ``id DESC``.
* ``get_proposal_by_id`` is scoped to ``chat_id``.
* ``accept_proposal`` happy-path: inserts the marriage, deletes proposal.
* ``accept_proposal`` restore-path: restores a divorced pair within 3 days,
  and starts a fresh marriage on the same row once the window lapses.
* ``accept_proposal`` rel-level gate: rejects under-level proposals.
* ``accept_proposal`` already-married gate: rejects when a party is taken.
* ``decline_proposal`` removes the proposal without touching marriages.
* ``soft_divorce`` marks the marriage as divorced with restore window.
* ``soft_divorce`` returns False when not married.
* ``terminate_relationship`` hard-deletes the relationship row.
* ``terminate_relationship`` returns False when pair doesn't exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import Marriage, Relationship, User
from telegram_invite_bot.repositories.bonds_repo import (
    BondsWriteRepo,
    MarriagesRepo,
    ProposalAlreadyResolvedError,
    RelationshipsRepo,
)
from tests.integration.repositories._session import build_session


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, UsersBase, "users.db") as s:
        yield s


async def _seed_user(s: AsyncSession, *, user_id: int, first_name: str) -> None:
    s.add(User(user_id=user_id, first_name=first_name))


async def test_marriages_list_orders_by_xp_desc_then_created(
    session: AsyncSession,
) -> None:
    """Two pairs with different XP, plus a tie to pin the ASC tiebreak."""
    await _seed_user(session, user_id=1, first_name="Alice")
    await _seed_user(session, user_id=2, first_name="Bob")
    await _seed_user(session, user_id=3, first_name="Carol")
    await _seed_user(session, user_id=4, first_name="Dan")
    await _seed_user(session, user_id=5, first_name="Eve")
    await _seed_user(session, user_id=6, first_name="Frank")
    session.add_all(
        [
            Marriage(
                chat_id=100,
                user1_id=1,
                user2_id=2,
                created_at=datetime(2024, 1, 1),
                experience=500,
                status="active",
            ),
            # Higher XP — should come first.
            Marriage(
                chat_id=100,
                user1_id=3,
                user2_id=4,
                created_at=datetime(2024, 6, 1),
                experience=1000,
                status="active",
            ),
            # Tie with the first pair on XP, later created — should come second.
            Marriage(
                chat_id=100,
                user1_id=5,
                user2_id=6,
                created_at=datetime(2024, 3, 1),
                experience=500,
                status="active",
            ),
        ]
    )
    await session.commit()

    pairs = await MarriagesRepo(session).list_active(chat_id=100, limit=50)
    ids = [(p.user1_id, p.user2_id) for p in pairs]
    assert ids == [(3, 4), (1, 2), (5, 6)]
    # JOIN populated names for the top row.
    assert pairs[0].user1_name == "Carol"
    assert pairs[0].user2_name == "Dan"


async def test_marriages_treats_null_status_as_active(
    session: AsyncSession,
) -> None:
    """Legacy migration left old rows without ``status`` — the filter
    MUST treat NULL as active or those couples vanish from the
    leaderboard after a migration (silent regression, bot.py:22990).
    """
    await _seed_user(session, user_id=1, first_name="A")
    await _seed_user(session, user_id=2, first_name="B")
    session.add(
        Marriage(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=100,
            status=None,
        )
    )
    await session.commit()

    pairs = await MarriagesRepo(session).list_active(chat_id=10, limit=50)
    assert len(pairs) == 1


async def test_marriages_hides_divorced_rows(session: AsyncSession) -> None:
    await _seed_user(session, user_id=1, first_name="A")
    await _seed_user(session, user_id=2, first_name="B")
    session.add(
        Marriage(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=100,
            status="divorced",
        )
    )
    await session.commit()
    assert await MarriagesRepo(session).list_active(chat_id=10, limit=50) == []


async def test_marriages_outer_join_keeps_pair_when_user_missing(
    session: AsyncSession,
) -> None:
    """User2 never ran ``/start`` so there's no row in ``users``.
    Without an OUTER join the marriage would silently disappear from
    the leaderboard — clearly wrong; the marriage exists regardless
    of who has hit the welcome flow.
    """
    await _seed_user(session, user_id=1, first_name="OnlyUser")
    session.add(
        Marriage(
            chat_id=10,
            user1_id=1,
            user2_id=999,  # not in ``users``
            created_at=datetime(2024, 1, 1),
            experience=10,
            status="active",
        )
    )
    await session.commit()

    pairs = await MarriagesRepo(session).list_active(chat_id=10, limit=50)
    assert len(pairs) == 1
    assert pairs[0].user1_name == "OnlyUser"
    assert pairs[0].user2_name is None  # handler falls back to "Пользователь"


async def test_marriages_scoped_by_chat(session: AsyncSession) -> None:
    """A bond in chat A must not appear in chat B's leaderboard — the
    table is denormalised by ``chat_id`` and legacy queries always
    include it (bot.py:22990).
    """
    await _seed_user(session, user_id=1, first_name="A")
    await _seed_user(session, user_id=2, first_name="B")
    session.add(
        Marriage(
            chat_id=11,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=100,
            status="active",
        )
    )
    await session.commit()
    assert await MarriagesRepo(session).list_active(chat_id=22, limit=50) == []


async def test_relationships_same_filter_and_ordering(
    session: AsyncSession,
) -> None:
    """RelationshipsRepo mirrors the marriage one. Pinning the same
    invariants here so a future refactor that touches one table
    can't silently regress the other.
    """
    await _seed_user(session, user_id=1, first_name="A")
    await _seed_user(session, user_id=2, first_name="B")
    await _seed_user(session, user_id=3, first_name="C")
    await _seed_user(session, user_id=4, first_name="D")
    session.add_all(
        [
            Relationship(
                chat_id=100,
                user1_id=1,
                user2_id=2,
                created_at=datetime(2024, 1, 1),
                experience=200,
                status=None,
            ),
            Relationship(
                chat_id=100,
                user1_id=3,
                user2_id=4,
                created_at=datetime(2024, 2, 1),
                experience=1500,
                status="active",
            ),
            # Hidden — status is not in {NULL, 'active'}.
            Relationship(
                chat_id=100,
                user1_id=1,
                user2_id=3,
                created_at=datetime(2024, 3, 1),
                experience=9999,
                status="ended",
            ),
        ]
    )
    await session.commit()

    pairs = await RelationshipsRepo(session).list_active(chat_id=100, limit=50)
    assert [(p.user1_id, p.user2_id) for p in pairs] == [(3, 4), (1, 2)]


# ===========================================================================
# BondsWriteRepo — integration tests (Stage T-019)
# ===========================================================================


async def test_propose_marriage_inserts_and_returns_pk(
    session: AsyncSession,
) -> None:
    prop = await BondsWriteRepo(session).propose_marriage(chat_id=10, from_id=1, to_id=2)
    assert prop.id is not None
    assert prop.id > 0
    assert prop.from_id == 1
    assert prop.to_id == 2
    assert prop.chat_id == 10
    await session.commit()


async def test_get_latest_proposal_for_returns_most_recent(
    session: AsyncSession,
) -> None:
    repo = BondsWriteRepo(session)
    await repo.propose_marriage(chat_id=10, from_id=1, to_id=3)
    p2 = await repo.propose_marriage(chat_id=10, from_id=2, to_id=3)
    await session.flush()

    latest = await repo.get_latest_proposal_for(chat_id=10, to_id=3)
    assert latest is not None
    assert latest.id == p2.id


async def test_get_proposal_by_id_scoped_to_chat(
    session: AsyncSession,
) -> None:
    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=10, from_id=1, to_id=2)
    await session.flush()

    # Same id, wrong chat → None
    assert await repo.get_proposal_by_id(prop.id, chat_id=999) is None
    # Correct chat → row
    found = await repo.get_proposal_by_id(prop.id, chat_id=10)
    assert found is not None
    assert found.id == prop.id


async def test_accept_proposal_happy_path(
    session: AsyncSession,
) -> None:
    """With a 6-level relationship the marriage must be inserted and the
    proposal deleted."""
    # Seed a relationship at XP 60000 (level 6)
    session.add(
        Relationship(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=60000,
            status="active",
        )
    )
    await session.flush()

    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=10, from_id=1, to_id=2)
    await session.flush()
    prop_id = prop.id

    ok, err = await repo.accept_proposal(prop)
    await session.commit()

    assert ok is True
    assert err == ""

    # Proposal must be gone
    assert await repo.get_proposal_by_id(prop_id, chat_id=10) is None

    # Marriage must exist
    marriage = await repo.get_marriage(chat_id=10, user_id=1)
    assert marriage is not None
    assert {marriage.user1_id, marriage.user2_id} == {1, 2}


async def test_accept_proposal_restores_divorced_pair(
    session: AsyncSession,
) -> None:
    """Accepting a new proposal within the 3-day restore window should
    flip the existing divorced row back to active rather than inserting
    a duplicate."""
    now = datetime.now()  # noqa: DTZ005
    # Pre-seed a divorced marriage within the restore window
    session.add(
        Marriage(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=100,
            status="divorced",
            divorced_at=now - timedelta(hours=1),
            restore_until=now + timedelta(days=2),
        )
    )
    session.add(
        Relationship(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=60000,
            status="active",
        )
    )
    await session.flush()

    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=10, from_id=1, to_id=2)
    await session.flush()

    ok, err = await repo.accept_proposal(prop)
    await session.commit()

    assert ok is True

    marriage = await repo.get_marriage(chat_id=10, user_id=1)
    assert marriage is not None
    assert marriage.status == "active"
    # XP must be preserved (not reset)
    assert (marriage.experience or 0) == 100


async def test_accept_proposal_rejects_under_rel_level(
    session: AsyncSession,
) -> None:
    """No active relationship → proposal must be auto-declined."""
    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=10, from_id=1, to_id=2)
    await session.flush()
    prop_id = prop.id

    ok, err = await repo.accept_proposal(prop)
    await session.commit()

    assert ok is False
    assert "rel_level" in err or "need_rel" in err
    # Proposal must be cleaned up
    assert await repo.get_proposal_by_id(prop_id, chat_id=10) is None


async def test_accept_proposal_rejects_when_already_married(
    session: AsyncSession,
) -> None:
    """If user1 is already married to someone else the proposal must fail."""
    # user1 is married to user3 in chat 10
    session.add(
        Marriage(
            chat_id=10,
            user1_id=1,
            user2_id=3,
            created_at=datetime(2024, 1, 1),
            experience=0,
            status="active",
        )
    )
    session.add(
        Relationship(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=60000,
            status="active",
        )
    )
    await session.flush()

    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=10, from_id=1, to_id=2)
    await session.flush()

    ok, err = await repo.accept_proposal(prop)
    assert ok is False
    assert "already_married" in err


async def test_decline_proposal_removes_row(
    session: AsyncSession,
) -> None:
    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=10, from_id=1, to_id=2)
    await session.flush()
    prop_id = prop.id

    await repo.decline_proposal(prop_id)
    await session.commit()

    assert await repo.get_proposal_by_id(prop_id, chat_id=10) is None
    # No marriage created
    assert await repo.get_marriage(chat_id=10, user_id=1) is None


async def test_soft_divorce_sets_divorced_status(
    session: AsyncSession,
) -> None:
    session.add(
        Marriage(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=50,
            status="active",
        )
    )
    await session.flush()

    repo = BondsWriteRepo(session)
    result = await repo.soft_divorce(chat_id=10, user_id=1)
    await session.commit()

    assert result is True

    # Marriage should now be hidden (status='divorced')
    assert await repo.get_marriage(chat_id=10, user_id=1) is None


async def test_soft_divorce_returns_false_when_not_married(
    session: AsyncSession,
) -> None:
    result = await BondsWriteRepo(session).soft_divorce(chat_id=10, user_id=99)
    assert result is False


async def test_terminate_relationship_hard_deletes(
    session: AsyncSession,
) -> None:
    session.add(
        Relationship(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=100,
            status="active",
        )
    )
    await session.flush()

    repo = BondsWriteRepo(session)
    result = await repo.terminate_relationship(chat_id=10, user_id=1, partner_id=2)
    await session.commit()

    assert result is True
    assert await repo.get_relationship(chat_id=10, user_id=1, partner_id=2) is None


async def test_terminate_relationship_returns_false_when_no_pair(
    session: AsyncSession,
) -> None:
    result = await BondsWriteRepo(session).terminate_relationship(
        chat_id=10, user_id=1, partner_id=99
    )
    assert result is False


# ---------------------------------------------------------------------------
# R-FIX-010: atomic proposal claim
# ---------------------------------------------------------------------------


async def test_accept_proposal_raises_when_already_resolved(
    session: AsyncSession,
) -> None:
    """Second accept on the same proposal must raise and NOT create a
    second marriage row."""
    await _seed_user(session, user_id=10, first_name="Alice")
    await _seed_user(session, user_id=20, first_name="Bob")
    session.add(
        Relationship(
            chat_id=-100,
            user1_id=10,
            user2_id=20,
            created_at=datetime(2024, 1, 1),
            experience=60000,
            status="active",
        )
    )
    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=-100, from_id=10, to_id=20)
    await session.commit()

    # First accept wins
    ok, _err = await repo.accept_proposal(prop)
    await session.commit()
    assert ok is True

    # Second accept (same row, now 'accepted' or deleted) loses
    with pytest.raises(ProposalAlreadyResolvedError):
        await repo.accept_proposal(prop)


async def test_decline_proposal_raises_when_already_resolved(
    session: AsyncSession,
) -> None:
    await _seed_user(session, user_id=10, first_name="Alice")
    await _seed_user(session, user_id=20, first_name="Bob")
    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=-100, from_id=10, to_id=20)
    await session.commit()
    prop_id = prop.id

    await repo.decline_proposal(prop_id)
    await session.commit()

    with pytest.raises(ProposalAlreadyResolvedError):
        await repo.decline_proposal(prop_id)


async def test_get_latest_proposal_for_skips_resolved(
    session: AsyncSession,
) -> None:
    """An accepted proposal must not resurface to /marry_accept."""
    await _seed_user(session, user_id=10, first_name="Alice")
    await _seed_user(session, user_id=20, first_name="Bob")
    session.add(
        Relationship(
            chat_id=-100,
            user1_id=10,
            user2_id=20,
            created_at=datetime(2024, 1, 1),
            experience=60000,
            status="active",
        )
    )
    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=-100, from_id=10, to_id=20)
    await session.commit()

    ok, _ = await repo.accept_proposal(prop)
    await session.commit()
    assert ok is True

    # No more pending proposals for user 20 in chat -100
    assert await repo.get_latest_proposal_for(chat_id=-100, to_id=20) is None


async def test_accept_proposal_concurrent_double_accept_one_wins(
    tmp_path: Path,
) -> None:
    """R-FIX-010-fp: drive two concurrent ``accept_proposal`` calls on
    the SAME proposal through two SEPARATE :class:`AsyncSession`
    instances — i.e. the real race shape a double-click from the
    opponent would produce when two workers (or two coroutines on one
    worker) reach the repo at the same time.

    Invariant: exactly one call returns ``ok=True`` and the OTHER call
    raises :class:`ProposalAlreadyResolvedError`. Exactly one
    :class:`Marriage` row exists when the dust settles.

    The previous regression test (``test_accept_proposal_raises_when_
    already_resolved``) only proves the SEQUENTIAL invariant — the
    second call SEES the first call's commit. That doesn't exercise
    the SAVEPOINT/UPDATE-RETURNING flow that closes the actual race;
    a regression that re-introduced the read-then-delete pattern
    inside a single transaction would pass that test while losing
    correctness here.
    """
    import asyncio as _asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'users.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(UsersBase.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)

        # Seed users + a level-clearing relationship in one session.
        async with sm() as setup_session:
            await _seed_user(setup_session, user_id=10, first_name="Alice")
            await _seed_user(setup_session, user_id=20, first_name="Bob")
            setup_session.add(
                Relationship(
                    chat_id=-100,
                    user1_id=10,
                    user2_id=20,
                    created_at=datetime(2024, 1, 1),
                    experience=60000,
                    status="active",
                )
            )
            prop = await BondsWriteRepo(setup_session).propose_marriage(
                chat_id=-100, from_id=10, to_id=20
            )
            await setup_session.commit()

        # Now race two accept_proposal calls on two independent sessions.
        async def _accept_in_session() -> tuple[bool, str | None] | type[BaseException]:
            async with sm() as s:
                try:
                    ok, err = await BondsWriteRepo(s).accept_proposal(prop)
                    await s.commit()
                except ProposalAlreadyResolvedError:
                    return ProposalAlreadyResolvedError
                return ok, err

        results = await _asyncio.gather(
            _accept_in_session(),
            _accept_in_session(),
            return_exceptions=False,
        )

        # Exactly one (ok=True) and one ProposalAlreadyResolvedError.
        winners = [r for r in results if isinstance(r, tuple) and r[0] is True]
        losers = [r for r in results if r is ProposalAlreadyResolvedError]
        assert len(winners) == 1, results
        assert len(losers) == 1, results

        # Exactly one marriage row.
        from sqlalchemy import select

        async with sm() as audit:
            rows = (await audit.execute(select(Marriage))).scalars().all()
        assert len(rows) == 1
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# L-34 / L-35: couple-activity history log
# ---------------------------------------------------------------------------


async def test_log_and_read_marriage_activity_orders_newest_first(
    session: AsyncSession,
) -> None:
    """Logged marriage activities come back newest-first, pair canonicalised."""
    repo = BondsWriteRepo(session)
    # user2_id < user1_id on purpose to prove canonical (min,max) storage.
    await repo.log_marriage_activity(-100, 20, 10, "dinner", 15, 20)
    await repo.log_marriage_activity(-100, 10, 20, "gift", 150, 10)
    await session.flush()

    # Read with the args in yet another order — still the same pair.
    entries = await repo.get_marriage_activity_log(-100, 20, 10, limit=15)
    assert [e.activity_key for e in entries] == ["gift", "dinner"]
    assert [e.xp_gained for e in entries] == [150, 15]


async def test_marriage_log_scoped_by_pair_and_chat(session: AsyncSession) -> None:
    """A different pair / chat must not leak into the history."""
    repo = BondsWriteRepo(session)
    await repo.log_marriage_activity(-100, 10, 20, "dinner", 15, 10)
    await repo.log_marriage_activity(-100, 30, 40, "gift", 150, 30)  # other pair
    await repo.log_marriage_activity(-999, 10, 20, "date", 50, 10)  # other chat
    await session.flush()

    entries = await repo.get_marriage_activity_log(-100, 10, 20, limit=15)
    assert [e.activity_key for e in entries] == ["dinner"]


async def test_relationship_log_respects_limit(session: AsyncSession) -> None:
    """Only the most recent ``limit`` relationship rows are returned."""
    repo = BondsWriteRepo(session)
    for i in range(20):
        await repo.log_relationship_activity(-100, 10, 20, f"act{i}", i, 10)
    await session.flush()

    entries = await repo.get_relationship_activity_log(-100, 10, 20, limit=15)
    assert len(entries) == 15
    # Newest first: act19 .. act5
    assert entries[0].activity_key == "act19"
    assert entries[-1].activity_key == "act5"


async def test_empty_history_returns_empty_list(session: AsyncSession) -> None:
    """No rows logged → empty list (handler renders the 'no records' copy)."""
    repo = BondsWriteRepo(session)
    assert await repo.get_marriage_activity_log(-100, 10, 20) == []
    assert await repo.get_relationship_activity_log(-100, 10, 20) == []


# ---------------------------------------------------------------------------
# Relationship XP inactivity decay (AUD-4: 5 XP / day, bot.py:21884)
# ---------------------------------------------------------------------------


async def _seed_relationship(s: AsyncSession, *, exp: int, days_ago: int | None) -> None:
    last = None if days_ago is None else datetime.now() - timedelta(days=days_ago)  # noqa: DTZ005
    await _seed_user(s, user_id=1, first_name="A")
    await _seed_user(s, user_id=2, first_name="B")
    s.add(
        Relationship(
            chat_id=100,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=exp,
            last_activity_at=last,
            status="active",
        )
    )
    await s.commit()


async def test_get_relationship_decays_inactive_xp_and_persists(
    session: AsyncSession,
) -> None:
    await _seed_relationship(session, exp=200, days_ago=3)
    repo = BondsWriteRepo(session)
    rel = await repo.get_relationship(100, 1, 2)
    assert rel is not None
    assert rel.experience == 200 - 3 * 5  # 185
    await session.commit()
    # Persisted: a fresh read sees the decayed value, and same-day re-read
    # consumes 0 further days (idempotent).
    again = await BondsWriteRepo(session).get_relationship(100, 1, 2)
    assert again is not None
    assert again.experience == 185


async def test_get_relationship_no_decay_same_day(session: AsyncSession) -> None:
    await _seed_relationship(session, exp=200, days_ago=0)
    rel = await BondsWriteRepo(session).get_relationship(100, 1, 2)
    assert rel is not None
    assert rel.experience == 200


async def test_get_relationship_decay_floors_at_zero(session: AsyncSession) -> None:
    await _seed_relationship(session, exp=10, days_ago=100)
    rel = await BondsWriteRepo(session).get_relationship(100, 1, 2)
    assert rel is not None
    assert rel.experience == 0


async def test_get_relationship_null_last_activity_no_decay(
    session: AsyncSession,
) -> None:
    await _seed_relationship(session, exp=200, days_ago=None)
    rel = await BondsWriteRepo(session).get_relationship(100, 1, 2)
    assert rel is not None
    assert rel.experience == 200


async def test_decay_can_drop_below_marriage_eligibility_level(
    session: AsyncSession,
) -> None:
    # Level 6 (marriage-eligible) needs 60000 XP; after enough decay the
    # pair falls below it — the gate must see the decayed level.
    await _seed_relationship(session, exp=60010, days_ago=5)  # -25 -> 59985
    repo = BondsWriteRepo(session)
    rel = await repo.get_relationship(100, 1, 2)
    assert rel is not None
    assert rel.experience == 59985
    assert repo._rel_xp_to_level(rel.experience) == 5  # below level 6


async def test_list_active_orders_by_decayed_xp(session: AsyncSession) -> None:
    await _seed_user(session, user_id=1, first_name="A")
    await _seed_user(session, user_id=2, first_name="B")
    await _seed_user(session, user_id=3, first_name="C")
    await _seed_user(session, user_id=4, first_name="D")
    session.add_all(
        [
            # Higher raw XP but heavily decayed (90 days * 5 = 450 off).
            Relationship(
                chat_id=100,
                user1_id=1,
                user2_id=2,
                created_at=datetime(2024, 1, 1),
                experience=500,
                last_activity_at=datetime.now() - timedelta(days=90),  # noqa: DTZ005, status="active",
            ),
            # Lower raw XP but fresh — should now rank FIRST.
            Relationship(
                chat_id=100,
                user1_id=3,
                user2_id=4,
                created_at=datetime(2024, 2, 1),
                experience=200,
                last_activity_at=datetime.now(),  # noqa: DTZ005, status="active",
            ),
        ]
    )
    await session.commit()
    pairs = await RelationshipsRepo(session).list_active(chat_id=100, limit=50)
    assert [(p.user1_id, p.user2_id) for p in pairs] == [(3, 4), (1, 2)]
    assert pairs[0].experience == 200  # fresh pair, undecayed
    assert pairs[1].experience == 50  # 500 - 450


def test_decayed_experience_tolerates_str_last_activity() -> None:
    """REV-2: a legacy-migrated row whose last_activity_at decodes as an ISO
    string must NOT crash the decay math (legacy parsed it defensively)."""
    from telegram_invite_bot.repositories.bonds_repo import _decayed_experience

    now = datetime(2024, 1, 11, 12, 0, 0)
    # 3 days earlier as an ISO string (incl. a 'Z' suffix to exercise the strip)
    new_exp, days = _decayed_experience(200, "2024-01-08T12:00:00Z", now)
    assert days == 3
    assert new_exp == 200 - 3 * 5
    # An unparseable value degrades to "no decay", never raises.
    assert _decayed_experience(200, "not-a-date", now) == (200, 0)


async def test_a_stale_decay_cannot_erase_an_xp_grant_that_landed_first(
    tmp_path: Path,
) -> None:
    """#1932: the decay write is a compare-and-set, not a blind overwrite.

    The SELECT that loads a bond runs in autocommit, so two overlapping
    updates on one couple (a double-tapped activity, or one partner on
    ``/rp`` while the other runs an activity) both read the same
    pre-decay row. The loser used to flush an ABSOLUTE
    ``experience = <its own stale figure>``, silently erasing the XP the
    winner had already granted with an in-SQL increment — the user paid
    the activity's cost and got nothing back.

    The interleaving is written out step by step rather than raced, so
    the assertion cannot pass by luck of scheduling.
    """
    async with build_session(tmp_path, UsersBase, "users.db") as a_session:
        await _seed_relationship(a_session, exp=200, days_ago=3)

        async with build_session(tmp_path, UsersBase, "users.db") as b_session:
            # B reads the pre-decay row and then stalls (its own decay
            # write is deferred to the end of this test).
            b_repo = BondsWriteRepo(b_session)
            b_rel = (
                await b_session.execute(select(Relationship).where(Relationship.id == 1))
            ).scalar_one()
            assert b_rel.experience == 200

            # A completes a whole activity: decay 200 -> 185, then +10 XP.
            a_repo = BondsWriteRepo(a_session)
            a_rel = await a_repo.get_relationship(100, 1, 2)
            assert a_rel is not None
            assert a_rel.experience == 185
            assert await a_repo.add_relationship_xp(100, 1, 2, 10) == 195
            await a_session.commit()

            # B now writes. Its figure (185) is stale by exactly A's grant.
            await b_repo._apply_decay(b_rel)  # noqa: SLF001 — the unit under test
            await b_session.commit()

            # A's +10 survives, and B sees the current row rather than the
            # one it loaded — the docstring's contract on a lost race.
            assert b_rel.experience == 195

        after = (
            await a_session.execute(select(Relationship.experience).where(Relationship.id == 1))
        ).scalar_one()
        assert after == 195


# --- Cross-chat one-sided reads for the profile social panel (RR-1 #2) ------


async def test_marriages_list_for_user_spans_chats_and_picks_the_partner(
    session: AsyncSession,
) -> None:
    """The DM hub has no chat to scope by, so this read must cross groups —
    and must report the *other* party regardless of column order."""
    for uid, name in ((1, "Alice"), (2, "Bob"), (3, "Carol"), (9, "Zed")):
        await _seed_user(session, user_id=uid, first_name=name)
    session.add_all(
        [
            # Caller is user1 here…
            Marriage(
                chat_id=100,
                user1_id=1,
                user2_id=2,
                created_at=datetime(2024, 1, 1),
                experience=100,
                status="active",
            ),
            # …and user2 here, in a different chat.
            Marriage(
                chat_id=200,
                user1_id=3,
                user2_id=1,
                created_at=datetime(2024, 2, 1),
                experience=900,
                status="active",
            ),
            # Divorced — must not surface.
            Marriage(
                chat_id=300,
                user1_id=1,
                user2_id=9,
                created_at=datetime(2024, 3, 1),
                experience=5000,
                status="divorced",
            ),
        ]
    )
    await session.commit()
    bonds = await MarriagesRepo(session).list_for_user(1, limit=10)
    assert [(b.chat_id, b.partner_id, b.partner_name) for b in bonds] == [
        (200, 3, "Carol"),
        (100, 2, "Bob"),
    ]


async def test_marriages_list_for_user_limits_in_sql(session: AsyncSession) -> None:
    """``limit`` must bound the transfer, not just the render — a user
    married in many groups shouldn't turn one card into a full scan."""
    await _seed_user(session, user_id=1, first_name="Alice")
    session.add_all(
        [
            Marriage(
                chat_id=100 + i,
                user1_id=1,
                user2_id=50 + i,
                created_at=datetime(2024, 1, 1),
                experience=i,
                status="active",
            )
            for i in range(6)
        ]
    )
    await session.commit()
    bonds = await MarriagesRepo(session).list_for_user(1, limit=3)
    assert len(bonds) == 3
    # Highest XP first, and a partner with no ``users`` row degrades to None
    # (the handler renders an ``ID<n>`` stub) instead of dropping the row.
    assert [b.experience for b in bonds] == [5, 4, 3]
    assert all(b.partner_name is None for b in bonds)


async def test_relationships_list_for_user_applies_decay_before_the_limit(
    session: AsyncSession,
) -> None:
    """Decay is applied on read exactly as on the leaderboard, so the cut
    has to happen *after* it — a stale high-XP pair must not displace a
    fresh lower-XP one."""
    for uid, name in ((1, "Alice"), (2, "Bob"), (3, "Carol")):
        await _seed_user(session, user_id=uid, first_name=name)
    now = datetime.now()  # noqa: DTZ005 — matches the writers' naive-local frame
    session.add_all(
        [
            # 500 XP but 90 days idle ⇒ decays to 50.
            Relationship(
                chat_id=100,
                user1_id=1,
                user2_id=2,
                created_at=datetime(2024, 1, 1),
                experience=500,
                last_activity_at=now - timedelta(days=90),
                status="active",
            ),
            # 200 XP and active ⇒ stays 200, so it must win.
            Relationship(
                chat_id=200,
                user1_id=3,
                user2_id=1,
                created_at=datetime(2024, 2, 1),
                experience=200,
                last_activity_at=now,
                status="active",
            ),
        ]
    )
    await session.commit()
    bonds = await RelationshipsRepo(session).list_for_user(1, limit=1)
    assert len(bonds) == 1
    assert bonds[0].partner_id == 3
    assert bonds[0].experience == 200


# ── R15: a failed insert must not strand the recovery path ───────────────────


async def test_accept_proposal_reports_save_error_instead_of_crashing(
    session: AsyncSession,
) -> None:
    """``marry_save_error`` is written, translated and reachable.

    ``_create_marriage`` catches a failed insert, deletes the proposal
    and returns ``(False, "marry_save_error")`` so the handler can show
    "не удалось сохранить брак, попробуйте ещё раз". That recovery does
    another DB call, which a failed flush would have made impossible:
    without the SAVEPOINT the transaction is already deactivated, the
    ``delete_proposal`` raises, and the user gets a crash where a retry
    prompt was written for them.

    A ``BEFORE INSERT`` trigger is the failure injector because it fails
    the same way the real constraint does — inside the flush, on the
    connection — rather than as a mock that never reached the DB.
    ``RAISE(ABORT, …)`` maps to SQLITE_CONSTRAINT_TRIGGER, so it arrives
    as :class:`~sqlalchemy.exc.IntegrityError`, which is exactly the
    class ``_create_marriage`` narrowed to in #433.
    """
    session.add(
        Relationship(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=60000,
            status="active",
        )
    )
    await session.flush()

    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=10, from_id=1, to_id=2)
    await session.flush()
    prop_id = prop.id

    await session.execute(
        text(
            "CREATE TRIGGER no_marriage BEFORE INSERT ON marriages "
            "BEGIN SELECT RAISE(ABORT, 'injected schema failure'); END"
        )
    )

    ok, err = await repo.accept_proposal(prop)
    assert (ok, err) == (False, "marry_save_error")

    # The recovery path really ran, and the session is still usable.
    await session.execute(text("DROP TRIGGER no_marriage"))
    await session.commit()
    assert await repo.get_proposal_by_id(prop_id, chat_id=10) is None
    assert await repo.get_marriage(chat_id=10, user_id=1) is None


# ── #433: the UNIQUE(chat_id, user1_id, user2_id) the ORM now declares ───────


async def test_second_accept_for_the_same_pair_cannot_duplicate_the_row(
    session: AsyncSession,
) -> None:
    """Accepting a second proposal for an already-married pair is refused.

    The free-partner loop in ``accept_proposal`` lets this through on
    purpose — the existing row belongs to *this* pair, which is what the
    3-day restore flow needs — and the restore UPDATE matches only
    ``status='divorced'``, so an already-active pair falls into the fresh
    INSERT. Production has always rejected that insert on
    ``UNIQUE(chat_id, user1_id, user2_id)``
    (``docs/prod_schemas.sql:79``); test DBs are built by
    ``metadata.create_all`` rather than by migrations, so until #433
    declared the constraint on the model they happily stored a SECOND
    active marriage for the pair and nothing here noticed.
    """
    session.add(
        Relationship(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=60000,
            status="active",
        )
    )
    await session.flush()

    repo = BondsWriteRepo(session)
    first = await repo.propose_marriage(chat_id=10, from_id=1, to_id=2)
    await session.flush()
    assert await repo.accept_proposal(first) == (True, "")

    second = await repo.propose_marriage(chat_id=10, from_id=1, to_id=2)
    await session.flush()
    assert await repo.accept_proposal(second) == (False, "marry_save_error")

    rows = (
        await session.execute(text("SELECT COUNT(*) FROM marriages WHERE chat_id = 10"))
    ).scalar_one()
    assert rows == 1
    # The recovery path ran and the session survived the failed flush.
    assert await repo.get_proposal_by_id(second.id, chat_id=10) is None


async def test_marriage_after_the_restore_window_lapses_starts_a_new_one(
    session: AsyncSession,
) -> None:
    """A pair whose divorce aged out of the 3-day window marries again.

    This used to pin a LEGACY dead end (#2018). ``bot.py:22551``
    restores only while ``restore_until > now``, ``bot.py:22557`` then
    inserts a fresh row that ``UNIQUE(chat_id, user1_id, user2_id)``
    rejects, and ``bot.py:22561`` turns that into ``marry_save_error``
    — so a pair that divorced and waited four days was told "couldn't
    save, try again" forever, with no command, no admin screen and no
    passage of time that could ever get them out of it. There is no
    ``DELETE FROM marriages`` anywhere in the package, so the row that
    blocks them is permanent.

    What kept the defect pinned was one product question: does the old
    marriage's ``experience`` carry over? The package had already
    answered it twice — the in-window restore a few lines up keeps the
    XP, and ``_create_relationship`` reactivates an ``ended`` pair
    keeping theirs — so the lapsed path now says the same thing rather
    than inventing a third rule. ``created_at`` *is* reset, also after
    ``_create_relationship``: the longevity tier on the ``/marriage``
    card is computed from it (``utils/bonds.marriage_category``), and a
    couple who split up for a year did not spend that year married.
    """
    session.add(
        Relationship(
            chat_id=11,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=60000,
            status="active",
        )
    )
    session.add(
        Marriage(
            chat_id=11,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=500,
            status="divorced",
            divorced_at=datetime(2024, 1, 10),
            restore_until=datetime(2024, 1, 13),
        )
    )
    await session.flush()

    repo = BondsWriteRepo(session)
    prop = await repo.propose_marriage(chat_id=11, from_id=1, to_id=2)
    await session.flush()
    assert await repo.accept_proposal(prop) == (True, "")

    married = await repo.get_marriage(chat_id=11, user_id=1)
    assert married is not None, "the pair is still stuck outside their own marriage"
    assert married.status == "active"
    assert married.divorced_at is None
    assert married.restore_until is None

    # One row, reused: a second one cannot exist — the UNIQUE index is
    # the whole reason this path was stuck — and the XP rides along, as
    # it does on both sibling paths.
    rows = (
        await session.execute(
            text("SELECT COUNT(*), MAX(experience) FROM marriages WHERE chat_id = 11")
        )
    ).one()
    assert rows == (1, 500)
    # A new marriage, not a resumed one: the anniversary is today.
    assert (datetime.now() - married.created_at).days == 0  # noqa: DTZ005


# ── #477: the /relationship list must rank by DECAYED xp, and must not write ──


async def test_list_relationships_for_ranks_by_decayed_xp(
    session: AsyncSession,
) -> None:
    """A bond that has decayed to nothing must not outrank a live one.

    The ordering is not cosmetic: ``couple_activities.handle_activities``
    takes ``rels[0]`` as *the* pair its activity menu acts on, so the
    stale stored column pointing the wrong way aims the menu at the wrong
    partner. Legacy had no ordering here at all
    (``bot.py:21918-21922`` — no ``ORDER BY``), so there is nothing to
    mirror; ranking by the value every caller actually renders is the
    only self-consistent choice.
    """
    for uid, name in ((1, "Alice"), (2, "Bob"), (3, "Carol")):
        await _seed_user(session, user_id=uid, first_name=name)
    now = datetime.now()  # noqa: DTZ005 — matches the writers' naive-local frame
    session.add_all(
        [
            # 500 stored XP, 200 days idle ⇒ decays to 0.
            Relationship(
                chat_id=100,
                user1_id=1,
                user2_id=2,
                created_at=datetime(2024, 1, 1),
                experience=500,
                last_activity_at=now - timedelta(days=200),
                status="active",
            ),
            # 120 stored XP, active ⇒ stays 120, so it must come first.
            Relationship(
                chat_id=100,
                user1_id=1,
                user2_id=3,
                created_at=datetime(2024, 2, 1),
                experience=120,
                last_activity_at=now,
                status="active",
            ),
        ]
    )
    await session.commit()

    rels = await BondsWriteRepo(session).list_relationships_for(100, 1)

    assert [r.user2_id for r in rels] == [3, 2]
    assert [r.experience for r in rels] == [120, 0]


async def test_list_relationships_for_does_not_persist_the_decay(
    session: AsyncSession,
) -> None:
    """Listing is a read. Decay is persisted by ``get_relationship``.

    Returning attached ORM rows with the decayed value written onto them
    would make a display call quietly write to every pair the caller is
    in, with no activity to justify it. The frozen view returned instead
    cannot; this pins that the stored column is untouched.
    """
    for uid, name in ((1, "Alice"), (2, "Bob")):
        await _seed_user(session, user_id=uid, first_name=name)
    now = datetime.now()  # noqa: DTZ005 — matches the writers' naive-local frame
    session.add(
        Relationship(
            chat_id=100,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=500,
            last_activity_at=now - timedelta(days=10),
            status="active",
        )
    )
    await session.commit()

    rels = await BondsWriteRepo(session).list_relationships_for(100, 1)
    assert rels[0].experience == 450  # 500 - 10*5, shown but not stored
    await session.commit()

    stored = (
        await session.execute(text("SELECT experience FROM relationships WHERE chat_id = 100"))
    ).scalar_one()
    assert int(stored) == 500


# ---------------------------------------------------------------------------
# #482: bulk dissolution for members who left the group
# ---------------------------------------------------------------------------

_SWEEP_NOW = datetime(2025, 6, 1, 12, 0)


async def test_soft_divorce_all_in_chat_uses_the_clock_it_is_given(
    session: AsyncSession,
) -> None:
    """The reason this is not a loop over ``soft_divorce``: that method
    reads ``datetime.now()`` itself, which leaves a background sweeper
    nothing to pin. Restore window must be exactly three days from the
    injected instant — the same grace a ``/divorce`` gets."""
    session.add(
        Marriage(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=50,
            status="active",
        )
    )
    await session.commit()

    count = await BondsWriteRepo(session).soft_divorce_all_in_chat(10, 1, now=_SWEEP_NOW)
    await session.commit()
    session.expire_all()

    assert count == 1
    row = (await session.execute(text("SELECT * FROM marriages"))).mappings().one()
    assert row["status"] == "divorced"
    assert str(row["divorced_at"]).startswith("2025-06-01 12:00")
    assert str(row["restore_until"]).startswith("2025-06-04 12:00")


async def test_soft_divorce_all_in_chat_sweeps_every_active_marriage(
    session: AsyncSession,
) -> None:
    """Legacy swept ALL of a departed member's active marriages
    (bot.py:21607-21612); ``soft_divorce``'s own lookup is LIMIT 1, so
    reusing it would leave the second one standing."""
    for partner in (2, 3):
        session.add(
            Marriage(
                chat_id=10,
                user1_id=1,
                user2_id=partner,
                created_at=datetime(2024, 1, 1),
                experience=0,
                status="active",
            )
        )
    await session.commit()

    count = await BondsWriteRepo(session).soft_divorce_all_in_chat(10, 1, now=_SWEEP_NOW)
    await session.commit()

    assert count == 2


async def test_soft_divorce_all_in_chat_matches_either_side_of_the_pair(
    session: AsyncSession,
) -> None:
    session.add(
        Marriage(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=0,
            status="active",
        )
    )
    await session.commit()

    count = await BondsWriteRepo(session).soft_divorce_all_in_chat(10, 2, now=_SWEEP_NOW)
    await session.commit()

    assert count == 1


async def test_soft_divorce_all_in_chat_is_scoped_and_idempotent(
    session: AsyncSession,
) -> None:
    session.add(
        Marriage(
            chat_id=99,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=0,
            status="active",
        )
    )
    await session.commit()
    repo = BondsWriteRepo(session)

    assert await repo.soft_divorce_all_in_chat(10, 1, now=_SWEEP_NOW) == 0
    assert await repo.soft_divorce_all_in_chat(99, 1, now=_SWEEP_NOW) == 1
    await session.commit()
    assert await repo.soft_divorce_all_in_chat(99, 1, now=_SWEEP_NOW) == 0


async def test_end_relationships_for_marks_ended_and_keeps_the_row(
    session: AsyncSession,
) -> None:
    """The load-bearing distinction of the whole ticket.

    ``terminate_relationship`` is a hard DELETE (legacy's ``/rel_break``,
    bot.py:22064). Reaching for it here would erase the ``created_at``
    and accumulated experience of every couple whose member merely
    stopped being in the group — data the pair gets back untouched if
    they return. The absence sweep ends the bond; it does not unwrite
    its history.
    """
    session.add(
        Relationship(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=300,
            status="active",
        )
    )
    await session.commit()

    count = await BondsWriteRepo(session).end_relationships_for(10, 1, now=_SWEEP_NOW)
    await session.commit()
    session.expire_all()

    assert count == 1
    row = (await session.execute(text("SELECT * FROM relationships"))).mappings().one()
    assert row["status"] == "ended"
    assert row["experience"] == 300
    assert str(row["created_at"]).startswith("2024-01-01")
    assert str(row["ended_at"]).startswith("2025-06-01 12:00")


async def test_end_relationships_for_sweeps_null_status_rows(
    session: AsyncSession,
) -> None:
    session.add(
        Relationship(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=0,
            status=None,
        )
    )
    await session.commit()

    count = await BondsWriteRepo(session).end_relationships_for(10, 2, now=_SWEEP_NOW)
    await session.commit()

    assert count == 1


async def test_end_relationships_for_is_scoped_and_idempotent(
    session: AsyncSession,
) -> None:
    session.add(
        Relationship(
            chat_id=99,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=0,
            status="active",
        )
    )
    await session.commit()
    repo = BondsWriteRepo(session)

    assert await repo.end_relationships_for(10, 1, now=_SWEEP_NOW) == 0
    assert await repo.end_relationships_for(99, 1, now=_SWEEP_NOW) == 1
    await session.commit()
    assert await repo.end_relationships_for(99, 1, now=_SWEEP_NOW) == 0


async def test_the_two_sweep_writers_do_not_touch_each_other_s_table(
    session: AsyncSession,
) -> None:
    """A marriage and a relationship are separate bonds; ending one must
    never end the other, or a departed member's partner would lose a
    bond nobody swept."""
    session.add(
        Marriage(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=0,
            status="active",
        )
    )
    session.add(
        Relationship(
            chat_id=10,
            user1_id=1,
            user2_id=2,
            created_at=datetime(2024, 1, 1),
            experience=0,
            status="active",
        )
    )
    await session.commit()
    repo = BondsWriteRepo(session)

    await repo.end_relationships_for(10, 1, now=_SWEEP_NOW)
    await session.commit()
    session.expire_all()

    marriage = (await session.execute(text("SELECT status FROM marriages"))).scalar_one()
    assert marriage == "active"
