"""Async repository for ``users.user_group_nicknames``.

Tiny table, tiny repo. Two writes (upsert / delete), no reads — the
display-name lookup that legacy does (``get_user_display_name_in_chat``)
hasn't been ported yet and will get its own read path when it is.

Lives on the same ``users.db`` session as :class:`UsersRepo` because
both share the per-update transaction boundary: a ``/nick`` invocation
that touches ``users.users`` (via ``UserService.touch``) AND
``users.user_group_nicknames`` must commit/rollback atomically. Two
sessions would split the boundary and risk a half-saved state if the
handler crashed between writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.sql import func

from telegram_invite_bot.db.models.users import UserGroupNickname

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class NicknamesRepo:
    """``user_group_nicknames`` writes. Constructed per request."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set(self, *, user_id: int, chat_id: int, display_name: str) -> None:
        """Upsert ``(user_id, chat_id)`` → ``display_name``.

        ``func.now()`` resolves to ``datetime('now')`` on SQLite —
        same value legacy writes via raw SQL at bot.py:6934, so a row
        written by either side carries the DB clock (not the writer's
        Python clock). This matters because legacy and the new ORM
        race the same row under WAL, and a Python-side timestamp would
        otherwise interleave handler-clock and DB-clock values.
        """
        stmt = sqlite_insert(UserGroupNickname).values(
            user_id=user_id,
            chat_id=chat_id,
            display_name=display_name,
            updated_at=func.now(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                UserGroupNickname.user_id,
                UserGroupNickname.chat_id,
            ],
            set_={
                "display_name": stmt.excluded.display_name,
                "updated_at": func.now(),
            },
        )
        await self._session.execute(stmt)

    async def clear(self, *, user_id: int, chat_id: int) -> None:
        """Delete the row if present. Idempotent — bare ``/nick`` after
        never setting one still succeeds, matching legacy."""
        await self._session.execute(
            delete(UserGroupNickname).where(
                UserGroupNickname.user_id == user_id,
                UserGroupNickname.chat_id == chat_id,
            )
        )
