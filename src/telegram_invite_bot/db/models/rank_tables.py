"""ORM mappings for ``moderation.db`` rank tables (DESIGN_RANKS.md §2.1).

Both tables are DELTA-ONLY stores over in-code defaults
(:mod:`telegram_invite_bot.core.ranks`):

* ``rank_permissions`` — one row per (rank, permission) cell an operator
  changed via ``/perm set``; absent cells fall back to
  :data:`~telegram_invite_bot.core.ranks.DEFAULT_RANK_PERMISSIONS`.
  Mirrors legacy's "store only the edited matrix in settings.json"
  shape (legacy ``rank_permissions`` defaults bot.py:2611-2712, writes
  via ``/perm set``).
* ``command_rank_overrides`` — one row per command whose minimum rank an
  operator changed via ``/cmdcfg set``; absent commands fall back to
  :data:`~telegram_invite_bot.core.ranks.COMMAND_CATALOG` defaults
  (legacy ``command_rank_overrides`` in settings.json over the
  hardcoded catalog, bot.py:42340-42436 / 42808-42894).

Deviation from legacy (approved in the design doc §2.1): legacy kept
both deltas in ``settings.json``; the new pipeline has no settings.json
writer, so they live as rows in ``moderation.db`` — transactional and
auditable, display-neutral.
"""

from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import ModerationBase


class RankPermissionOverride(ModerationBase):
    """One overridden cell of the rank → permission matrix."""

    __tablename__ = "rank_permissions"

    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    permission: Mapped[str] = mapped_column(Text, primary_key=True)
    #: SQLite has no BOOLEAN; stored as 0/1 like the legacy JSON bools.
    allowed: Mapped[int] = mapped_column(Integer, nullable=False)


class CommandRankOverride(ModerationBase):
    """One overridden per-command minimum rank (``/cmdcfg set``)."""

    __tablename__ = "command_rank_overrides"

    command_key: Mapped[str] = mapped_column(Text, primary_key=True)
    min_rank: Mapped[int] = mapped_column(Integer, nullable=False)
