"""SQLAlchemy mapping for ``users.ai_daily_requests`` (M-P-2).

One row per (user_id, date_iso) pair, where ``date_iso`` is the UTC
calendar day (``YYYY-MM-DD``) that the count belongs to. Legacy wrote
this same table at ``bot.py:38479-38490`` via raw sqlite3; we model it
for SQLAlchemy access so the quota is enforced in the database rather
than in an in-memory counter a restart would forget.

Composite PK ``(user_id, date_iso)`` matches the prod dump exactly
(``docs/prod_schemas.sql:160``) so ``Base.metadata.create_all`` in
tests builds the same table the prod rows already live in, and
neither side ever needs an ALTER to read the other's.
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import UsersBase


class AiDailyRequest(UsersBase):
    """Daily AI quota counter row.

    ``date_iso`` is a TEXT column in prod (e.g. ``"2026-05-27"``) so
    the natural calendar boundary is operator-readable in raw SQL
    dumps and immune to timezone interpretation drift across drivers.
    The repo computes the day in UTC to match the audit's reset
    requirement ("reset at midnight UTC").
    """

    __tablename__ = "ai_daily_requests"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date_iso: Mapped[str] = mapped_column(String, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
