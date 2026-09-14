"""#1941: answering a support ticket must be a claim, not a check-then-act.

:meth:`SupportTicketsRepo.add_admin_reply` opened with a ``SELECT``,
compared ``status`` in Python, and then wrote unconditionally. A
``select`` never promotes the transaction — ``db/engines.py``'s
``_promote_to_write_txn`` leaves reads in autocommit on purpose — so the
decision "this ticket is still open" was made with no writer lock held.

Two admins answering the same id therefore both passed the check. The
second write then overwrote the first answer, its author and its
timestamp; the row holds exactly ONE ``answer`` column and nothing
archives the previous value. Both admins were told OK, both DMs reached
the user, and ``/my_tickets`` showed only the survivor. The method's own
docstring named that loss as the reason the guard exists.

Two things about the setup are deliberate.

The engines come from the real :func:`build_registry`, not from a
hand-rolled ``create_async_engine``: the defect lives in the seam
between the safety listener and ``_promote_to_write_txn``, and a test
that attaches only the first would be arguing about a stack that does
not exist in production.

The interleaving is forced rather than raced. Two coroutines and
``asyncio.gather`` reproduce the overwrite only about eight times in
twenty — the losing SELECT has to land inside the winner's window by
luck — which is a test that passes on the broken code more often than
not. :class:`_WriteGate` instead holds the LOSER at the moment it first
tries to write, until the winner has committed. That is the race stated
as an ordering rather than waited for.

The gate sits on the session, in front of ``execute`` of an ``UPDATE``
and in front of ``flush`` — the two doors either implementation can
leave through, so neither is favoured: whatever reads the loser wanted
it has already taken, and its write reaches the file after the
winner's is durable. Deliberately NOT a ``before_cursor_execute``
listener: SQLAlchemy runs those in registration order, and
``_promote_to_write_txn`` is already registered, so a hook there pauses
the loser with ``BEGIN IMMEDIATE`` taken and deadlocks the winner
behind the file lock instead of racing it. On the session the pause is
a plain ``await``, before anything reaches the driver.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.sql.dml import Update

from telegram_invite_bot.config.settings import AppEnv, Settings
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.support import SupportTicket
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.support_tickets_repo import (
    ReplyOutcome,
    SupportTicketsRepo,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 9, 12, 0, 0)
_WINNER = 1001
_LOSER = 2002
# Generous: it bounds a hang, it does not pace the test. Every wait
# below is released by the other side within microseconds.
_GATE_TIMEOUT = 10.0


class _WriteGate:
    """Holds a session at its first write until the winner releases it.

    ``execute`` of an ``UPDATE`` and ``flush`` are the two doors an
    implementation can leave through — the claim uses the first, the
    old check-then-act used the second — so both are watched and the
    gate closes on whichever comes first. One-shot: once the loser has
    been let through, its ``commit`` runs unhindered.
    """

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.released = asyncio.Event()
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    async def hold(self) -> None:
        if not self._armed:
            return
        self._armed = False
        self.reached.set()
        await asyncio.wait_for(self.released.wait(), timeout=_GATE_TIMEOUT)


class _HeldSession:
    """``AsyncSession`` proxy that pauses at the gate before writing.

    Everything else is the real session, reached through ``__getattr__``
    — the repository is handed this object and cannot tell, which is
    the point: the interleaving is imposed from outside the code under
    test, not built into it.
    """

    def __init__(self, session: AsyncSession, gate: _WriteGate) -> None:
        self._session = session
        self._gate = gate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(statement, Update):
            await self._gate.hold()
        return await self._session.execute(statement, *args, **kwargs)

    async def flush(self, *args: Any, **kwargs: Any) -> None:
        await self._gate.hold()
        await self._session.flush(*args, **kwargs)


@pytest.fixture
async def two_admins(
    make_settings: Callable[..., Settings],
) -> AsyncIterator[tuple[AsyncSession, AsyncSession, _WriteGate]]:
    """Two production registries over ONE users.db, plus the loser's gate.

    Two registries rather than two sessions on one: the defect is about
    a decision taken outside a writer lock, and a single engine would
    share both the connection pool and the identity map, which would
    hide it.
    """
    settings = make_settings(AppEnv.PROD)
    registries = [build_registry(settings), build_registry(settings)]
    gate = _WriteGate()
    try:
        async with registries[0].engine(DBName.USERS).begin() as conn:
            await conn.run_sync(UsersBase.metadata.create_all)
        async with (
            registries[0].session(DBName.USERS)() as winner,
            registries[1].session(DBName.USERS)() as loser,
        ):
            yield winner, cast("AsyncSession", _HeldSession(loser, gate)), gate
    finally:
        for registry in registries:
            await registry.dispose()


async def _open_ticket(session: AsyncSession) -> int:
    ticket_id = await SupportTicketsRepo(session).create_open_ticket(
        user_id=42, username="alice", first_name="Alice", text="сломался /balance", now=_NOW
    )
    await session.commit()
    return ticket_id


async def _reply(
    session: AsyncSession, ticket_id: int, admin_id: int, answer: str
) -> tuple[ReplyOutcome, SupportTicket | None]:
    """One admin's whole ``/ticket_reply``, committed as the middleware would."""
    outcome, row = await SupportTicketsRepo(session).add_admin_reply(
        ticket_id, admin_id=admin_id, answer=answer, now=_NOW
    )
    await session.commit()
    return outcome, row


