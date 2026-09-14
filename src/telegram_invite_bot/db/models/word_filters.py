"""ORM mapping for ``moderation.db`` — per-group banned-word filter (L-52).

Legacy stores a ``word_filters`` table (bot.py:5739) with per-group
banned words; every group message is checked and offending ones are
deleted (+ optional warn). The legacy table lives in ``users.db`` with
extra ``action``/``severity`` columns the new pipeline does not use
(the new automod is delete-only, gated on live Telegram admin status).

In the new pipeline the table lives in ``moderation.db`` alongside the
other moderation state (warnings, audit log) so a single
``ModerationMiddleware`` session can serve both the moderation and
word-filter handlers.

Only the columns the new pipeline reads/writes are mapped:

* ``group_id`` — the chat the rule applies to.
* ``word``     — the banned word, stored lower-cased / stripped.
* ``added_by`` — admin who added the rule (audit attribution).
* ``created_at`` — insertion timestamp.

``unique(group_id, word)`` makes "add the same word twice" a no-op the
repo can detect and report cleanly instead of stacking duplicate rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import ModerationBase


class WordFilter(ModerationBase):
    """One banned word, scoped to a single group."""

    __tablename__ = "word_filters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    word: Mapped[str] = mapped_column(Text, nullable=False)
    added_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("group_id", "word", name="uq_word_filters_group_word"),)
