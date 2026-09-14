"""``session_for`` commit/rollback contract.

Handlers receive an ``AsyncSession`` via dishka REQUEST scope and the
:class:`BaseSessionMiddleware` lifecycle — ``session_for`` is the
explicit alternative for background tasks, migrations, and ad-hoc
scripts. Same commit-on-clean-exit / rollback-on-exception contract,
but no middleware in front of it; the helper IS the contract.

A regression that flipped the try/except → try/finally (or forgot to
re-raise) would silently commit half-written state from a crashed
background job. These mock-only tests lock both branches.

The second half of the file covers :class:`Checkpoint` (#1493): that it
commits in ``_COMMIT_ORDER`` rather than in tracking order, that one
failing DB does not cancel the rest, and that a partial outcome says so
in the journal. All three are invisible in normal operation and only
show up once a commit has already failed, which is precisely when
nobody is in a position to discover them by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import Checkpoint, session_for
from telegram_invite_bot.webhook.metrics import CHECKPOINT_TEARS

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _fake_registry_with_session(session: AsyncMock) -> MagicMock:
    """Mirror of the helper in ``tests/unit/middlewares/test_base.py``:
    return a registry whose ``session(db)`` yields a sessionmaker that
    yields ``session`` via ``async with``.
    """
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    sessionmaker = MagicMock(return_value=session)
    registry = MagicMock()
    registry.session.return_value = sessionmaker
    return registry


@pytest.mark.asyncio
async def test_session_for_commits_on_clean_exit() -> None:
    session = AsyncMock()
    registry = _fake_registry_with_session(session)

    async with session_for(registry, DBName.USERS) as resolved:
        assert resolved is session

    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_for_rolls_back_and_reraises_on_exception() -> None:
    """The "must re-raise" half of the contract — swallowing the
    exception here would let a crashed background task look like a
    successful run.
    """
    session = AsyncMock()
    registry = _fake_registry_with_session(session)

    sentinel = RuntimeError("body exploded")

    with pytest.raises(RuntimeError) as excinfo:
        async with session_for(registry, DBName.ECONOMY):
            raise sentinel

    assert excinfo.value is sentinel
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_for_routes_to_requested_db_name() -> None:
    """Sanity: the ``db`` argument is what gets passed to
    ``registry.session(...)``. A typo in a caller would silently
    write to the wrong DB; this test makes that impossible to
    regress without breaking.
    """
    session = AsyncMock()
    registry = _fake_registry_with_session(session)

    async with session_for(registry, DBName.MODERATION):
        pass

    registry.session.assert_called_once_with(DBName.MODERATION)


class _FakeSession:
    """Records its own commit into a shared log; optionally fails.

    ``Checkpoint`` calls exactly two things on a tracked session —
    ``in_transaction()`` (through ``session_was_touched``) and
    ``commit()`` — so two methods are the whole double. An ``AsyncMock``
    would not do here: the point of these tests is the ORDER of the
    commits, and separate mocks cannot record a shared sequence without
    the same side-effect plumbing.
    """

    def __init__(
        self, name: str, log: list[str], *, fails: bool = False, touched: bool = True
    ) -> None:
        self.name = name
        self._log = log
        self._fails = fails
        self._touched = touched

    def in_transaction(self) -> bool:
        return self._touched

    async def commit(self) -> None:
        self._log.append(self.name)
        if self._fails:
            raise RuntimeError(f"{self.name} lost")


def _fake(name: str, log: list[str], *, fails: bool = False, touched: bool = True) -> AsyncSession:
    """The double under the annotated type — see :class:`_FakeSession`."""
    return cast("AsyncSession", _FakeSession(name, log, fails=fails, touched=touched))


@pytest.mark.asyncio
async def test_the_checkpoint_commits_in_a_fixed_order_not_the_tracking_order() -> None:
    """Sessions are tracked worst-order-first and still come out right.

    Mount order is what decides this without #1493, and mount order is
    an accident of how the routers are wired. The ledger has to be the
    half that survives: money credited with the moderation half missing
    is a support ticket, money NOT credited with the effect applied is
    free and repeatable.
    """
    log: list[str] = []
    checkpoint = Checkpoint()
    for db in (
        DBName.ACTIVITY,
        DBName.MESSAGE_STATS,
        DBName.USERS,
        DBName.MODERATION,
        DBName.ECONOMY,
    ):
        checkpoint.track(_fake(db.value, log), db)

    await checkpoint()

    assert log == ["economy", "moderation", "users", "message_stats", "activity"]


@pytest.mark.asyncio
async def test_a_session_tracked_without_a_db_name_commits_last() -> None:
    """``track`` keeps ``db`` optional for callers that only have a
    session (four such call sites exist in the tests). An unnamed
    session has nothing to sort on, so it goes after every named one
    rather than jumping the ledger.
    """
    log: list[str] = []
    checkpoint = Checkpoint()
    checkpoint.track(_fake("nameless", log))
    checkpoint.track(_fake("activity", log), DBName.ACTIVITY)
    checkpoint.track(_fake("economy", log), DBName.ECONOMY)

    await checkpoint()

    assert log == ["economy", "activity", "nameless"]


@pytest.mark.asyncio
async def test_a_failing_commit_does_not_cancel_the_ones_behind_it() -> None:
    """Stopping at the first failure would leave a prefix on disk chosen
    by nothing in particular. Rolling the prefix back is not available
    (it is already committed), so the only useful answer is to commit
    everything that still can be and say what happened.
    """
    log: list[str] = []
    checkpoint = Checkpoint()
    checkpoint.track(_fake("economy", log, fails=True), DBName.ECONOMY)
    checkpoint.track(_fake("users", log), DBName.USERS)
    checkpoint.track(_fake("activity", log), DBName.ACTIVITY)

    with pytest.raises(RuntimeError, match="economy lost"):
        await checkpoint()

    assert log == ["economy", "users", "activity"]


@pytest.mark.asyncio
async def test_the_first_failure_is_the_one_the_caller_sees() -> None:
    """Two failures, and the one raised is the first in commit order —
    the later one would name a bookkeeping DB and send the reader
    looking in the wrong place.
    """
    log: list[str] = []
    checkpoint = Checkpoint()
    checkpoint.track(_fake("users", log, fails=True), DBName.USERS)
    checkpoint.track(_fake("economy", log, fails=True), DBName.ECONOMY)

    with pytest.raises(RuntimeError, match="economy lost"):
        await checkpoint()

    assert log == ["economy", "users"]


@pytest.mark.asyncio
async def test_a_partial_commit_names_both_halves_in_the_journal() -> None:
    """Without this line a partial commit reads exactly like a clean
    rollback, and an investigation into a divergence has nowhere to
    start. The context comes from the middleware and identifies the
    update behind it.
    """
    log: list[str] = []
    records: list[str] = []
    handler_id = logger.add(records.append, level="ERROR", format="{message}")
    try:
        checkpoint = Checkpoint(context="update_id=7 user_id=42")
        checkpoint.track(_fake("economy", log), DBName.ECONOMY)
        checkpoint.track(_fake("users", log, fails=True), DBName.USERS)

        with pytest.raises(RuntimeError, match="users lost"):
            await checkpoint()
    finally:
        logger.remove(handler_id)

    assert len(records) == 1
    assert "committed=['economy']" in records[0]
    assert "failed=['users']" in records[0]
    assert "update_id=7 user_id=42" in records[0]


def _tear_count(failed: str) -> float:
    """Current value of the tear counter for one failing DB.

    ``prometheus_client`` keeps counters in a process-global registry
    and the tests do not reset it, so every assertion here diffs
    against a snapshot — the same convention ``webhook/metrics.py``
    documents.
    """
    return CHECKPOINT_TEARS.labels(failed=failed)._value.get()


@pytest.mark.asyncio
async def test_a_torn_checkpoint_is_counted_not_just_logged() -> None:
    """#1989: the journal line is the only trace a tear leaves, and
    nothing reads the journal.

    A checkpoint that commits ``economy`` and then fails ``users`` is
    money taken for an effect that did not land — the exact failure
    ``_COMMIT_ORDER`` chooses to have, precisely because it is the one
    a human can see and refund. But "a human can see it" has so far
    meant "a human who greps the journal for a string nobody knows to
    grep for". A counter is what makes it alertable, and the label
    says which half was lost.
    """
    log: list[str] = []
    before = _tear_count("users")
    checkpoint = Checkpoint(context="update_id=7 user_id=42")
    checkpoint.track(_fake("economy", log), DBName.ECONOMY)
    checkpoint.track(_fake("users", log, fails=True), DBName.USERS)

    with pytest.raises(RuntimeError, match="users lost"):
        await checkpoint()

    assert _tear_count("users") == before + 1


@pytest.mark.asyncio
async def test_a_checkpoint_that_committed_nothing_is_not_a_tear() -> None:
    """The distinction the counter exists to make.

    One session, one failure, nothing on disk: that is an ordinary
    failed commit, the middleware rolls back and the update is undone
    whole. Counting it as a tear would bury the real ones — the alert
    would fire on every transient ``database is locked`` and get muted
    inside a week, which is how the reversal alerts nearly died.

    The journal line is also corrected here: it used to announce a
    "partial commit" with ``committed=[]``, which is not partial and
    not a commit.
    """
    log: list[str] = []
    records: list[str] = []
    before = _tear_count("economy")
    handler_id = logger.add(records.append, level="ERROR", format="{message}")
    try:
        checkpoint = Checkpoint(context="update_id=7 user_id=42")
        checkpoint.track(_fake("economy", log, fails=True), DBName.ECONOMY)

        with pytest.raises(RuntimeError, match="economy lost"):
            await checkpoint()
    finally:
        logger.remove(handler_id)

    assert _tear_count("economy") == before
    assert len(records) == 1
    assert "partial commit" not in records[0]
    assert "failed=['economy']" in records[0]


@pytest.mark.asyncio
async def test_a_clean_checkpoint_says_nothing_at_all() -> None:
    """The ERROR line must stay rare enough to be worth reading."""
    log: list[str] = []
    records: list[str] = []
    handler_id = logger.add(records.append, level="ERROR", format="{message}")
    try:
        checkpoint = Checkpoint(context="update_id=7 user_id=42")
        checkpoint.track(_fake("economy", log), DBName.ECONOMY)
        checkpoint.track(_fake("users", log), DBName.USERS)

        await checkpoint()
    finally:
        logger.remove(handler_id)

    assert log == ["economy", "users"]
    assert records == []


@pytest.mark.asyncio
async def test_a_session_nobody_wrote_on_is_still_skipped() -> None:
    """The ordering rewrite must not have cost the lazy-skip gate: an
    untouched session has no transaction to end and committing it is a
    wasted round-trip on the hot path.
    """
    log: list[str] = []
    checkpoint = Checkpoint()
    checkpoint.track(_fake("economy", log, touched=False), DBName.ECONOMY)
    checkpoint.track(_fake("users", log), DBName.USERS)

    await checkpoint()

    assert log == ["users"]


@pytest.mark.asyncio
async def test_forget_drops_the_session_it_was_handed_and_no_other() -> None:
    """``forget`` scans by identity now that the tracked item is a
    ``(db, session)`` pair. An equality-based scan would be a trap: two
    doubles that compare equal would make it drop the wrong one.
    """
    log: list[str] = []
    dropped = _fake("economy", log)
    checkpoint = Checkpoint()
    checkpoint.track(dropped, DBName.ECONOMY)
    checkpoint.track(_fake("users", log), DBName.USERS)

    checkpoint.forget(dropped)
    await checkpoint()

    assert log == ["users"]
