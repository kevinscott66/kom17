"""Async repository for ``moderation.db`` — per-group command aliases (L-60).

* :class:`GroupAliasRepo` — per-request, session-scoped. Methods:

  - ``upsert``  — insert a word→command mapping, or overwrite the target
                  if the word is already aliased (legacy dict-assignment
                  semantics, bot.py:42138). Returns ``True`` when a NEW
                  row was created, ``False`` when an existing mapping was
                  overwritten.
  - ``remove``  — delete a mapping; returns ``False`` if absent.
  - ``list``    — all ``(word, target_command)`` pairs for a group,
                  sorted by word (legacy lists sorted, bot.py:42110).
  - ``mapping`` — ``dict[word, target_command]`` for the routing
                  middleware cache.

Words are normalised via :func:`normalize_alias_word` on the way in —
the exact legacy ``normalize_alias_token`` rule (bot.py:41951-41953).
Target-command validation (format, no chaining) lives in the handler.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.exc import IntegrityError

from telegram_invite_bot.db.models.group_aliases import GroupAlias

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Legacy ``normalize_alias_token`` (bot.py:41951-41953): lower-case and
# strip every char that is neither ``\w`` nor а-яё (the ``re.IGNORECASE``
# on the legacy pattern is redundant after ``.lower()``; kept behaviour
# is identical).
_NORMALIZE_RE = re.compile(r"[^\wа-яё]+", re.IGNORECASE)


def normalize_alias_word(token: str) -> str:
    """Canonical trigger-word form — mirrors legacy bot.py:41951-41953."""
    return _NORMALIZE_RE.sub("", token.lower()).strip()


class GroupAliasRepo:
    """``moderation.db`` group-alias access. Constructed per request with a session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self, *, group_id: int, word: str, target_command: str, added_by: int | None
    ) -> bool:
        """Create or overwrite the mapping for ``word`` in ``group_id``.

        Returns ``True`` if a new row was inserted, ``False`` if an
        existing mapping was overwritten (legacy ADD silently overwrites,
        bot.py:42138).
        """
        normalized = normalize_alias_word(word)
        existing = await self._session.execute(
            select(GroupAlias).where(
                GroupAlias.group_id == group_id,
                GroupAlias.word == normalized,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            row.target_command = target_command
            row.added_by = added_by
            await self._session.flush()
            return False
        try:
            async with self._session.begin_nested():
                self._session.add(
                    GroupAlias(
                        group_id=group_id,
                        word=normalized,
                        target_command=target_command,
                        added_by=added_by,
                        created_at=datetime.now(UTC).replace(tzinfo=None),
                    )
                )
                await self._session.flush()
        except IntegrityError:
            # The SELECT above ran outside any transaction: this project
            # opens one lazily and only for a write-headed statement
            # (``db/engines.py:204-210``). So a concurrent ``/alias add``
            # of the same word can commit its row in the gap before this
            # INSERT takes the writer lock, and
            # ``uq_group_aliases_group_word`` fires.
            #
            # ADD means overwrite (legacy bot.py:42138), so the answer to
            # losing that race is to do what this method would have done
            # had the SELECT seen the row: point the word at the target
            # the admin just named. Doing it as an UPDATE rather than
            # re-reading keeps the whole thing inside the writer lock the
            # failed INSERT has now taken, so it cannot lose a second
            # time. ``False`` — "overwrote" — is then the truth.
            #
            # Narrow in the shape ``BondsRepo`` already uses
            # (bonds_repo.py:879): UNIQUE is the only failure this branch
            # describes honestly, and everything else propagates to the
            # errors router. The savepoint has rolled back, so the
            # pending row is gone and the session is usable.
            await self._session.execute(
                update(GroupAlias)
                .where(
                    GroupAlias.group_id == group_id,
                    GroupAlias.word == normalized,
                )
                .values(target_command=target_command, added_by=added_by)
                .execution_options(synchronize_session=False)
            )
            return False
        return True

    async def remove(self, *, group_id: int, word: str) -> bool:
        """Delete the mapping for ``word``; ``True`` if a row was deleted."""
        normalized = normalize_alias_word(word)
        result = await self._session.execute(
            delete(GroupAlias).where(
                GroupAlias.group_id == group_id,
                GroupAlias.word == normalized,
            )
        )
        return cast("CursorResult[object]", result).rowcount > 0

    async def list(self, *, group_id: int) -> list[tuple[str, str]]:
        """All ``(word, target_command)`` pairs for ``group_id``, word-sorted."""
        result = await self._session.execute(
            select(GroupAlias.word, GroupAlias.target_command)
            .where(GroupAlias.group_id == group_id)
            .order_by(GroupAlias.word)
        )
        return [(word, target) for word, target in result.all()]

    async def mapping(self, *, group_id: int) -> dict[str, str]:
        """``word -> target_command`` map for the routing middleware."""
        return dict(await self.list(group_id=group_id))
