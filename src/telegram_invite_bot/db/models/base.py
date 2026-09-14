"""Per-database :class:`DeclarativeBase` classes.

Each SQLite file has its own metadata so Alembic can autogenerate
migrations independently per DB without cross-contamination (a single
``Base`` would force one shared ``MetaData``).

Stage 2 ships only the base classes + their imports — concrete table
mappings land per-handler as we migrate (Stage 4: ``UsersUser``,
``EconomyUser``; Stage 5+: the rest). This is deliberate: ORM-mapping
all 67 prod tables up front would freeze type signatures we haven't
agreed on yet. The baseline Alembic migration captures the existing
schema verbatim from the prod dump so we never lose track of reality.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class UsersBase(DeclarativeBase):
    """Metadata root for ``users.db`` (~31 tables in prod)."""


class EconomyBase(DeclarativeBase):
    """Metadata root for ``economy.db`` (~26 tables in prod)."""


class ActivityBase(DeclarativeBase):
    """Metadata root for ``activity.db`` (4 tables in prod)."""


class ModerationBase(DeclarativeBase):
    """Metadata root for ``moderation.db`` (6 tables in prod)."""


class MessageStatsBase(DeclarativeBase):
    """Metadata root for ``message_stats.db`` (1 table in prod)."""
