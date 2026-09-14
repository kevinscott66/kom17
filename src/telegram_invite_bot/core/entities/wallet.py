"""Domain entity for an economy wallet — what handlers see.

Kept distinct from the SQLAlchemy ``EconomyUser`` row so the handler
layer can't accidentally mutate ORM state after the session closes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Wallet:
    user_id: int
    balance: int
    total_earned: int
    total_spent: int
    daily_streak: int
    last_daily: datetime | None
    language: str
    # RR-2/RR-3 #26: lifetime game counters (for the /balance dashboard).
    # Defaulted so hand-built Wallets (tests, menu tap) stay valid.
    games_played: int = 0
    games_won: int = 0
    # #1946: when this wallet row was seeded, naive-UTC like every other
    # timestamp the new pipeline writes (``EconomyRepo.get_or_create``).
    # Read by the ``/daily`` new-account lockout; ``None`` on the rows
    # legacy created before the column was populated, which the guard
    # deliberately treats as "old enough" rather than locking out every
    # pre-existing user. Defaulted so hand-built Wallets stay valid.
    registered: datetime | None = None
