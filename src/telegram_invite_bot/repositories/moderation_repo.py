"""Async repository for ``moderation.db`` — warnings + audit log.

T-020 scope:

* :class:`ModerationRepo` — per-request, session-scoped.  Methods:

  - ``add_warning`` — insert a warning row, log the action.
  - ``remove_last_warning`` — soft-delete the most-recent active warning.
  - ``remove_warning_by_id`` — soft-delete one specific warning row,
    scoped to (id, user_id, chat_id) so an operator cannot lift a
    warning that belongs to another user or another chat.
  - ``get_warning_count`` — count active, non-expired warnings.
  - ``list_warnings`` — fetch rows for /warnings display.
  - ``record_action`` — append to moderation_log (called by handlers
    that don't also touch warnings: ban, kick, mute, pin, fine, …).
  - ``recent_actions`` — read the last few log rows about one user in
    one chat, for the profile card's "recent actions" block.
  - ``recent_chat_actions`` / ``count_chat_actions`` — the whole-chat
    log tail and per-action totals behind the /groupadmin stats page.

No inline SQL anywhere; all writes go through SQLAlchemy ORM / core
expressions.  The repo trusts its arguments; validation lives in the
handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select, update

from telegram_invite_bot.db.models.moderation import ModerationLog, Warning

if TYPE_CHECKING:
    from sqlalchemy import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


def _not_expired(now: datetime) -> ColumnElement[bool]:
    """Filter clause: a warning is live if it has no expiry or expires
    after ``now``.

    We pass a Python-side naive-UTC ``now`` rather than the SQL
    ``func.now()`` for three reasons:

    * **Format parity.** ``expires`` is written from a SQLAlchemy
      ``datetime`` → SQLite stores ``'YYYY-MM-DD HH:MM:SS.ffffff'``.
      ``func.now()`` (``CURRENT_TIMESTAMP``) yields the second-precision
      ``'YYYY-MM-DD HH:MM:SS'``; binding a Python datetime serialises to
      the *same* microsecond format, so the comparison is exact rather
      than relying on lexicographic luck at the sub-second boundary.
    * **Portability.** On Postgres ``func.now()`` returns a tz-aware
      transaction timestamp while ``expires`` is naive — a latent
      mismatch. A bound naive-UTC value compares cleanly on either DB.
    * **Determinism.** The same ``now`` already drives the audit-log
      ``date`` in these methods, so a warning and its log row reference
      one consistent instant instead of two clock reads.
    """
    return (Warning.expires.is_(None)) | (Warning.expires > now)


@dataclass(frozen=True, slots=True)
class WarningRow:
    """Domain view of a single warning (returned by :meth:`list_warnings`)."""

    id: int
    reason: str
    admin_id: int
    date: datetime
    expires: datetime | None


@dataclass(frozen=True, slots=True)
class ActionRow:
    """Domain view of one audit-log entry (:meth:`recent_actions`).

    ``admin_id`` is deliberately absent: the only consumer is the user's
    own profile card, and naming the moderator there turns an audit trail
    into an invitation to go argue with them. The action, when, and why
    is the whole of what the subject needs.

    ``reason`` is ``str``, never ``None``, because :meth:`recent_actions`
    normalises it on the way out. The mapped column says ``NOT NULL`` but
    prod's ``moderation_log.reason`` is nullable (``prod_schemas.sql:738``)
    and legacy rows may carry ``NULL``, so the guarantee has to be made
    here rather than assumed from the schema.
    """

    action: str
    reason: str
    date: datetime


class ModerationRepo:
    """``moderation.db`` access.  Constructed per request with an open session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Warning writes
    # ------------------------------------------------------------------

    async def add_warning(
        self,
        *,
        user_id: int,
        chat_id: int,
        admin_id: int,
        reason: str,
        expires_days: int = 30,
    ) -> tuple[int, int]:
        """Insert a warning and log the action atomically.

        ``expires_days=0`` means "never expires" (``expires=NULL``).

        Returns ``(warning_id, active_count)`` where ``active_count``
        is the total of active, non-expired warnings for the (user,
        chat) pair *after* this row is inserted. The count is computed
        inside the same SQLAlchemy session (and therefore the same
        SQLite write transaction) so two concurrent ``add_warning``
        callers observe distinct, monotonically-increasing counts
        — eliminating the M-M-1 read-decide-write race in /warn where
        two admins could both observe count<threshold and both bump
        the user above the threshold.

        R15 (#1948): the three steps run inside a SAVEPOINT, so raising
        is all this method does. ``handlers.moderation.handle_warn``
        catches, replies ``h_mod_warn_fail`` and returns NORMALLY, and
        :class:`BaseSessionMiddleware` rolls back only on a RAISED
        exception — so without the savepoint whatever the failed call
        had already flushed was committed on the way out. Both halves
        of that were real: anything raising AFTER the flush (the count,
        or queueing the audit row) left the ``warnings`` INSERT pending
        and a Core statement failure does not deactivate the
        transaction, so the user really was warned, with no audit row,
        under an admin who had just been told it failed; and a failed
        ``flush`` DOES deactivate it, turning the middleware's later
        ``commit()`` into an unhandled ``PendingRollbackError`` for an
        update the handler had already reported cleanly. Same reasoning,
        same shape as :meth:`SupportTicketsRepo.create_open_ticket`.

        The savepoint spans the count as well as the two writes, so the
        M-M-1 guarantee above is unchanged: the row and the count that
        observes it stand or fall together.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        expires: datetime | None = None
        if expires_days > 0:
            expires = now + timedelta(days=expires_days)

        row = Warning(
            user_id=user_id,
            chat_id=chat_id,
            reason=reason,
            admin_id=admin_id,
            date=now,
            expires=expires,
            active=True,
        )
        async with self._session.begin_nested():
            self._session.add(row)
            await self._session.flush()  # get PK before logging

            # M-M-1: count active rows in the same transaction. SQLite WAL
            # serialises write transactions, so a concurrent ``add_warning``
            # will either see this row (and bump its own count) or be
            # serialised after; never simultaneously cross the threshold.
            count_stmt = select(func.count()).where(
                Warning.user_id == user_id,
                Warning.chat_id == chat_id,
                Warning.active.is_(True),
                _not_expired(now),
            )
            active_count = int((await self._session.execute(count_stmt)).scalar_one())

            await self._record_action_internal(
                action="warn",
                user_id=user_id,
                admin_id=admin_id,
                chat_id=chat_id,
                reason=reason,
                details=f"warning_id={row.id}",
                now=now,
            )

        return row.id, active_count

    async def remove_last_warning(
        self,
        *,
        user_id: int,
        chat_id: int,
        admin_id: int,
        reason: str = "",
    ) -> bool:
        """Soft-delete the most-recent active, non-expired warning.

        #1960: the UPDATE re-states the ``active`` predicate the SELECT
        already applied, and ``rowcount`` — not the SELECT's hit — is
        what the return value reports. The two statements do not share a
        snapshot: connections run in autocommit until a write promotes
        them (``db/engines.py``), so a concurrent caller commits between
        them and the blind ``WHERE id = ?`` used to deactivate an
        already-deactivated row and call it a success.

        That was a money bug one caller over. ``InventoryUseService``
        hands the shop's unwarn item to the handler UNCONSUMED, so two
        taps on two SEPARATE 800-coin items both clear the ``used = 0``
        pre-check, both read the same lone warning here, and both used
        to be told they lifted it — after which each consumed its own
        entry. Two items burned, one warning gone. #1133 made the
        consume the arbiter for the mirror case (two warnings, ONE
        item); it cannot arbitrate this one, because the two entries are
        different rows and neither consume loses.

        Returns ``True`` if a row was found and deactivated.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        stmt = (
            select(Warning.id)
            .where(
                Warning.user_id == user_id,
                Warning.chat_id == chat_id,
                Warning.active.is_(True),
                _not_expired(now),
            )
            .order_by(Warning.date.desc(), Warning.id.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        warning_id = result.scalar_one_or_none()
        if warning_id is None:
            return False

        deactivated = await self._session.execute(
            update(Warning)
            .where(Warning.id == warning_id, Warning.active.is_(True))
            .values(active=False)
        )
        if cast("CursorResult[Any]", deactivated).rowcount != 1:
            return False

        now = datetime.now(UTC).replace(tzinfo=None)
        await self._record_action_internal(
            action="unwarn",
            user_id=user_id,
            admin_id=admin_id,
            chat_id=chat_id,
            reason=reason,
            details=f"warning_id={warning_id}",
            now=now,
        )
        return True

    async def remove_warning_by_id(
        self,
        *,
        warning_id: int,
        user_id: int,
        chat_id: int,
        admin_id: int,
        reason: str = "",
    ) -> bool:
        """Soft-delete one specific active, non-expired warning row.

        #252(7): legacy ``remove_warning`` (bot.py:8571-8639) accepted a
        ``warning_id`` and scoped the UPDATE to
        ``WHERE id=? AND user_id=? AND chat_id=?`` (bot.py:8596) — that
        three-column scope is a security property, not an optimisation:
        without it an operator in chat A could lift a warning issued in
        chat B just by guessing its row id.  It is preserved verbatim.

        One legacy behaviour is deliberately NOT ported: legacy set
        ``removed_id = warning_id`` and returned ``True`` (bot.py:8599,
        :8635) without ever checking that the UPDATE matched a row, so a
        mistyped id reported success *and* wrote a bogus ``unwarn`` row
        into ``moderation_log``.  Here the row is looked up first and a
        miss returns ``False`` with nothing written.

        Returns ``True`` if the row was found and deactivated.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        stmt = select(Warning.id).where(
            Warning.id == warning_id,
            Warning.user_id == user_id,
            Warning.chat_id == chat_id,
            Warning.active.is_(True),
            _not_expired(now),
        )
        result = await self._session.execute(stmt)
        if result.scalar_one_or_none() is None:
            return False

        deactivated = await self._session.execute(
            update(Warning)
            .where(Warning.id == warning_id, Warning.active.is_(True))
            .values(active=False)
        )
        if cast("CursorResult[Any]", deactivated).rowcount != 1:
            # #1960: same guard as :meth:`remove_last_warning`. Two
            # admins racing ``/unwarn <id>`` on one row both used to be
            # told they lifted it, and both used to write an ``unwarn``
            # row into ``moderation_log`` — the audit-trail half of the
            # legacy defect this docstring says was NOT ported.
            return False

        await self._record_action_internal(
            action="unwarn",
            user_id=user_id,
            admin_id=admin_id,
            chat_id=chat_id,
            reason=reason,
            details=f"warning_id={warning_id}",
            now=datetime.now(UTC).replace(tzinfo=None),
        )
        return True

    async def clear_warnings(
        self,
        *,
        user_id: int,
        chat_id: int,
        admin_id: int,
        reason: str = "",
    ) -> int:
        """Soft-delete every active, non-expired warning for (user, chat).

        Returns how many rows were deactivated. Zero writes nothing at
        all — not even an audit row, because a sweep that swept nothing
        is not a moderator decision worth recording.

        #1780: /unban's counterpart. Lifting a ban without clearing the
        slate leaves the target sitting at ``max_warns``, and the automod
        escalation's re-ban branch reaches ``ban`` on ``count >=
        max_warns`` *without* adding a warning — so the next filtered
        message bans them again, permanently, with no grace and nothing
        in the DB saying why.

        One audit row for the whole sweep, not one per warning: three
        rows would read back as three separate moderator decisions.
        ``details`` carries the width instead. That is the one place this
        differs from looping :meth:`remove_last_warning`, which is also
        why it does not.

        The (user, chat) scope is the same security property spelled out
        in :meth:`remove_warning_by_id`: an operator in chat A must not
        be able to touch chat B's record.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        result = await self._session.execute(
            update(Warning)
            .where(
                Warning.user_id == user_id,
                Warning.chat_id == chat_id,
                Warning.active.is_(True),
                _not_expired(now),
            )
            .values(active=False)
        )
        cleared = cast("CursorResult[Any]", result).rowcount
        if cleared == 0:
            return 0

        await self._record_action_internal(
            action="unwarn",
            user_id=user_id,
            admin_id=admin_id,
            chat_id=chat_id,
            reason=reason,
            details=f"cleared={cleared}",
            now=now,
        )
        return cleared

    # ------------------------------------------------------------------
    # Warning reads
    # ------------------------------------------------------------------

    async def get_warning_count(self, *, user_id: int, chat_id: int) -> int:
        """Count active, non-expired warnings for (user, chat)."""
        now = datetime.now(UTC).replace(tzinfo=None)
        stmt = select(func.count()).where(
            Warning.user_id == user_id,
            Warning.chat_id == chat_id,
            Warning.active.is_(True),
            _not_expired(now),
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def count_active_warnings_in_chat(self, *, chat_id: int) -> int:
        """RR-4 #44: total active, non-expired warnings across ALL users in
        a chat — the at-a-glance moderation-load figure legacy showed in the
        /groupadmin header."""
        now = datetime.now(UTC).replace(tzinfo=None)
        stmt = select(func.count()).where(
            Warning.chat_id == chat_id,
            Warning.active.is_(True),
            _not_expired(now),
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def list_warnings(
        self,
        *,
        user_id: int,
        chat_id: int,
        limit: int = 20,
    ) -> list[WarningRow]:
        """Fetch active, non-expired warnings, most-recent first."""
        now = datetime.now(UTC).replace(tzinfo=None)
        stmt = (
            select(Warning)
            .where(
                Warning.user_id == user_id,
                Warning.chat_id == chat_id,
                Warning.active.is_(True),
                _not_expired(now),
            )
            .order_by(Warning.date.desc(), Warning.id.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [
            WarningRow(
                id=row.id,
                reason=row.reason,
                admin_id=row.admin_id,
                date=row.date,
                expires=row.expires,
            )
            for row in result.scalars()
        ]

    # ------------------------------------------------------------------
    # Audit log (all actions, including ban / kick / mute / pin / fine)
    # ------------------------------------------------------------------

    async def recent_actions(
        self,
        *,
        user_id: int,
        chat_id: int,
        limit: int = 3,
        days: int = 90,
        now: datetime | None = None,
    ) -> list[ActionRow]:
        """Recent audit entries about ``user_id`` in ``chat_id``.

        Mirrors legacy ``get_moderation_logs(limit=3, user_id=…,
        chat_id=…, days=90)`` (bot.py:39783), the read behind the
        profile card's "recent actions" block: newest first, capped both
        by count and by age so a punishment from last year stops
        following the user around.

        Scoped to one ``(user, chat)`` pair, so target-less entries
        (``/pin`` and ``/unpin`` write ``user_id = NULL``) can never
        appear here — an equality test never matches NULL in SQL, which
        is the behaviour we want and not an accident worth relying on
        silently.

        ``now`` is injectable for the same reason as elsewhere in this
        module: a test that seeds a row "88 days ago" must agree with the
        cutoff the query computes, and a second clock read is a second
        answer.
        """
        cutoff = (now or datetime.now(UTC).replace(tzinfo=None)) - timedelta(days=days)
        stmt = (
            select(ModerationLog)
            .where(
                ModerationLog.user_id == user_id,
                ModerationLog.chat_id == chat_id,
                ModerationLog.date >= cutoff,
            )
            .order_by(ModerationLog.date.desc(), ModerationLog.id.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [
            ActionRow(action=row.action, reason=row.reason or "", date=row.date)
            for row in result.scalars()
        ]

    async def recent_chat_actions(self, *, chat_id: int, limit: int = 5) -> list[ActionRow]:
        """The last few audit entries for a WHOLE chat, newest first.

        RR-4 #37: the /groupadmin stats page's moderation log. Unlike
        :meth:`recent_actions` this is not scoped to a user and has no
        age cutoff — the audience is the group's own admin looking at
        their group's recent moderation, not a member reading their own
        record, so a quiet group showing an old entry is informative
        rather than a punishment that follows someone around.

        Target-less rows (``/pin``, ``/unpin`` — ``user_id IS NULL``) are
        included: they are real moderation actions in this chat.

        Served by ``idx_modlog_chat_date`` — which prod did not have
        until ``moderation/0011_chat_scoped_indexes`` (#1949): the
        models declared it, no revision created it, and legacy's own
        DDL indexes ``moderation_log`` by user, admin and date only.
        """
        stmt = (
            select(ModerationLog)
            .where(ModerationLog.chat_id == chat_id)
            .order_by(ModerationLog.date.desc(), ModerationLog.id.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [
            ActionRow(action=row.action, reason=row.reason or "", date=row.date)
            for row in result.scalars()
        ]

    async def count_chat_actions(self, *, chat_id: int) -> dict[str, int]:
        """``{action: count}`` over the whole audit log for one chat.

        RR-4 #37: the aggregate counters on the stats page. Legacy read
        dedicated ``mutes``/``bans`` tables (bot.py:31042-31050); the port
        has no such tables — the append-only ``moderation_log`` IS the
        record — so the counts are grouped out of it in one pass instead
        of one COUNT query per action.

        Actions absent from the log are absent from the mapping; the
        renderer supplies the zero. Nothing is filtered out here — which
        action types get a counter line is the renderer's editorial
        call, so adding one there needs no change in this repo.
        """
        stmt = (
            select(ModerationLog.action, func.count())
            .where(ModerationLog.chat_id == chat_id)
            .group_by(ModerationLog.action)
        )
        result = await self._session.execute(stmt)
        return {action: int(count) for action, count in result.all()}

    async def record_action(
        self,
        *,
        action: str,
        user_id: int | None,
        admin_id: int,
        chat_id: int,
        reason: str = "",
        details: str | None = None,
    ) -> None:
        """Append one audit row.  Called directly by handlers for non-warning actions.

        M-M-2: ``user_id`` is ``None`` for target-less actions (/pin and
        /unpin act on messages, not users). The previous ``user_id=0``
        sentinel conflated three different cases — NULL is the honest
        representation.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        await self._record_action_internal(
            action=action,
            user_id=user_id,
            admin_id=admin_id,
            chat_id=chat_id,
            reason=reason,
            details=details,
            now=now,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _record_action_internal(
        self,
        *,
        action: str,
        user_id: int | None,
        admin_id: int,
        chat_id: int,
        reason: str,
        details: str | None,
        now: datetime,
    ) -> None:
        self._session.add(
            ModerationLog(
                action=action,
                user_id=user_id,
                admin_id=admin_id,
                chat_id=chat_id,
                reason=reason,
                details=details,
                date=now,
            )
        )
