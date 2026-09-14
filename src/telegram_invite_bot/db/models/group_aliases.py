"""ORM mapping for ``moderation.db`` — per-group dynamic command aliases (L-60).

Legacy NOTE (verified): the telebot monolith's ``/alias add|del|list``
(bot.py:42074 ``cmd_alias``) is **owner-only** (``is_owner`` gate,
bot.py:42084-42086) and **global** — aliases live in the singleton
``settings["command_aliases"]`` dict (bot.py:42099-42103), not per
group. Backlog item L-60 re-scopes the feature to per-group,
group-admin-managed aliases; that intentional upgrade is what this
table models — a deliberate divergence from legacy, not a port bug.

Columns:

* ``group_id``       — the chat the alias belongs to.
* ``word``           — the trigger word, stored in the legacy-normalised
                       form (``normalize_alias_token``, bot.py:41951-41953:
                       lower-cased, ``[^\\wа-яё]`` stripped).
* ``target_command`` — canonical command name WITHOUT the leading slash
                       (legacy stored ``/command`` strings; we store the
                       bare name and render the slash at the edges).
* ``added_by``       — admin who created the mapping (audit attribution).
* ``created_at``     — insertion timestamp.

``unique(group_id, word)`` mirrors the legacy dict semantics: one word
maps to exactly one command per group, and re-adding the same word
overwrites the mapping (legacy ``aliases_cfg[alias_key] = command_str``,
bot.py:42138).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import ModerationBase


class GroupAlias(ModerationBase):
    """One trigger word → command mapping, scoped to a single group."""

    __tablename__ = "group_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    word: Mapped[str] = mapped_column(Text, nullable=False)
    target_command: Mapped[str] = mapped_column(Text, nullable=False)
    added_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("group_id", "word", name="uq_group_aliases_group_word"),)
