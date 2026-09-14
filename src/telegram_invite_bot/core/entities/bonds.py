"""Domain entities for marriages and relationships (Stage 19).

Pair-shaped value objects so handlers can render leaderboards without
holding a live session. ``first_name`` is denormalised (joined from
``users.users``) at read time so the display layer never has to fetch
names a second time — same pattern as :class:`InventoryEntry` in
``shop.py``.

Two parallel types instead of one polymorphic ``Bond``: the legacy
schema keeps marriages and relationships in separate tables with
different XP curves and different extra columns (``duration_days``,
``auto_divorce``). Squashing them into one entity would mean the type
either grows optional marriage-only fields or loses information the
``/marriages`` renderer relies on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MarriagePair:
    user1_id: int
    user2_id: int
    user1_name: str | None
    user2_name: str | None
    created_at: datetime
    experience: int
    # Purchased days from ``/marry_extend``. Added on top of the
    # natural calendar days when computing the marriage category.
    extra_days: int


@dataclass(frozen=True, slots=True)
class RelationshipPair:
    user1_id: int
    user2_id: int
    user1_name: str | None
    user2_name: str | None
    created_at: datetime
    experience: int