async def _race(
    two_admins: tuple[AsyncSession, AsyncSession, _WriteGate], ticket_id: int
) -> tuple[tuple[ReplyOutcome, SupportTicket | None], tuple[ReplyOutcome, SupportTicket | None]]:
    """Run both replies with the loser's write ordered after the winner's."""
    winner_session, loser_session, gate = two_admins
    gate.arm()

    losing = asyncio.create_task(_reply(loser_session, ticket_id, _LOSER, "перезагрузите бота"))
    # The loser now sits one statement short of writing, holding no
    # lock: everything it has done so far was a read.
    await asyncio.wait_for(gate.reached.wait(), timeout=_GATE_TIMEOUT)

    winner = await _reply(winner_session, ticket_id, _WINNER, "починили")
    gate.released.set()
    return winner, await losing


async def _stored(session: AsyncSession, ticket_id: int) -> SupportTicket:
    """The row as the database actually holds it, never a cached copy."""
    stmt = (
        select(SupportTicket)
        .where(SupportTicket.id == ticket_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


async def test_the_loser_of_a_simultaneous_reply_is_refused(
    two_admins: tuple[AsyncSession, AsyncSession, _WriteGate],
) -> None:
    """The regression. Both admins decide while the ticket is open."""
    winner_session, _, _ = two_admins
    ticket_id = await _open_ticket(winner_session)

    (outcome_a, _), (outcome_b, row_b) = await _race(two_admins, ticket_id)

    assert outcome_a is ReplyOutcome.OK
    assert outcome_b is ReplyOutcome.NOT_OPEN
    assert row_b is not None
    stored = await _stored(winner_session, ticket_id)
    assert stored.answer == "починили"
    assert stored.answered_by == _WINNER


async def test_the_refusal_names_the_status_the_winner_wrote(
    two_admins: tuple[AsyncSession, AsyncSession, _WriteGate],
) -> None:
    """The loser's row must be the current one, not their stale copy.

    The handler renders ``ticket.status`` straight into the refusal, so
    handing back the read that lost the race would tell the admin the
    ticket is still open — right after refusing them for not being.
    """
    winner_session, _, _ = two_admins
    ticket_id = await _open_ticket(winner_session)

    _, (_, row_b) = await _race(two_admins, ticket_id)

    assert row_b is not None
    assert row_b.status == "answered"
    assert row_b.answered_by == _WINNER


async def test_a_lone_reply_still_succeeds(
    two_admins: tuple[AsyncSession, AsyncSession, _WriteGate],
) -> None:
    """The control: the uncontended path is untouched."""
    winner_session, _, _ = two_admins
    ticket_id = await _open_ticket(winner_session)

    outcome, row = await _reply(winner_session, ticket_id, _WINNER, "починили")

    assert outcome is ReplyOutcome.OK
    assert row is not None
    assert row.status == "answered"
    assert row.answer == "починили"
    assert row.answered_by == _WINNER
    assert row.answered_at == _NOW
    stored = await _stored(winner_session, ticket_id)
    assert stored.answered_at == _NOW


async def test_a_missing_ticket_is_still_not_found(
    two_admins: tuple[AsyncSession, AsyncSession, _WriteGate],
) -> None:
    """The claim must not turn "no such row" into "already answered"."""
    winner_session, _, _ = two_admins
    await _open_ticket(winner_session)

    outcome, row = await _reply(winner_session, 9999, _WINNER, "починили")

    assert outcome is ReplyOutcome.NOT_FOUND
    assert row is None


async def test_a_closed_ticket_is_refused_untouched(
    two_admins: tuple[AsyncSession, AsyncSession, _WriteGate],
) -> None:
    """The other status the guard exists for, uncontended."""
    winner_session, _, _ = two_admins
    ticket_id = await _open_ticket(winner_session)
    await SupportTicketsRepo(winner_session).mark_closed(ticket_id, now=_NOW)
    await winner_session.commit()

    outcome, row = await _reply(winner_session, ticket_id, _WINNER, "починили")

    assert outcome is ReplyOutcome.NOT_OPEN
    assert row is not None
    assert row.status == "closed"
    assert row.answer is None
