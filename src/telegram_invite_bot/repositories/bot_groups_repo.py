"""Async repository for the ``users.bot_groups`` table (L-49 write-side).

Until ``/transfer_rights`` migrated, ``bot_groups`` was read-only in the
new pipeline (legacy writes a row per ``my_chat_member`` update; the
``/mygroups`` handler reads ``added_by_user_id`` inline). This repo adds
the FIRST new-pipeline write: re-attributing a group to a new bot-side
owner. Since #111 it also owns the membership itself — :meth:`register`
on join, :meth:`deactivate` on leave — because legacy is stopped and
nothing else was writing those rows at all.

Every read here is scoped to *active* rows. A group the bot has been
removed from must not show up in the ``/shop`` picker: buying "for" it
credits 15% of the price to the registrar of a chat the bot can no
longer post to.

Legacy anchor: ``cmd_transfer_rights`` (bot.py:41603-41759) transferred
the GLOBAL ``owner_user_id`` in ``settings.json`` — a single-owner-bot
concept that doesn't exist in the multi-group pipeline. The new
semantics (per the rank epic, DESIGN_RANKS.md) re-target the per-group
attribution that ``/mygroups`` and the group-admin surfaces key off:
``bot_groups.added_by_user_id``.

The reads duplicate the inline selects in ``handlers/mygroups.py`` on
purpose — that handler predates this repo and is owned by another
cluster, so it keeps its engine-level queries; new callers should come
through here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.users import BotGroup
from telegram_invite_bot.utils.time import db_now

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


# Written as ``is_active IS NOT 0`` rather than ``!= 0``: SQL's ``!=``
# is unknown against NULL and would silently hide a row written before
# the column existed by a path that skipped the server default.
_ACTIVE = BotGroup.is_active.is_distinct_from(0)


class BotGroupsRepo:
    """``users.bot_groups`` access. Constructed per request with an open
    session; the caller owns transaction boundaries (``session_for``
    commits on clean exit).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_owned(self, user_id: int, *, limit: int = 200) -> list[tuple[int, str | None]]:
        """All ``(chat_id, chat_title)`` rows attributed to ``user_id``.

        Ordered by ``chat_id`` ascending — same stable order (and the
        same pathological-attribution cap) as the ``/mygroups`` list, so
        the transfer picker shows the groups in the order the user
        already knows.
        """
        rows = await self._session.execute(
            select(BotGroup.chat_id, BotGroup.chat_title)
            .where(BotGroup.added_by_user_id == user_id)
            .where(_ACTIVE)
            .order_by(BotGroup.chat_id)
            .limit(limit)
        )
        return [(int(r[0]), r[1]) for r in rows.all()]

    async def get_owned(self, chat_id: int, user_id: int) -> tuple[int, str | None] | None:
        """The ``(chat_id, title)`` row IF ``user_id`` is its registered
        owner; ``None`` for "unknown group", "someone else's group" and
        "the bot is no longer in it" alike — indistinguishable so callbacks can't probe
        which chat ids the bot knows about (same contract as
        ``/mygroups``' ``_fetch_owned_group``).
        """
        row = (
            await self._session.execute(
                select(BotGroup.chat_id, BotGroup.chat_title)
                .where(BotGroup.chat_id == chat_id)
                .where(BotGroup.added_by_user_id == user_id)
                .where(_ACTIVE)
            )
        ).first()
        if row is None:
            return None
        return (int(row[0]), row[1])

    async def get_active(self, chat_id: int) -> tuple[int, str | None] | None:
        """The ``(chat_id, title)`` row for a chat the bot is STILL in.

        Unlike :meth:`get_owned` this asks nothing about who registered
        the group — the caller is the ``grp_`` deep link (#1926), where
        the question is "is this a real group of ours" and the person
        tapping the button is an ordinary member, not the registrar.

        Not an authorisation check on its own, and deliberately so: it
        says the chat exists, never that this user belongs to it. The
        deep-link handler confirms membership against Telegram before
        it stores anything.
        """
        row = (
            await self._session.execute(
                select(BotGroup.chat_id, BotGroup.chat_title)
                .where(BotGroup.chat_id == chat_id)
                .where(_ACTIVE)
            )
        ).first()
        if row is None:
            return None
        return (int(row[0]), row[1])

    async def transfer_ownership(self, chat_id: int, *, from_user_id: int, to_user_id: int) -> bool:
        """Re-attribute ``chat_id`` from ``from_user_id`` to ``to_user_id``.

        The ``added_by_user_id == from_user_id`` guard is part of the
        WHERE clause, so the UPDATE is atomic against a concurrent
        transfer (or a legacy-side re-attribution): if the caller is no
        longer the owner by the time the statement runs, zero rows
        match and the method reports ``False`` — the handler surfaces
        that as "group not found" rather than silently stealing the row.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(BotGroup)
                .where(BotGroup.chat_id == chat_id)
                .where(BotGroup.added_by_user_id == from_user_id)
                .where(_ACTIVE)
                .values(added_by_user_id=to_user_id)
            ),
        )
        return bool(result.rowcount == 1)

    async def register(
        self,
        chat_id: int,
        *,
        added_by_user_id: int,
        chat_title: str | None,
        has_admin_rights: bool,
    ) -> None:
        """Record that the bot is in ``chat_id``, or that it is back.

        ``added_by_user_id`` is written ONLY when the row is new. On a
        re-add the existing attribution stands: the row is what the 15%
        group cut is paid against, so letting the last person to add the
        bot claim it would turn "kick the bot and add it again" into a
        way for any co-admin to take over a group's payouts — and would
        silently undo a deliberate ``/transfer_rights``.

        ``added_at`` is likewise kept: it means "since when has the bot
        been in this group", and a removal followed by a re-add does not
        make that story start over.
        """
        stmt = sqlite_insert(BotGroup).values(
            chat_id=chat_id,
            added_by_user_id=added_by_user_id,
            added_at=db_now(),
            chat_title=chat_title,
            bot_has_admin_rights=int(has_admin_rights),
            is_active=1,
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=[BotGroup.chat_id],
                set_={
                    "chat_title": stmt.excluded.chat_title,
                    "bot_has_admin_rights": stmt.excluded.bot_has_admin_rights,
                    "is_active": 1,
                },
            )
        )

    async def deactivate(self, chat_id: int) -> bool:
        """Mark ``chat_id`` as one the bot is no longer in.

        ``True`` when a row actually changed. ``False`` covers both "no
        such row" and "already inactive" — Telegram can deliver the same
        leave transition twice, and neither case is worth a log line.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(BotGroup)
                .where(BotGroup.chat_id == chat_id)
                .where(_ACTIVE)
                .values(is_active=0)
            ),
        )
        return bool(result.rowcount)
