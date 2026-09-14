"""SQLAlchemy mapping for ``users.support_tickets``.

Schema matches ``docs/prod_schemas.sql`` byte-for-byte (Stage 2 dump).
Stage 14 only writes ``user_id, username, first_name, text, status,
created_at`` on ``/feedback`` — the answer/closed columns are filled
by the legacy admin reply flow we haven't ported yet. Declaring every
column means the test-time ``create_all`` produces a table the legacy
admin handler can also read/write without column-mismatch surprises.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import UsersBase


class SupportTicket(UsersBase):
    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="open", nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    answered_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
