"""Async repository for stored voice transcriptions (L-70).

Backs the ``voice_transcriptions`` table in ``users.db`` (modelled on
:class:`VoiceTranscription`). Two operations:

* :meth:`add` — persist one billed voice-transcription request.
* :meth:`stats` — aggregate counts + last text for the L-72 stats view
  (read by agent C2).

Two readings of the same table, deliberately different (#260):
:meth:`count_since` / :meth:`seconds_since` back the daily spend
ceilings and count every billed request, including the ones that came
back without a usable transcript; :meth:`stats` backs an
operator-facing card that says "transcriptions" and counts only the
rows that have one.

Writes run on the caller's session; ``created_at`` is filled Python-side
by the model default (:func:`db_now`, naive-UTC) for new-pipeline rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from telegram_invite_bot.db.models.users import VoiceTranscription

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class VoiceTranscriptionStats:
    """Aggregate view of a group's transcription history (L-72).

    ``last_text`` / ``last_created`` describe the most recently inserted
    row (by ``id``); ``avg_processing_ms`` is the rounded mean of the
    ``processing_time`` column across all rows that recorded one. All
    fields are ``None``/zero when the group has no transcriptions yet.
    """

    total: int
    last_text: str | None
    last_created: datetime | None
    avg_processing_ms: int | None


class VoiceTranscriptionRepo:
    """``voice_transcriptions`` access. Constructed per request with an open session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        group_id: int,
        user_id: int,
        message_id: int | None,
        file_id: str | None,
        file_unique_id: str | None,
        duration: int | None,
        transcribed_text: str | None,
        language: str | None,
        model_used: str | None,
        processing_time: int | None,
    ) -> None:
        """Persist one BILLED voice-transcription request.

        ``created_at`` is left to the model default (:func:`db_now`), so
        callers never pass it. The row is added to the caller's session;
        commit/rollback is owned by the surrounding ``session_for``
        context (or the injected request session).

        ``transcribed_text=None`` is a legitimate row, not a bug: #260
        records the billed-but-unusable Whisper outcomes so the daily
        ceilings count them. Such a row is invisible to :meth:`stats`
        and fully visible to :meth:`count_since` / :meth:`seconds_since`.
        """
        self._session.add(
            VoiceTranscription(
                group_id=group_id,
                user_id=user_id,
                message_id=message_id,
                file_id=file_id,
                file_unique_id=file_unique_id,
                duration=duration,
                transcribed_text=transcribed_text,
                language=language,
                model_used=model_used,
                processing_time=processing_time,
            )
        )
        await self._session.flush()

    async def count_since(self, group_id: int, since: datetime) -> int:
        """How many rows this group has stored at or after ``since``.

        Backs the per-group daily STT ceiling
        (``OPENAI_STT_GROUP_DAILY_LIMIT``): the handler calls this before
        spending an OpenAI Whisper request, so an unattended group can't
        drain the operator's key. ``since`` must be NAIVE UTC — both the
        new pipeline's :func:`db_now` default and legacy's
        ``CURRENT_TIMESTAMP`` write naive UTC, so one comparison covers
        rows from either writer.

        Rows land on every BILLED Whisper request, which is not the
        same as every successful one. Until #260 this docstring claimed
        the two coincided and that "a failed call costs nothing" — false
        for the ``empty`` and ``bad_response`` outcomes, which arrive on
        an HTTP 200 after OpenAI has transcribed and charged for the
        audio. Those now write a row with ``transcribed_text=None`` so
        the ceiling sees the spend; ``network``/``http_*``/``no_key`` do
        not, because no request was served. See ``_BILLED_ERRORS`` in
        ``handlers/voice_transcribe``.

        The counter is therefore denominated in invoice lines, which is
        what a spend ceiling should bound. :meth:`stats` is the one that
        means "transcriptions" and skips the text-less rows.
        """
        return (
            await self._session.execute(
                select(func.count())
                .select_from(VoiceTranscription)
                .where(
                    VoiceTranscription.group_id == group_id,
                    VoiceTranscription.created_at >= since,
                )
            )
        ).scalar_one()

    async def seconds_since(self, group_id: int, since: datetime) -> int:
        """Total transcribed audio seconds for this group since ``since``.

        The sibling :meth:`count_since` bounds how many Whisper requests
        a group may make; this bounds how much audio those requests
        carry, which is what the invoice is actually computed from. Same
        rows, same naive-UTC ``since`` contract, so one index serves
        both.

        ``duration`` is nullable — legacy rows predate it — so the sum
        is coalesced rather than trusted to be non-NULL. Under-counting
        an old row is the harmless direction: it can only make the gate
        more permissive for a group that already stopped accruing.

        Like :meth:`count_since` this spans every billed request,
        text-less #260 rows included — those carry the duration OpenAI
        charged for even though the transcript came back empty, which is
        precisely the audio this budget exists to bound.
        """
        total = (
            await self._session.execute(
                select(func.coalesce(func.sum(VoiceTranscription.duration), 0))
                .select_from(VoiceTranscription)
                .where(
                    VoiceTranscription.group_id == group_id,
                    VoiceTranscription.created_at >= since,
                )
            )
        ).scalar_one()
        return int(total or 0)

    async def seconds_since_for_user(self, user_id: int, since: datetime) -> int:
        """Total transcribed audio seconds for this SPEAKER since ``since``.

        The sibling :meth:`seconds_since` bounds one group; this one
        bounds one person across every group at once, which is the only
        scope a per-group budget cannot express (#1938). Groups are free
        to create, so a ceiling that resets per chat resets on demand.

        Deliberately not filtered by ``group_id``: the point is to see
        the same speaker's spend in chats this one knows nothing about.
        Same naive-UTC ``since`` contract and the same coalesce as its
        sibling, for the same reason — legacy rows predate ``duration``
        and under-counting them can only be permissive, never punitive.
        """
        total = (
            await self._session.execute(
                select(func.coalesce(func.sum(VoiceTranscription.duration), 0))
                .select_from(VoiceTranscription)
                .where(
                    VoiceTranscription.user_id == user_id,
                    VoiceTranscription.created_at >= since,
                )
            )
        ).scalar_one()
        return int(total or 0)

    async def stats(self, group_id: int) -> VoiceTranscriptionStats:
        """Aggregate transcription stats for ``group_id`` (L-72 read side).

        Counts only rows that carry a transcript. The card this backs
        says "Всего" under the heading "Статистика транскрипций", and
        after #260 the table also holds billed requests that produced no
        text (silence, noise, an unreadable body). Those belong to the
        spend counters, not to a count of transcriptions — folding them
        in would inflate the total, and ``last_created`` would point at
        a row whose preview is blank.
        """
        transcribed = VoiceTranscription.transcribed_text.is_not(None)

        total = (
            await self._session.execute(
                select(func.count())
                .select_from(VoiceTranscription)
                .where(VoiceTranscription.group_id == group_id, transcribed)
            )
        ).scalar_one()

        avg_raw = (
            await self._session.execute(
                select(func.avg(VoiceTranscription.processing_time)).where(
                    VoiceTranscription.group_id == group_id, transcribed
                )
            )
        ).scalar_one_or_none()
        avg_processing_ms = round(avg_raw) if avg_raw is not None else None

        last = (
            await self._session.execute(
                select(
                    VoiceTranscription.transcribed_text,
                    VoiceTranscription.created_at,
                )
                .where(VoiceTranscription.group_id == group_id, transcribed)
                .order_by(VoiceTranscription.id.desc())
                .limit(1)
            )
        ).one_or_none()

        if last is None:
            return VoiceTranscriptionStats(
                total=int(total),
                last_text=None,
                last_created=None,
                avg_processing_ms=avg_processing_ms,
            )
        return VoiceTranscriptionStats(
            total=int(total),
            last_text=last.transcribed_text,
            last_created=last.created_at,
            avg_processing_ms=avg_processing_ms,
        )
