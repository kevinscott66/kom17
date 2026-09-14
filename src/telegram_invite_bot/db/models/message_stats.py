"""ORM mapping for ``message_stats.db``.

Prod schema reference: ``docs/prod_schemas.sql`` lines 756-769. The
table is small (one row per ``(user_id, chat_id, date)`` triple). The
activity recording path has migrated: the new pipeline both writes it
(``middlewares/message_activity.py`` → ``MessageStatsRepo.increment``)
and reads it.

Indexes are declared verbatim so ``CREATE ALL`` against an in-memory
test DB matches prod's read patterns.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import MessageStatsBase


class MessageCount(MessageStatsBase):
    """One row per ``(user_id, chat_id, day)`` — count of messages that day."""

    __tablename__ = "message_counts"
    __table_args__ = (
        UniqueConstraint("user_id", "chat_id", "date", name="uq_message_counts_user_chat_date"),
        Index("idx_msg_counts_user", "user_id", "chat_id"),
        Index("idx_msg_counts_date", "date"),
        # #1975: the chat-scoped reads. Neither index above leads with
        # ``chat_id``, so the four /chatstats window queries were driven
        # off ``idx_msg_counts_date`` — a range over every chat's rows in
        # the window. Created on existing databases by
        # ``migrations/versions/message_stats/0002_chat_scoped_index.py``.
        Index("idx_msg_counts_chat_date", "chat_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stored as ``YYYY-MM-DD`` text — matches legacy's ``date('now')``
    # writes via raw sqlite3.
    date: Mapped[str] = mapped_column(String, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message: Mapped[datetime | None] = mapped_column(nullable=True)
