"""Read-only per-user game-history aggregates (cluster F2 / L-20).

``/duel_stats`` (and any future ``/roulette_stats``-style card) needs a
single SELECT of seven aggregates over ``economy.games``. The write
side of that table is owned by :class:`EconomyRepo.record_game`
(cluster F1) — this repo deliberately carries **no writes** so the two
ownership domains never collide: F1 appends rows, F2 reads roll-ups.

Filtering on ``game == <name>`` matches legacy ``get_duel_stats``
(bot.py) verbatim — other game rows (roulette, dice, flip, cpc) live in
the same table and would skew a per-game card if folded in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import case, func, select

from telegram_invite_bot.db.models.economy import GameResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class GameStatsRow:
    """Aggregate view of one user's record in one game.

    Frozen + slots so renderers can't mutate the numbers in flight.
    ``max_loss`` is **signed** — the most-negative profit value (the
    largest loss); renderers typically show ``abs(max_loss)``.
    """

    total: int
    wins: int
    losses: int
    total_profit: int
    avg_bet: int
    max_win: int
    max_loss: int


class GameStatsRepo:
    """``economy.games`` read-only aggregates, one query per card."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def stats_for(self, user_id: int, *, game: str) -> GameStatsRow:
        """One SELECT with seven aggregates for ``user_id`` in ``game``.

        ``CASE WHEN win`` matches the storage convention (BOOLEAN as
        0/1). Every SUM/AVG/MAX/MIN is wrapped in ``coalesce`` so an
        empty record renders zeros, never ``None``.
        """
        wins_expr = func.sum(case((GameResult.win.is_(True), 1), else_=0))
        losses_expr = func.sum(case((GameResult.win.is_(False), 1), else_=0))
        result = await self._session.execute(
            select(
                func.count().label("total"),
                wins_expr.label("wins"),
                losses_expr.label("losses"),
                func.coalesce(func.sum(GameResult.profit), 0).label("total_profit"),
                func.coalesce(func.avg(GameResult.bet), 0).label("avg_bet"),
                func.coalesce(func.max(GameResult.profit), 0).label("max_win"),
                func.coalesce(func.min(GameResult.profit), 0).label("max_loss"),
            ).where(GameResult.user_id == user_id, GameResult.game == game)
        )
        row = result.one()
        return GameStatsRow(
            total=int(row.total or 0),
            wins=int(row.wins or 0),
            losses=int(row.losses or 0),
            total_profit=int(row.total_profit or 0),
            avg_bet=int(row.avg_bet or 0),
            max_win=int(row.max_win or 0),
            max_loss=int(row.max_loss or 0),
        )
