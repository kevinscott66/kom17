"""ORM mapping for ``moderation.db`` — per-group welcome template (L-57).

FEAT-WELCOME ships a fixed new-member greeting (``handlers/group_events.py``).
L-57 extends it: an admin can store a *custom* welcome template per group,
with ``{user}`` / ``{chat}`` placeholders, and toggle it on/off — mirroring
the legacy per-group welcome-text feature.

One row per group (``group_id`` PK):

* ``template`` — the raw admin-supplied template, ``NULL`` when unset (the
  group falls back to the default i18n welcome card). Stored verbatim;
  placeholder substitution + HTML-escaping happens at render time in the
  handler, never at write time, so the audit value stays the operator's
  literal text.
* ``enabled`` — ``1`` (on) by default; ``/welcome_off`` flips it to ``0``
  to suppress the custom template without discarding it (``/welcome_on``
  restores). A disabled row falls back to the default card exactly like an
  unset one.

This table lives in the MODERATION db (admin/group-scoped config sits
alongside warnings + the moderation audit log).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import ModerationBase


class WelcomeConfig(ModerationBase):
    """Per-group custom welcome template + enabled toggle (L-57)."""

    __tablename__ = "welcome_config"

    # Group chat ids are negative and can exceed 32-bit range for
    # supergroups (-100xxxxxxxxxx), so BigInteger — not the plain Integer
    # used by the older warning tables whose ids predate that concern.
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    template: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
