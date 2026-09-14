"""ORM mappings for ``moderation.db``.

T-020 maps the tables the new moderation handler reads and writes:

* ``warnings``       — one warning row per /warn issuance.
* ``moderation_log`` — append-only audit trail for all mod actions.

Prod schema reference: ``docs/prod_schemas.sql`` (the tables were
created by legacy at ``bot.py:8393-8688``). Every prod column is
declared so ``ModerationBase.metadata.create_all`` in tests produces
a schema-compatible table — a missing column would let legacy write
a row the new ORM cannot read, surfacing as silent NULLs.

NOTE: bans and mutes are enforced via the Telegram Bot API
(``ban_chat_member`` / ``restrict_chat_member``) and are NOT stored
in the new pipeline's own tables. Legacy owned ``bans`` / ``mutes``
and was removed in T-011, so those tables now only hold what it wrote
before the cutover; nothing reads them for enforcement, because the
enforcement lives in Telegram. The new handler delegates storage to the
Bot API and only writes the audit log.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import ModerationBase


class Warning(ModerationBase):
    """One warning, scoped to a single (user, chat) pair.

    ``active`` starts as ``True``; /unwarn sets it to ``False``
    (soft-delete — legacy never hard-deletes warning rows so the
    history survives for audit). ``expires`` is nullable: ``NULL``
    means "never expires", matching the legacy ``expires_days=0``
    path.
    """

    __tablename__ = "warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    admin_id: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("idx_warnings_user_chat", "user_id", "chat_id"),
        Index("idx_warnings_chat", "chat_id"),
    )


class ModerationLog(ModerationBase):
    """Append-only audit trail for all moderation actions.

    Legacy calls this ``log_moderation_action`` (bot.py:~8450).  One
    row per action, never mutated after INSERT.  The new pipeline
    writes here for every /ban, /kick, /mute, /warn, /unwarn, /pin,
    /unpin and /fine so operators have a searchable audit trail that
    survives even if Telegram's own logs are inaccessible.

    ``details`` is a free-form text field carrying action-specific
    context (e.g. ``duration_seconds=600``, ``warning_id=42``).
    """

    __tablename__ = "moderation_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    # M-M-2: ``user_id`` is now nullable. ``/pin`` and ``/unpin`` have no
    # human target (pin acts on a *message*, not a user), and the previous
    # convention of writing ``user_id=0`` collided with the unpin sentinel
    # and any future query that filters on ``user_id=0`` — audit queries
    # could not distinguish "no human target" from "pin against a
    # channel-forwarded reply whose ``from_user`` was None". NULL is the
    # honest representation; targeted actions (ban/kick/mute/warn/unwarn/
    # fine) keep populating it as before.
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    admin_id: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_modlog_chat_date", "chat_id", "date"),
        Index("idx_modlog_user", "user_id"),
    )
