"""ORM mapping for ``economy.game_plays`` — persistent anti-abuse counter (L-25).

NET-NEW table (absent from the prod dump). Backs the per-user game
anti-abuse caps (cooldown 180s + 8/hour + 25/day) that legacy enforced
by ``COUNT(*) FROM games WHERE user_id = ? AND game = 'roulette' AND
date >= <window>`` (bot.py:14449 hour, :14461 day) plus an in-memory
cooldown key (bot.py:14438-14444).

Why a dedicated table rather than re-reading ``economy.games``
-------------------------------------------------------------
``economy.games`` (mapped as :class:`~telegram_invite_bot.db.models.economy.GameResult`)
is the *result-of-a-game* ledger — it carries bet / profit / win and is
read by ``/duel_stats`` and the leaderboards. Re-pointing the abuse caps
at it would (a) couple the cap window to whether a game writes a row at
all (e.g. a /duel that never settles writes nothing), and (b) make every
cap check a COUNT over a table that grows without bound. A purpose-built
append-only ``game_plays`` table keyed by ``(user_id, played_at)`` keeps
the cap query a single indexed range scan and lets the cleanup sweep
prune anything older than 48h — twice the widest cap window, so a
cap check never reads past the prune horizon — keeping it small.

The new pipeline previously enforced these caps only via an in-memory
``RouletteLimiter`` (deleted by this cutover, so there is nothing left to
link to), which reset on every process restart (a redeploy hands every abuser a
fresh slate) and is per-process (two workers each grant the full cap).
Persisting the stamps here makes the caps real and shared.

``game`` discriminates the play family (``"roulette"`` / ``"duel"`` /
``"rps"``) so a future per-game cap divergence is a WHERE clause, not a
schema change; today every game shares the one cap set.

Migration ``0008_game_plays`` creates it; ``create_all`` builds it for
tests (importing :mod:`telegram_invite_bot.repositories.game_limits_repo`
pulls this module in, registering the table on ``EconomyBase.metadata``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import EconomyBase


class GamePlay(EconomyBase):
    """One completed-play stamp for the anti-abuse caps (L-25).

    Append-only: one row per *completed* play (rejected / blocked
    attempts write nothing, matching legacy which counted only
    ``result.save()`` rows). The composite index on
    ``(user_id, played_at)`` backs the rolling-window COUNT the cap
    check runs; the standalone ``played_at`` index backs the cleanup
    sweep's range delete.
    """

    __tablename__ = "game_plays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    game: Mapped[str] = mapped_column(String, nullable=False)
    played_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_game_plays_user_played", "user_id", "played_at"),
        Index("idx_game_plays_played", "played_at"),
    )
