"""Async repository for the ``users.users`` table.

Single point of SQL contact. Returns domain entities, never raw
SQLAlchemy rows, so callers can't accidentally rely on session state
after the session is closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.core.entities.user import User as UserEntity
from telegram_invite_bot.db.models.users import User as UserRow
from telegram_invite_bot.repositories._helpers import (
    reload_after_upsert,
    row_exists,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class RankedUser:
    """One holder of a global bot rank, as the staff roster needs them.

    ``first_name`` is whatever Telegram last told us (it can be ``None``
    for a user who was ranked by id before ever messaging the bot) — the
    renderer falls back to the id, never to an empty label.
    """

    user_id: int
    rank: int
    first_name: str | None


def _to_entity(row: UserRow, *, is_new: bool) -> UserEntity:
    return UserEntity(
        user_id=row.user_id,
        username=row.username,
        first_name=row.first_name,
        last_name=row.last_name,
        language_code=row.language_code,
        is_premium=bool(row.is_premium),
        joined_date=row.joined_date,
        last_seen=row.last_seen,
        last_active=row.last_active,
        is_new=is_new,
    )


class UsersRepo:
    """``users.users`` access. Constructed per request with an open session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> UserEntity | None:
        row = await self._session.get(UserRow, user_id)
        return _to_entity(row, is_new=False) if row is not None else None

    async def get_by_username(self, username: str) -> UserEntity | None:
        """Case-insensitive lookup by ``users.username``.

        Mirrors legacy ``bot.py:19048`` (``WHERE username IS NOT NULL AND
        lower(username) = ?``) and the ad-hoc ``@username`` resolution
        sprinkled across the legacy /send, /transfer_rights and check-
        creation flows. Telegram-side usernames are case-preserving but
        case-insensitive at the protocol level, and legacy stores
        whatever case Telegram delivered — comparing lowered both sides
        is the only correct match.

        A leading ``@`` is stripped defensively: the parse layer above
        already does it, but exposing the looser contract here keeps
        future callers (e.g. admin tooling that may pass raw input)
        from having to remember. An empty string short-circuits to
        ``None`` without touching the DB — the lower-cased ``LIKE``
        could otherwise return any user with ``username = ''`` if
        such a row existed.

        #702: ``ORDER BY last_seen DESC`` is part of the mirrored query,
        not decoration. Telegram usernames are transferable, so two rows
        can carry the same one — the account that gave it up and the
        account that took it. Both legacy sites sort (``bot.py:19049``,
        ``bot.py:41592``) and pick the more recently seen; a bare
        ``LIMIT 1`` lets SQLite hand back whichever row it reaches
        first, which for ``/send``, ``/give`` and check creation means
        money can land on the abandoned account. NULL ``last_seen``
        sorts last under DESC in SQLite, so a never-seen row only wins
        when it is the only candidate — again matching legacy.
        """
        cleaned = username.strip().lstrip("@")
        if not cleaned:
            return None
        stmt = (
            select(UserRow)
            .where(UserRow.username.is_not(None))
            .where(func.lower(UserRow.username) == cleaned.lower())
            .order_by(UserRow.last_seen.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_entity(row, is_new=False) if row is not None else None

    async def first_names_by_ids(self, user_ids: list[int]) -> dict[int, str]:
        """Bulk lookup of ``first_name`` for a set of user IDs.

        Used by leaderboards that collect IDs from a different DB (e.g.
        ``message_stats.message_counts``) and need names without a per-
        row round-trip. Missing IDs are simply absent from the result —
        the handler decides the fallback copy. An empty input list
        short-circuits without hitting the DB.
        """
        if not user_ids:
            return {}
        stmt = select(UserRow.user_id, UserRow.first_name).where(UserRow.user_id.in_(user_ids))
        result = await self._session.execute(stmt)
        return {int(row[0]): (row[1] or "") for row in result.all()}

    async def all_user_ids(self) -> list[int]:
        """Every known ``user_id`` — the /broadcast fan-out audience.

        Mirrors legacy ``SELECT user_id FROM users`` (bot.py:26002):
        the broadcast goes to every row ever upserted, with no
        activity / blocked-state filtering — delivery failures are
        counted (not pre-filtered) by the send loop, same as legacy's
        per-user ``except → failed += 1``. Materialised as a plain
        ``list`` (not an async generator) on purpose: the caller snapshots
        the audience while its request-scoped session is open, then sends
        from a background task AFTER the session closes.
        """
        result = await self._session.execute(select(UserRow.user_id))
        return [int(uid) for uid in result.scalars()]

    async def get_rank(self, user_id: int) -> int:
        """Global rank for ``user_id`` (ranks epic R1).

        Mirrors legacy ``get_user_rank``'s DB read (bot.py:6637-6646):
        a missing row OR a NULL ``rank`` column both mean rank 0.
        Deliberately read-only — legacy's incidental "insert a stub row
        on miss" is NOT ported (row creation belongs to
        ``upsert_from_telegram``; a read must stay side-effect-free).

        Developer elevation, caching and every policy decision live in
        :class:`~telegram_invite_bot.services.rank_service.RankService`
        — this repo stays dumb.
        """
        rank = (
            await self._session.execute(select(UserRow.rank).where(UserRow.user_id == user_id))
        ).scalar_one_or_none()
        return int(rank) if rank is not None else 0

    async def ranks_for(self, user_ids: Sequence[int]) -> dict[int, int]:
        """Global ranks for a known set of users, in one round trip.

        The /groupadmin staff roster needs the rank of every Telegram
        admin of a group. Legacy solved the same problem with one
        ``user_ranks[uid]`` dict lookup per person because the whole
        table lived in process memory (bot.py:31108); we read from the
        DB instead, so the loop has to become a single ``IN`` query —
        otherwise a chat with 20 admins costs 20 round trips per panel
        refresh.

        Users with no row (or a NULL rank) are simply absent from the
        result; callers use ``.get(uid, 0)``, matching
        :meth:`get_rank`'s "missing means 0" contract.
        """
        if not user_ids:
            return {}
        result = await self._session.execute(
            select(UserRow.user_id, UserRow.rank).where(
                UserRow.user_id.in_(list(user_ids)), UserRow.rank.is_not(None)
            )
        )
        return {int(uid): int(rank) for uid, rank in result.all()}

    async def list_ranked(self, *, min_rank: int = 1, limit: int) -> list[RankedUser]:
        """Users holding a global rank, strongest first, capped at ``limit``.

        Ranks in this bot are GLOBAL — there is no per-group rank column
        — so this is "everyone who holds bot power anywhere", not "staff
        of group X". The staff panel intersects the result with actual
        group membership before showing it; the cap is what keeps that
        intersection from turning into an unbounded number of
        ``get_chat_member`` calls (legacy had no cap and probed the
        whole table on every panel open, bot.py:31108-31115).

        Ordered by rank desc then ``user_id`` so the cap is stable
        across refreshes and always keeps the strongest ranks.
        """
        result = await self._session.execute(
            select(UserRow.user_id, UserRow.rank, UserRow.first_name)
            .where(UserRow.rank.is_not(None), UserRow.rank >= min_rank)
            .order_by(UserRow.rank.desc(), UserRow.user_id)
            .limit(limit)
        )
        return [
            RankedUser(user_id=int(uid), rank=int(rank), first_name=first_name)
            for uid, rank, first_name in result.all()
        ]

    async def set_rank(self, user_id: int, rank: int, *, by: int | None = None) -> None:
        """Persist ``rank`` for ``user_id`` (upsert, legacy bot.py:6677-6681).

        ``INSERT … ON CONFLICT DO UPDATE SET rank=excluded.rank`` so a
        user who has never messaged the bot can still be ranked — the
        same upsert shape legacy uses. ``by`` is the acting admin,
        carried for the service-layer audit log; the developer-
        immutability guard and the 0..6 range check are enforced one
        level up in ``RankService.set_rank`` (repo stays dumb by
        design — it is a pure persistence primitive).
        """
        del by  # audit attribution is logged by the service layer
        stmt = sqlite_insert(UserRow).values(user_id=user_id, rank=rank)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={"rank": stmt.excluded.rank},
        )
        await self._session.execute(stmt)

    async def upsert_from_telegram(
        self,
        *,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        language_code: str | None,
        is_premium: bool,
        now: datetime | None = None,
    ) -> UserEntity:
        """Mirror legacy ``update_user_info`` semantics exactly.

        ``joined_date`` is preserved on conflict (COALESCE) — only the
        very first row gets the wall clock from this call. ``last_seen``
        / ``last_active`` always advance.
        """
        now = now or datetime.now(UTC).replace(tzinfo=None)
        is_new = not await row_exists(self._session, UserRow.user_id, user_id)

        stmt = sqlite_insert(UserRow).values(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code=language_code,
            is_premium=is_premium,
            joined_date=now,
            last_seen=now,
            last_active=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                "username": stmt.excluded.username,
                "first_name": stmt.excluded.first_name,
                "last_name": stmt.excluded.last_name,
                "language_code": stmt.excluded.language_code,
                "is_premium": stmt.excluded.is_premium,
                "last_seen": stmt.excluded.last_seen,
                "last_active": stmt.excluded.last_active,
                # COALESCE preserves the original join timestamp across
                # upserts — matches legacy ``bot.py:44278-44305``.
                "joined_date": func.coalesce(
                    UserRow.__table__.c.joined_date, stmt.excluded.joined_date
                ),
            },
        )
        await self._session.execute(stmt)
        row = await reload_after_upsert(self._session, UserRow.user_id, user_id)
        return _to_entity(row, is_new=is_new)
