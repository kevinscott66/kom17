"""Async repository for ``moderation.db`` — per-group banned-word filter (L-52).

* :class:`WordFilterRepo` — per-request, session-scoped. Methods:

  - ``add``          — insert a banned word; returns False if already present.
  - ``remove``       — delete a banned word; returns False if not present.
  - ``list``         — fetch all banned words for a group (sorted).
  - ``list_entries`` — the same rows WITH their primary keys (RR-4 #43).
  - ``remove_by_id`` — delete one row by (group, id); returns the word.
  - ``match``        — return the first banned word contained in a text, or None.

All matching is case-insensitive: words are normalised (``strip().lower()``)
on the way in, and ``match`` normalises the candidate text the same way
before comparison. No inline SQL — all access goes through SQLAlchemy
ORM / core expressions. The repo trusts its arguments; validation
(empty word, command-looking word) lives in the handler.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, NamedTuple, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.exc import IntegrityError

from telegram_invite_bot.db.models.word_filters import WordFilter

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def normalize_word(word: str) -> str:
    """Canonical form used for both storage and matching: stripped + lower."""
    return word.strip().lower()


class WordEntry(NamedTuple):
    """One stored filter word together with its row id (RR-4 #43).

    The id exists so a per-word delete BUTTON can name its target
    without putting the word itself on the wire: Telegram caps
    ``callback_data`` at 64 bytes, and a word is up to 100 characters —
    two bytes each in Cyrillic — so a word-carrying payload is not
    merely long, it is unbuildable, and the whole keyboard would fail to
    render. Legacy did exactly that (``f"moderation_del_{word}"``,
    bot.py:32386). An id is also stable under re-sorting: the list is
    alphabetical, so a positional index would point at a different word
    the moment somebody added one between render and tap.
    """

    id: int
    word: str


class WordFilterRepo:
    """``moderation.db`` word-filter access. Constructed per request with a session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, *, group_id: int, word: str, added_by: int | None) -> bool:
        """Insert a banned word for ``group_id``.

        Returns ``True`` if a new row was inserted, ``False`` if the word
        was already filtered for this group. ``unique(group_id, word)``
        is what guarantees idempotency; the SELECT below is only the
        cheap path to the same answer, and the ``SAVEPOINT`` around the
        INSERT is what makes it honest when the two racers arrive
        together.
        """
        normalized = normalize_word(word)
        existing = await self._session.execute(
            select(WordFilter.id).where(
                WordFilter.group_id == group_id,
                WordFilter.word == normalized,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return False
        try:
            async with self._session.begin_nested():
                self._session.add(
                    WordFilter(
                        group_id=group_id,
                        word=normalized,
                        added_by=added_by,
                        created_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                )
                await self._session.flush()
        except IntegrityError:
            # The check above is a SELECT, and this project opens the
            # SQLite transaction lazily and only for a write-headed
            # statement (``db/engines.py:204-210``) — so it ran outside
            # any transaction, and a concurrent ``/filter_add`` of the
            # same word can commit in the gap before this INSERT takes
            # the writer lock. ``unique(group_id, word)`` then fires.
            #
            # "Already in the list" is not a consolation prize here: it
            # is the very answer the SELECT would have given a
            # millisecond later, and the word the admin asked for IS
            # filtered. Reporting it beats what used to happen —
            # ``/filter_add`` wraps this call in no ``try`` (unlike
            # ``/groupadmin``'s copy of the flow), so the error reached
            # the errors router and the admin got a crash card for a
            # request that had in fact succeeded.
            #
            # Narrow on purpose, in the shape ``BondsRepo`` already uses
            # (bonds_repo.py:879): UNIQUE is the only failure this
            # ``False`` describes truthfully, and everything else still
            # propagates. The savepoint has rolled back, so the update's
            # transaction stays usable for whatever the handler writes
            # next.
            return False
        return True

    async def remove(self, *, group_id: int, word: str) -> bool:
        """Delete a banned word from ``group_id``.

        Returns ``True`` if a row was deleted, ``False`` if the word was
        not present.
        """
        normalized = normalize_word(word)
        result = await self._session.execute(
            delete(WordFilter).where(
                WordFilter.group_id == group_id,
                WordFilter.word == normalized,
            )
        )
        return cast("CursorResult[object]", result).rowcount > 0

    async def remove_by_id(self, *, group_id: int, word_id: int) -> str | None:
        """Delete row ``word_id`` **if it belongs to** ``group_id``.

        Returns the deleted word (so the caller can name it in a
        confirmation), or ``None`` when there was nothing to delete —
        either the row is gone already or it belongs to another group.

        The ``group_id`` term is the whole point: ``word_id`` arrives
        from a button payload, i.e. through the user, so an id copied
        out of another chat's panel must resolve to nothing rather than
        edit that chat's filter.
        """
        row = await self._session.execute(
            select(WordFilter.word).where(
                WordFilter.id == word_id,
                WordFilter.group_id == group_id,
            )
        )
        word = row.scalar_one_or_none()
        if word is None:
            return None
        await self._session.execute(
            delete(WordFilter).where(
                WordFilter.id == word_id,
                WordFilter.group_id == group_id,
            )
        )
        return word

    async def list_entries(self, *, group_id: int) -> list[WordEntry]:
        """All banned words for ``group_id`` with their ids, sorted."""
        result = await self._session.execute(
            select(WordFilter.id, WordFilter.word)
            .where(WordFilter.group_id == group_id)
            .order_by(WordFilter.word)
        )
        return [WordEntry(id=int(row[0]), word=row[1]) for row in result.all()]

    async def list(self, *, group_id: int) -> list[str]:
        """Return all banned words for ``group_id``, alphabetically sorted.

        Derived from :meth:`list_entries` so the two views can never
        disagree about which rows a group has or in what order.
        """
        return [entry.word for entry in await self.list_entries(group_id=group_id)]

    async def match(self, *, group_id: int, text: str) -> str | None:
        """Return the first banned word contained in ``text``, or ``None``.

        Substring match (case-insensitive). This does NOT mirror legacy:
        legacy anchored every entry at ``\\b`` before compiling it
        (bot.py:8297-8298). The runtime filter that does match legacy is
        ``handlers.wordfilter.WordFilterAutomodMiddleware._compile``;
        nothing in ``src`` calls this helper, which exists for the
        repository tests only. Returns the *stored* (normalised) form of
        the matched word so callers can log/report it.
        """
        haystack = text.lower()
        words = await self.list(group_id=group_id)
        for word in words:
            if word and word in haystack:
                return word
        return None
