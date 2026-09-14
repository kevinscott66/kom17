"""SQLAlchemy 2.0 models, grouped by database file.

Submodules are added per migration stage. Importing this package is
side-effect-free — the actual tables register on their respective
``DeclarativeBase`` metadata when each submodule is imported.
"""

from __future__ import annotations

from telegram_invite_bot.db.models.base import (
    ActivityBase,
    EconomyBase,
    MessageStatsBase,
    ModerationBase,
    UsersBase,
)

__all__ = [
    "ActivityBase",
    "EconomyBase",
    "MessageStatsBase",
    "ModerationBase",
    "UsersBase",
]
