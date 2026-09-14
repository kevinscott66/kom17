"""Async repository for per-group voice-transcription settings (L-70).

Backs the six L-70 columns on the ``group_settings`` table in
``users.db`` (modelled on :class:`GroupSettings`). The repo exposes a
small domain view (:class:`VoiceSettings`) plus one upsert per column;
the handler reads the view to decide whether/where to transcribe.

Upserts use the ``sqlite_insert + on_conflict_do_update`` pattern from
``handlers/rules.py`` so a brand-new group row is created on the first
write and only the touched column changes on subsequent writes (the
prod ``updated_at`` / unmodelled columns keep their schema defaults).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.users import GroupSettings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm.attributes import InstrumentedAttribute

    _Toggle = Callable[[int], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    """Domain view of the L-70 transcription columns for one group.

    A missing ``group_settings`` row yields all defaults (transcription
    disabled), matching legacy behaviour where an un-configured group
    never transcribes.
    """

    enabled: bool
    target: str
    language: str
    log_chat_id: int | None
    auto_delete: bool
    only_admins: bool


_DEFAULTS = VoiceSettings(
    enabled=False,
    target="chat",
    language="ru",
    log_chat_id=None,
    auto_delete=False,
    only_admins=False,
)


class VoiceSettingsRepo:
    """``group_settings`` L-70 access. Constructed per request with an open session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, group_id: int) -> VoiceSettings:
        """Return the transcription settings for ``group_id``.

        Missing row → :data:`_DEFAULTS` (transcription off). ``NULL``
        column values fall back to their per-field default so a row
        written by legacy with only some columns set still reads sanely.
        """
        row = (
            await self._session.execute(
                select(
                    GroupSettings.voice_transcription,
                    GroupSettings.transcription_target,
                    GroupSettings.transcription_language,
                    GroupSettings.transcription_log_chat_id,
                    GroupSettings.auto_delete_voice,
                    GroupSettings.transcription_only_for_admins,
                ).where(GroupSettings.group_id == group_id)
            )
        ).one_or_none()
        if row is None:
            return _DEFAULTS
        return VoiceSettings(
            enabled=bool(row.voice_transcription),
            target=row.transcription_target or "chat",
            language=row.transcription_language or "ru",
            log_chat_id=row.transcription_log_chat_id,
            auto_delete=bool(row.auto_delete_voice),
            only_admins=bool(row.transcription_only_for_admins),
        )

    async def set_enabled(self, group_id: int, enabled: bool) -> None:
        """Upsert the master on/off flag for ``group_id``."""
        await self._upsert(group_id, {"voice_transcription": 1 if enabled else 0})

    async def set_target(self, group_id: int, target: str) -> None:
        """Upsert the delivery target (``chat``/``private``/``admins``/``log_chat``)."""
        await self._upsert(group_id, {"transcription_target": target})

    async def set_language(self, group_id: int, language: str) -> None:
        """Upsert the Whisper language hint for ``group_id``."""
        await self._upsert(group_id, {"transcription_language": language})

    async def set_auto_delete(self, group_id: int, auto_delete: bool) -> None:
        """Upsert "delete the voice note once transcribed" (RR-6 #73).

        ``voice_transcribe`` has honoured this flag since L-70; until now
        nothing in the new pipeline could *set* it, so a group that had it
        on from legacy could never turn it back off from the bot.
        """
        await self._upsert(group_id, {"auto_delete_voice": 1 if auto_delete else 0})

    async def set_only_admins(self, group_id: int, only_admins: bool) -> None:
        """Upsert "transcribe admins' voice notes only" (RR-6 #73)."""
        await self._upsert(group_id, {"transcription_only_for_admins": 1 if only_admins else 0})

    async def toggle_enabled(self, group_id: int) -> bool:
        """Atomically flip the master on/off flag; return the new value."""
        return await self._toggle(GroupSettings.voice_transcription)(group_id)

    async def toggle_auto_delete(self, group_id: int) -> bool:
        """Atomically flip "delete the voice note once transcribed"."""
        return await self._toggle(GroupSettings.auto_delete_voice)(group_id)

    async def toggle_only_admins(self, group_id: int) -> bool:
        """Atomically flip "transcribe admins' voice notes only"."""
        return await self._toggle(GroupSettings.transcription_only_for_admins)(group_id)

    # NOTE: no ``set_log_chat_id``. ``transcription_log_chat_id`` is read
    # (delivery honours it) but nothing writes it — and legacy never wrote
    # it either: the monolith only ever ALTERed the column in and read it
    # back (bot.py:5870, 7832, 38840), with no menu, command or prompt to
    # set it. Adding a writer here would mean inventing a surface the old
    # bot never had, so the menu instead says out loud that the target
    # falls back to a group reply while the chat is unset (RR-6 #73).

    def _toggle(self, column: InstrumentedAttribute[int]) -> _Toggle:
        """Build the atomic flip for one boolean-ish column (#740).

        A read-then-write flip in the handler is NOT safe even inside a
        single session: ``get`` is a bare ``SELECT``, and the engine only
        promotes a connection to ``BEGIN IMMEDIATE`` on a write statement
        (``db/engines.py:204-210``). The read therefore runs in autocommit,
        outside the ``users.db`` writer lock, so two admins tapping the same
        button both read the old value and both write the same new one —
        two taps, one flip.

        The flip lives in SQL instead: ``1 - COALESCE(col, 0)`` inside the
        ``DO UPDATE`` of a single upsert, with ``RETURNING`` handing back
        the value that was actually stored. ``COALESCE`` mirrors
        :meth:`get`, which already reads a ``NULL`` column as ``False``.
        The insert branch seeds ``1`` because a group with no row is off
        by default, so the first tap must turn the feature on.
        """

        async def run(group_id: int) -> bool:
            insert = sqlite_insert(GroupSettings).values(group_id=group_id, **{column.key: 1})
            stmt = insert.on_conflict_do_update(
                index_elements=[GroupSettings.group_id],
                set_={column.key: 1 - func.coalesce(column, 0)},
            ).returning(column)
            stored = (await self._session.execute(stmt)).scalar_one()
            await self._session.flush()
            return bool(stored)

        return run

    async def _upsert(self, group_id: int, values: dict[str, object]) -> None:
        """INSERT ... ON CONFLICT(group_id) DO UPDATE for the given columns.

        Mirrors ``handlers/rules.py:_store_rules`` — a single atomic
        SQLite upsert touching only the supplied columns, run on the
        caller's session (no fresh connection, which would deadlock the
        ``users.db`` writer lock).
        """
        stmt = sqlite_insert(GroupSettings).values(group_id=group_id, **values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[GroupSettings.group_id],
            set_=values,
        )
        await self._session.execute(stmt)
        await self._session.flush()
