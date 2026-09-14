"""Async repository for ``message_stats.message_counts``.

Originally read-only (Stage 9); A-03 adds the per-message write
(:meth:`MessageStatsRepo.increment`) so the new pipeline records group
activity itself now that the legacy bridge is gone. The ``/stats`` /
``/top`` / profile handlers build on top of the read queries.

Date arithmetic is performed entirely with ISO ``YYYY-MM-DD`` strings,
matching how legacy stores the column. Doing it in Python keeps the
SQL portable across the prod sqlite + the in-memory test DB without
needing ``date('now', '-N days')`` SQLite-specific syntax inside the
repo (the index still helps because it's ``BETWEEN``-friendly).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.message_stats import MessageCount

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement


@dataclass(frozen=True, slots=True)
class DailyCount:
    """One day's row for a user in a chat. Date is ``YYYY-MM-DD``."""

    date: str
    count: int


def _window_since(today: date, days: int) -> date:
    """Lower bound of an inclusive N-day window ending at ``today``.

    ``days=1`` is ``today`` only; ``days=7`` covers today + the previous
    six days. Raises ``ValueError`` for ``days < 1`` so a bogus caller
    can't silently flip the bound past ``today`` (which a negative
    ``timedelta`` would do).

    Pulled out of four callers (``count_for_days``,
    ``chat_totals_by_date``, ``top_users_by_messages``, ``last_n_days``)
    where the same two-line validate-and-compute block was repeated.
    Centralising it means the off-by-one semantics live in exactly one
    place — a future shift to "exclusive of today" or "anchored at the
    week boundary" lands as a one-method edit instead of four.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    return today - timedelta(days=days - 1)


def _date_in_window(today: date, days: int) -> ColumnElement[bool]:
    """Inclusive ``BETWEEN since AND today`` predicate on ``MessageCount.date``.

    Replaces the three-line ``since = _window_since(...); .where(date >=
    since.isoformat()); .where(date <= today.isoformat())`` block that
    appeared verbatim in ``chat_totals_by_date``, ``top_users_by_messages``
    and ``last_n_days`` — and which ``count_for_days`` was missing the
    upper half of until it moved here too. Centralising the predicate means a future tweak
    (e.g. switching the column to TEXT-with-timestamps, or treating
    ``today`` as exclusive) lands as a one-line edit instead of three, and
    the inclusive-on-both-ends invariant lives next to ``_window_since``
    rather than being implied by repetition.

    Hardcoded to ``MessageCount.date`` deliberately — making it generic
    over a column type drags ``InstrumentedAttribute`` into the signature
    and buys nothing because no other table in this repo has a date
    column with the same ISO-string shape.
    """
    since = _window_since(today, days)
    return MessageCount.date.between(since.isoformat(), today.isoformat())


class MessageStatsRepo:
    """``message_stats.message_counts`` access: one writer + the readers.

    The write half is :meth:`increment` below, called from
    ``middlewares/message_activity.py`` on every qualifying group
    message; everything after it reads. (This docstring used to say
    "read-only at Stage 9" — true when it was written, stale since the
    activity pipeline migrated.)
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def increment(
        self,
        user_id: int,
        chat_id: int,
        *,
        today: str,
        now: datetime,
    ) -> None:
        """Bump the ``(user_id, chat_id, today)`` daily counter by one.

        Mirrors legacy ``increment_message_count`` (bot.py:44543): an
        upsert that inserts a fresh row at ``count=1`` or atomically
        increments an existing day's ``count`` and refreshes
        ``last_message``. Race-safe via ``ON CONFLICT`` on the
        ``(user_id, chat_id, date)`` unique constraint, so two
        concurrent messages from the same user can't lose a tick to a
        read-modify-write gap.

        ``today`` is an ISO ``YYYY-MM-DD`` string and ``now`` an explicit
        timestamp — both passed in (not derived here) for the same
        TZ-correctness reason the read methods require ``today``: the
        caller owns what "today" means.
        """
        stmt = (
            sqlite_insert(MessageCount)
            .values(
                user_id=user_id,
                chat_id=chat_id,
                date=today,
                count=1,
                last_message=now,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "chat_id", "date"],
                set_={
                    "count": MessageCount.count + 1,
                    "last_message": now,
                },
            )
        )
        await self._session.execute(stmt)

    async def total_for_user(self, user_id: int, chat_id: int) -> int:
        """Sum of all daily counts for ``(user_id, chat_id)``. 0 if absent."""
        stmt = (
            select(func.coalesce(func.sum(MessageCount.count), 0))
            .where(MessageCount.user_id == user_id)
            .where(MessageCount.chat_id == chat_id)
        )
        result = await self._session.execute(stmt)
        # ``coalesce(SUM, 0)`` always returns an int — assert for mypy strict.
        value = result.scalar_one()
        return int(value)

    async def first_activity_date(self, user_id: int, chat_id: int) -> date | None:
        """Earliest recorded activity day for ``(user_id, chat_id)``.

        Backs the group-profile "in group since" line. The new pipeline
        only counts from when message tracking began, so this is the
        legacy "учёт с момента обновления" approximation — the first day
        we ever saw the user post in this chat. ``None`` when no rows.
        """
        stmt = (
            select(func.min(MessageCount.date))
            .where(MessageCount.user_id == user_id)
            .where(MessageCount.chat_id == chat_id)
        )
        raw = (await self._session.execute(stmt)).scalar_one_or_none()
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            return None

    async def count_since(
        self,
        user_id: int,
        chat_id: int,
        *,
        since: date,
    ) -> int:
        """Sum of counts on or after ``since`` (inclusive)."""
        stmt = (
            select(func.coalesce(func.sum(MessageCount.count), 0))
            .where(MessageCount.user_id == user_id)
            .where(MessageCount.chat_id == chat_id)
            .where(MessageCount.date >= since.isoformat())
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def count_for_days(self, user_id: int, chat_id: int, *, days: int, today: date) -> int:
        """Sum over the last ``days`` calendar days, inclusive of ``today``.

        ``days=1`` is just today. ``days=7`` covers today + previous 6
        days (matches legacy ``STATS_PERIOD_DAYS`` semantics).

        ``today`` is required (not defaulted) because the legacy code
        uses SQLite's ``date('now')``, which is always **UTC** — the
        ``localtime`` modifier legacy never passes is what would have made
        it follow the server's clock. So legacy's "today" is a UTC
        calendar day regardless of where the box runs, which is not the
        day the rest of the card means (``handlers/chatstats.py:32-38``).
        Forcing the caller to pass an explicit date eliminates the silent
        off-by-one at TZ boundaries and pushes the policy decision to the
        Stage 10 handler, which has access to ``Settings``.

        Bounded on both ends via :func:`_date_in_window`, not the
        open-ended :meth:`count_since`. The docstring above promises
        "inclusive of ``today``", i.e. ``days=1`` is *just* today — an
        open-ended ``date >= since`` would quietly also fold in rows
        stamped **after** today, which is reachable whenever a chat's
        counters were written under a ``STATS_TIMEZONE`` ahead of the
        one the caller now passes. That made the same window read
        differently here than in :meth:`last_n_days` / the chat-wide
        aggregates, which have always been bounded.
        """
        stmt = (
            select(func.coalesce(func.sum(MessageCount.count), 0))
            .where(MessageCount.user_id == user_id)
            .where(MessageCount.chat_id == chat_id)
            .where(_date_in_window(today, days))
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def chat_totals_by_date(
        self,
        chat_id: int,
        *,
        days: int,
        today: date,
    ) -> list[DailyCount]:
        """Per-day totals for the whole chat (all users summed), newest first.

        Powers the chat-wide ``/stats`` card. Missing days are omitted —
        the renderer can densify if it wants flat columns; legacy doesn't.

        ``today`` is required for the same TZ-correctness reason as
        :meth:`count_for_days`: caller decides what "today" means.
        """
        stmt = (
            select(
                MessageCount.date,
                func.coalesce(func.sum(MessageCount.count), 0).label("total"),
            )
            .where(MessageCount.chat_id == chat_id)
            .where(_date_in_window(today, days))
            .group_by(MessageCount.date)
            .order_by(MessageCount.date.desc())
        )
        result = await self._session.execute(stmt)
        return [DailyCount(date=row[0], count=int(row[1] or 0)) for row in result.all()]

    async def chat_total_for_days(self, chat_id: int, *, days: int, today: date) -> int:
        """Sum of ALL users' counts in ``chat_id`` over the last ``days`` days.

        ``days=1`` is today only; ``days=7`` covers today + the previous
        six days (matches the ``/chatstats`` "today" and "week" windows).
        Powers the activity totals on the ``/chatstats`` card, which the
        legacy command computed via ``SUM(count) WHERE date>=date('now',
        '-6 days')``. ``today`` is required for the same TZ-correctness
        reason as :meth:`count_for_days`.
        """
        stmt = (
            select(func.coalesce(func.sum(MessageCount.count), 0))
            .where(MessageCount.chat_id == chat_id)
            .where(_date_in_window(today, days))
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def active_user_count(self, chat_id: int, *, days: int, today: date) -> int:
        """Count of DISTINCT users with at least one message in the window.

        Mirrors legacy ``COUNT(DISTINCT user_id) ... WHERE date>=
        date('now','-6 days')`` for the ``/chatstats`` "active members"
        line. ``today`` is required for the same TZ-correctness reason as
        :meth:`count_for_days`.
        """
        stmt = (
            select(func.count(func.distinct(MessageCount.user_id)))
            .where(MessageCount.chat_id == chat_id)
            .where(_date_in_window(today, days))
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def newcomer_count(self, chat_id: int, *, days: int, today: date) -> int:
        """Users whose FIRST recorded day in ``chat_id`` falls in the window.

        Backs the "new this week" line on the ``/chatstats`` card.

        Legacy computed that number as ``get_chat_members_count() -
        _count_notified_users()`` (bot.py:41014) — it guessed newcomers
        from the gap between Telegram's live member count and the rows
        in ``users.db`` flagged ``notified=1`` (bot.py:38210). That flag
        table has no chat column at all, so the subtraction only means
        anything for one group; legacy knew it and rendered the segment
        exclusively when ``chat_id == CHAT_ID``, omitting it everywhere
        else. Even there it drifted permanently: a notified member who
        leaves is subtracted from the head count forever.

        Here the number is derived from data we actually own, per chat:
        a user is "new" when the earliest ``message_counts`` row we hold
        for them in this chat is inside the window. That is what lets
        the ``/chatstats`` card show the line in *every* group rather
        than just the main one.

        The trade-off is honest and worth stating: a long-time lurker
        who posts for the very first time this week counts as new, and
        anyone who joined before message tracking began never will. Both
        are the same "учёт с момента обновления" approximation
        :meth:`first_activity_date` already makes for the group profile,
        so the two lines can't contradict each other.

        Note the HAVING bound is deliberately two-sided while the
        grouped ``min(date)`` is computed over ALL history: filtering
        rows to the window *before* grouping would make every active
        user look new, since their earliest in-window row is trivially
        in the window.
        """
        first_seen = func.min(MessageCount.date)
        since = _window_since(today, days)
        grouped = (
            select(MessageCount.user_id)
            .where(MessageCount.chat_id == chat_id)
            .group_by(MessageCount.user_id)
            .having(first_seen.between(since.isoformat(), today.isoformat()))
            .subquery()
        )
        result = await self._session.execute(select(func.count()).select_from(grouped))
        return int(result.scalar_one())

    async def top_users_by_messages(
        self,
        chat_id: int,
        *,
        days: int,
        today: date,
        limit: int,
    ) -> list[tuple[int, int]]:
        """Top ``limit`` users in ``chat_id`` by summed message count.

        Returns ``(user_id, count)`` ordered ``total DESC, user_id ASC``.
        Users with zero rows in the window are absent (legacy's
        ``GROUP BY`` does the same — no row to sum).

        Cross-DB join to ``users.users`` for display names is not
        possible (separate engines), so the handler resolves names via
        :class:`UsersRepo` afterwards. Returning IDs + counts keeps the
        repo single-DB and the join logic in one named place.

        ``today`` is required for the same TZ-correctness reason as
        :meth:`count_for_days`.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        total = func.coalesce(func.sum(MessageCount.count), 0).label("total")
        stmt = (
            select(MessageCount.user_id, total)
            .where(MessageCount.chat_id == chat_id)
            .where(_date_in_window(today, days))
            .group_by(MessageCount.user_id)
            .order_by(total.desc(), MessageCount.user_id.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [(int(row[0]), int(row[1] or 0)) for row in result.all()]

    async def last_n_days(
        self,
        user_id: int,
        chat_id: int,
        *,
        days: int,
        today: date,
    ) -> list[DailyCount]:
        """Per-day breakdown for the last ``days`` days, newest first.

        Missing days are omitted (legacy renders them as "no data" rows
        in the UI; the caller can densify if needed). Output is ordered
        ``date DESC`` to match legacy's ``ORDER BY date DESC``.

        ``today`` is required for the same TZ-correctness reason as
        :meth:`count_for_days`.
        """
        stmt = (
            select(MessageCount.date, MessageCount.count)
            .where(MessageCount.user_id == user_id)
            .where(MessageCount.chat_id == chat_id)
            .where(_date_in_window(today, days))
            .order_by(MessageCount.date.desc())
        )
        result = await self._session.execute(stmt)
        return [DailyCount(date=row[0], count=int(row[1] or 0)) for row in result.all()]
