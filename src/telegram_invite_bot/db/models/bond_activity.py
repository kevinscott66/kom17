"""Activity-log tables for the couple joint-activities subsystem (L-34/35).

Two append-only logs — one per bond tier — recording every paid joint
activity a couple performs: which activity, how much pair-XP it granted,
who footed the bill, and when. The marriage/relationship *status cards*
(``/marriage`` + the ``/relationship`` per-pair card) read the most recent
15 rows to render a "history" view (L-34/L-35).

Both tables already exist in prod (``docs/prod_schemas.sql:107`` and
``:119``, created verbatim by the legacy ``bot.py:5568``/``:5583``
``CREATE TABLE IF NOT EXISTS`` bootstrap). The new aiogram pipeline never
modelled them because the original FEAT-COUPLE port wrote XP without a
log row (see ``BondsWriteRepo.add_relationship_xp`` docstring). This
module restores the model so the new pipeline can both WRITE the log
(from ``handlers/couple_activities``) and READ it (from the card history
views) instead of leaving the history button a dead end.

Schema mirrors the legacy DDL exactly (same columns, same indexes) so
``Base.metadata.create_all`` in tests produces a table legacy could keep
writing to without ALTER, and the prod table the new code writes to is
byte-for-byte the one legacy created. Pairs are stored in the canonical
``(min, max)`` order — same convention as :class:`Marriage` /
:class:`Relationship` — so a single ``WHERE user1_id=? AND user2_id=?``
matches regardless of which spouse paid.
"""

from __future__ import annotations

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import UsersBase


class MarriageActivityLog(UsersBase):
    """One row per marriage joint-activity performed (append-only)."""

    __tablename__ = "marriage_activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user1_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user2_id: Mapped[int] = mapped_column(Integer, nullable=False)
    activity_key: Mapped[str] = mapped_column(Text, nullable=False)
    xp_gained: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stored as ISO-8601 TEXT to match the legacy column (bot.py:5576).
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_marriage_activity_log_chat", "chat_id"),
        Index(
            "idx_marriage_activity_log_pair",
            "chat_id",
            "user1_id",
            "user2_id",
        ),
    )


class RelationshipActivityLog(UsersBase):
    """One row per relationship joint-activity performed (append-only)."""

    __tablename__ = "relationship_activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user1_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user2_id: Mapped[int] = mapped_column(Integer, nullable=False)
    activity_key: Mapped[str] = mapped_column(Text, nullable=False)
    xp_gained: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stored as ISO-8601 TEXT to match the legacy column (bot.py:5576).
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_relationship_activity_log_chat", "chat_id"),
        Index(
            "idx_relationship_activity_log_pair",
            "chat_id",
            "user1_id",
            "user2_id",
        ),
    )
