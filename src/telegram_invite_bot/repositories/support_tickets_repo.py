"""Async repository for ``users.support_tickets`` (extended in T-022).

Stage 14 surface: ``create_open_ticket`` (minimal write path for
``/feedback``). T-022 extends the repo with read paths needed by the
full ticket lifecycle:

* :meth:`list_by_user` — user-facing "my tickets" command.
* :meth:`get_by_id` — admin inspection before replying.
* :meth:`list_open` — admin dashboard listing.
* :meth:`add_admin_reply` — admin reply; sets answer/answered_at/status.
* :meth:`mark_closed` — close a ticket.

``add_admin_reply`` returns a :class:`ReplyOutcome` rather than a bare
``SupportTicket | None`` so the handler can tell "no such ticket" from
"this ticket is not open any more" — legacy refused the second reply but
told the admin it had been sent (bot.py:35600-35604 returns ``False``,
and ``process_admin_answer`` at bot.py:35844 discards that result).

All new methods use SQLAlchemy 2.0-style ``select(Model).where(...)``
rather than raw SQL. Returning ``SupportTicket`` entities (not bare ids)
lets callers read ``user_id``, ``answer``, etc. without an extra fetch.

Session ownership note: one ``AsyncSession`` per request, managed by the
caller (usually :class:`SessionMiddleware`). All methods flush but do
NOT commit — the middleware commits on successful handler return and
rolls back on exception, which keeps the "half-saved row cannot leak"
guarantee from Stage 14.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, update

from telegram_invite_bot.db.models.support import SupportTicket

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


class ReplyOutcome(StrEnum):
    """Mutually-exclusive results of :meth:`SupportTicketsRepo.add_admin_reply`.

    StrEnum so log lines render the name as plain text — same posture as
    ``ClaimOutcome`` in :mod:`telegram_invite_bot.services.check_service`.
    """

    OK = "ok"
    NOT_FOUND = "not_found"
    NOT_OPEN = "not_open"


class SupportTicketsRepo:
    """``users.support_tickets`` reader/writer. One session per request."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_open_ticket(
        self,
        *,
        user_id: int,
        username: str | None,
        first_name: str | None,
        text: str,
        now: datetime | None = None,
    ) -> int:
        """Insert a new ``open`` ticket; return its autoincrement id.

        Legacy :func:`execute_query` swallowed errors and returned
        ``None`` on failure — its own docstring promises «Результат
        запроса или None при ошибке» (#1657: the address here used to be
        ``bot/handlers/support.py:47``, a directory that has never
        existed in this repository; the claim was right, only its anchor
        was invented). We raise instead —
        the session middleware rolls back on raise, so the handler
        can map the exception to user-facing "couldn't save" copy
        without leaving a half-committed row behind.

        R15: the insert runs inside a SAVEPOINT so that raising is all
        it does. All three ``/feedback`` and ``/support`` call sites
        catch this exception, reply "couldn't save" and return — which
        only works if the session is still usable afterwards. Without
        the savepoint a failed flush deactivates the whole per-update
        transaction, so the middleware's later ``commit()`` raises
        ``PendingRollbackError`` and takes unrelated users-DB writes
        from the same update (the ``last_seen`` touch, a nickname, an
        AI-quota increment) down with it.
        """
        ticket = SupportTicket(
            user_id=user_id,
            username=(username or None),
            first_name=(first_name or None),
            text=text,
            status="open",
            created_at=now or datetime.now(UTC).replace(tzinfo=None),
        )
        async with self._session.begin_nested():
            self._session.add(ticket)
            await self._session.flush()
        # ``id`` is autoincrement; populated by flush.
        return ticket.id

    async def list_by_user(
        self,
        user_id: int,
        *,
        limit: int = 10,
    ) -> list[SupportTicket]:
        """Return the most-recent ``limit`` tickets for ``user_id``.

        Ordered newest-first (id DESC). The handler truncates the
        displayed text to 80 chars for readability — the full text is
        stored verbatim here and the truncation is UI-layer only.
        """
        stmt = (
            select(SupportTicket)
            .where(SupportTicket.user_id == user_id)
            .order_by(SupportTicket.id.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, ticket_id: int) -> SupportTicket | None:
        """Fetch one ticket by primary key; ``None`` if it does not exist.

        Used by admin commands (``/ticket_reply``, ``/ticket_close``)
        before they write back to the row so they can surface a typed
        "not found" error rather than a raw SQL exception.
        """
        stmt = select(SupportTicket).where(SupportTicket.id == ticket_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_open(self, *, limit: int = 20) -> list[SupportTicket]:
        """Return the most-recent ``limit`` tickets whose status is ``open``.

        Admin-facing view. Ordered newest-first so the admin sees the
        freshest un-answered item at the top, matching the ordering users
        expect from a "pending queue".
        """
        stmt = (
            select(SupportTicket)
            .where(SupportTicket.status == "open")
            .order_by(SupportTicket.id.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def add_admin_reply(
        self,
        ticket_id: int,
        *,
        admin_id: int,
        answer: str,
        now: datetime | None = None,
    ) -> tuple[ReplyOutcome, SupportTicket | None]:
        """Record an admin reply on ``ticket_id`` if it is still open.

        Sets ``answer``, ``answered_by``, ``answered_at``, and flips
        ``status`` to ``"answered"``. Returns the updated row alongside
        :attr:`ReplyOutcome.OK` so the handler can read ``user_id`` and
        forward the reply as a DM without a second round-trip.

        ONLY an ``open`` ticket may be answered — legacy's
        ``answer_ticket`` opens with ``if not ticket or ticket.status !=
        "open": return False`` (bot.py:35600-35604). Without that guard
        a second ``/ticket_reply`` on the same id overwrites the first
        answer, its author and its timestamp with no way back: the row
        holds exactly one ``answer`` column, and nothing archives the
        previous value. The same applies to a ticket the user already
        closed.

        DIVERGENCE, deliberate: legacy DISCARDED the ``False``
        (bot.py:35844 calls ``answer_ticket`` for its side effect only),
        so it silently skipped the write, DM-ed the user anyway and told
        the admin "✅ Ответ отправлен". We report the refusal instead —
        claiming a reply was saved when it was not is the worse of the
        two behaviours, and the stored answer is what the user sees in
        ``/my_tickets``.

        ``(NOT_FOUND, None)`` when no such row exists; ``(NOT_OPEN,
        ticket)`` when it exists but has already been answered or
        closed — the row is returned unmodified so the caller can name
        its current status.

        #1941: the guard above is a CLAIM, and it is spelled as one.
        It used to be a ``SELECT`` followed by an unconditional write,
        and a ``select`` never promotes the transaction — see
        ``db/engines.py``'s ``_promote_to_write_txn``, whose
        ``_NON_WRITE_HEADS`` deliberately leaves reads in autocommit.
        So the decision "this ticket is still open" was taken with no
        writer lock held, and two admins answering the same id both
        passed it: the second reply then overwrote the first answer,
        its author and its timestamp, while BOTH admins were told OK
        and BOTH DMs reached the user — with ``/my_tickets`` showing
        only the survivor. Exactly the loss the paragraph above
        declares unacceptable, and it is not hypothetical merely
        because the command is developer-gated.

        The conditional ``UPDATE`` is the house idiom for this
        (``WithdrawalsRepo.claim_terminal`` #776,
        ``P2pRepo``): it has a write head, so SQLite takes the writer
        lock, evaluates ``status = 'open'`` under it, and reports
        through ``rowcount`` which caller actually won. The loser falls
        through to the same read the old code opened with, which is
        what still separates NOT_FOUND from NOT_OPEN and, in the raced
        case, hands back the row the WINNER wrote — so the refusal
        names the real current status rather than the stale one.

        One consequence for callers: a write head promotes the
        transaction, so this method leaves the session holding the
        users.db writer lock on EVERY path out — a refusal included,
        where the statement matched nothing. ``handle_ticket_reply``
        therefore checkpoints before it answers the admin instead of
        keeping that lock across a Telegram round trip.
        """
        ts = (now or datetime.now(UTC)).replace(tzinfo=None)
        result = await self._session.execute(
            update(SupportTicket)
            .where(SupportTicket.id == ticket_id, SupportTicket.status == "open")
            .values(answer=answer, answered_by=admin_id, answered_at=ts, status="answered")
        )
        won = cast("CursorResult[Any]", result).rowcount == 1
        # Read back in both branches. On a win it is the row the caller
        # needs for the DM; on a loss it is what tells NOT_FOUND from
        # NOT_OPEN. The ORM-enabled UPDATE above already synchronised
        # the session, so this sees the values just written rather than
        # a stale identity-map copy.
        ticket = await self.get_by_id(ticket_id)
        if ticket is None:
            # Only reachable on a loss: a win updated the row, and
            # nothing deletes tickets.
            return (ReplyOutcome.NOT_FOUND, None)
        if not won:
            return (ReplyOutcome.NOT_OPEN, ticket)
        await self._session.flush()
        return (ReplyOutcome.OK, ticket)

    async def mark_closed(
        self,
        ticket_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Set ``status='closed'`` and stamp ``closed_at`` on ``ticket_id``.

        Idempotent: calling on an already-closed ticket sets the fields
        again without error and still returns ``True``. Returns ``False``
        only when the ticket does not exist.
        """
        ticket = await self.get_by_id(ticket_id)
        if ticket is None:
            return False
        ts = (now or datetime.now(UTC)).replace(tzinfo=None)
        ticket.status = "closed"
        ticket.closed_at = ts
        await self._session.flush()
        return True
