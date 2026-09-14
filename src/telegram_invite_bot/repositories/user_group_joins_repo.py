"""Async repository for the ``users.user_group_joins`` table.

Answers one question — *when did this user join this chat?* — which the
group profile card needs twice: for the "in this group since" date and
for the "messages since joining" counter.

The obvious-looking alternative is ``MessageStatsRepo.first_activity_date``
— a ``MIN(date)`` over ``message_stats.message_counts``, not a stored
join date; there is no such column, and message_stats.db has no table
but that one. It answers a different question anyway (the first day we
*counted a message* from them there). Using it as a join date makes the
since-join counter equal the lifetime total for every user, i.e. a
second copy of a line the card already prints. ``users.user_group_joins``
is the only honest source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.users import Marriage, Relationship, UserGroupJoin
from telegram_invite_bot.repositories._helpers import legacy_status_active

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class UserGroupJoinsRepo:
    """``users.user_group_joins`` access. One open session per request."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def joined_at(self, user_id: int, chat_id: int) -> datetime | None:
        """When ``user_id`` joined ``chat_id``, or ``None`` if unrecorded.

        Returns the row regardless of ``is_active``: a user who left and
        came back still joined when they joined, and the card that asks
        this is rendered *inside* the chat, so the reader is a member by
        construction. Legacy filtered on ``is_active`` here
        (``bot.py:44348``) and so blanked the date for anyone whose
        rejoin it hadn't observed — a stale flag silently erasing a fact
        that is not in doubt.

        ``None`` is a real, common answer: the table only knows about
        users seen since legacy started writing it, and about joins the
        new pipeline has observed. Callers must render the unknown case,
        never guess it.
        """
        stmt = (
            select(UserGroupJoin.joined_at)
            .where(UserGroupJoin.user_id == user_id)
            .where(UserGroupJoin.chat_id == chat_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record_join(
        self,
        user_id: int,
        chat_id: int,
        *,
        joined_at: datetime,
        source: str,
        group_title: str | None = None,
    ) -> None:
        """Record that ``user_id`` is in ``chat_id``, first sighting wins.

        On conflict the existing ``joined_at`` and ``source`` are kept
        deliberately: the column means "since when do we know this
        person is here", and a rejoin — or a second observation of the
        same membership — must not push that date forward and shrink
        every since-join counter derived from it. What *does* get
        refreshed is the liveness half: ``last_seen``, and clearing
        ``left_at`` / re-raising ``is_active`` so a previously departed
        member is no longer flagged as gone.

        ``group_title`` is refreshed too, but only when this call knows
        one — a caller that passes ``None`` is saying "I don't have the
        title", not "the group has no name", and must not blank a title
        an earlier sighting recorded.
        """
        insert = sqlite_insert(UserGroupJoin)
        stmt = insert.values(
            user_id=user_id,
            chat_id=chat_id,
            joined_at=joined_at,
            source=source,
            group_title=group_title,
            last_seen=joined_at,
            left_at=None,
            is_active=1,
        ).on_conflict_do_update(
            index_elements=["user_id", "chat_id"],
            set_={
                "last_seen": joined_at,
                "left_at": None,
                "is_active": 1,
                "group_title": func.coalesce(
                    insert.excluded.group_title, UserGroupJoin.group_title
                ),
            },
        )
        await self._session.execute(stmt)

    async def mark_left(self, user_id: int, chat_id: int, *, left_at: datetime) -> None:
        """Flag ``user_id`` as no longer present in ``chat_id`` (#244).

        Mirrors legacy verbatim — ``UPDATE user_group_joins SET
        is_active=0, left_at=? WHERE user_id=? AND chat_id=?``
        (bot.py:44187-44191). The row is *flagged*, never deleted, so
        ``joined_at`` survives the departure and a later rejoin heals the
        flag through :meth:`record_join` instead of pushing the date
        forward.

        Both predicates are load-bearing and neither may be dropped:
        ``user_id`` alone would declare the person gone from every group
        the bot serves, ``chat_id`` alone would empty the group.

        No row is inserted when none matches. A departure we never saw
        the arrival for says nothing about ``joined_at``, and inventing a
        date would poison the since-join counter this table exists to
        feed — the honest answer stays "unrecorded".
        """
        stmt = (
            update(UserGroupJoin)
            .where(UserGroupJoin.user_id == user_id)
            .where(UserGroupJoin.chat_id == chat_id)
            .values(left_at=left_at, is_active=0)
        )
        await self._session.execute(stmt)

    async def list_departed_chats(self) -> list[int]:
        """Chat ids that hold at least one departed row (#482).

        The sweeper that ends bonds after a long absence has no other
        way to learn which chats to look at: legacy ran its cleanup
        inline from five read paths and was therefore always handed a
        ``chat_id`` by the caller (``bot.py:21758`` and four others),
        while a background pass has no caller to be handed one by.
        Deriving the set from the departure flag itself keeps the sweep
        proportional to the work there actually is — a bot in a hundred
        quiet groups scans none of them.

        ``is_active = 0`` is the only predicate: a chat whose departed
        rows are all too recent still costs one cheap indexed scan in
        :meth:`list_departed_with_active_bonds`, which is far less than
        the alternative of carrying a second source of truth for "chats
        we have ever seen".
        """
        stmt = (
            select(UserGroupJoin.chat_id)
            .where(UserGroupJoin.is_active == 0)
            .distinct()
            .order_by(UserGroupJoin.chat_id)
        )
        return [int(chat_id) for chat_id in (await self._session.execute(stmt)).scalars()]

    async def list_departed_with_active_bonds(
        self, chat_id: int, threshold: datetime, *, limit: int
    ) -> list[int]:
        """Departed-and-still-bonded members of ``chat_id``, oldest first.

        Ports the SELECT half of legacy's ``_cleanup_left_users_bonds``
        (``bot.py:21589``), which read the same question out of the
        ``user_chat_left`` table this one replaced. Three deliberate
        differences from that query:

        * ``left_at IS NOT NULL`` is stated rather than assumed. Legacy's
          table only existed to record departures, so every row had one;
          here ``left_at`` is a nullable column on the *join* row, and a
          row can carry ``is_active = 0`` with no timestamp if some
          future writer ever flags one without stamping it. A NULL means
          "we do not know when", and a bond must never be dissolved on a
          date we do not have.
        * ``limit`` is mandatory. Each returned id costs the caller one
          live ``get_chat_member`` round-trip (the sweeper must confirm
          the person is really gone before ending anything), so an
          unbounded result is an unbounded burst of Telegram calls —
          the same unbounded-probe shape :meth:`UsersRepo.list_ranked`
          caps for the staff panel. Ordering by ``left_at`` makes the
          cap take the longest-departed first and stable across passes.
        * The bond filter is IN the query rather than applied to its
          result. #2013: this used to be two calls — this SELECT, then
          ``BondsWriteRepo.list_users_with_active_bonds`` over what came
          back — and that order is wrong in a way that fails silently.
          A settled departure is permanent (:meth:`clear_departure`
          fires only for a member the probe found *present*), so in any
          chat with more than ``limit`` lifetime departures the window
          fills with people who have nothing left to end, the narrowing
          returns nothing, and the sweeper skips the chat without so
          much as counting it as scanned. Bonds outlive their owner's
          departure forever and no log line says so. Narrowing first
          makes ``limit`` a bound on *work to do* rather than on rows to
          look at.

        Two EXISTS rather than a join: a member can hold a marriage and a
        relationship in the same chat, and a join would return them twice
        and spend two probes on one person.

        ``threshold`` is compared in the naive-LOCAL frame the column is
        written in — see :meth:`mark_left` and ``_record_leave``'s
        docstring. Passing a naive-UTC ``now`` here would age every
        departure by the host's UTC offset.

        ``status IS NULL`` counts as active on both bond tables
        (:func:`legacy_status_active`): prod rows predating the status
        column carry NULL, and reading those as finished would make the
        sweep skip exactly the oldest bonds.
        """
        bonded_here = or_(
            *(
                select(1)
                .where(
                    model.chat_id == chat_id,
                    or_(
                        model.user1_id == UserGroupJoin.user_id,
                        model.user2_id == UserGroupJoin.user_id,
                    ),
                    legacy_status_active(model.status),
                )
                .exists()
                for model in (Marriage, Relationship)
            )
        )
        stmt = (
            select(UserGroupJoin.user_id)
            .where(UserGroupJoin.chat_id == chat_id)
            .where(UserGroupJoin.is_active == 0)
            .where(UserGroupJoin.left_at.is_not(None))
            .where(UserGroupJoin.left_at <= threshold)
            .where(bonded_here)
            .order_by(UserGroupJoin.left_at)
            .limit(limit)
        )
        return [int(user_id) for user_id in (await self._session.execute(stmt)).scalars()]

    async def clear_departure(self, user_id: int, chat_id: int) -> None:
        """Undo a departure flag for someone who turned out to be present.

        The counterpart to :meth:`mark_left`, and the honest port of the
        one branch of legacy's cleanup that did NOT end anything: when
        its membership probe came back "still here", it deleted the
        ``user_chat_left`` row and moved on (``bot.py:21601``). Deleting
        is not available to us — the row legacy deleted held nothing but
        the departure, whereas ours also holds ``joined_at``, the date
        the group profile card prints. So the flag is cleared and the
        row kept, exactly as :meth:`record_join` heals it on a rejoin
        the bot actually observed.

        This is not the same call as :meth:`record_join`: that one needs
        a ``joined_at`` and a ``source`` because it may have to insert.
        Here there is a row by construction, and inventing a join date
        for it is the specific harm ``mark_left`` refuses to do.
        """
        stmt = (
            update(UserGroupJoin)
            .where(UserGroupJoin.user_id == user_id)
            .where(UserGroupJoin.chat_id == chat_id)
            .values(left_at=None, is_active=1)
        )
        await self._session.execute(stmt)
