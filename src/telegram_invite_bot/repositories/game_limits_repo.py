"""Persistent per-user game anti-abuse counter (L-25).

Backs the cooldown / per-hour / per-day caps that legacy enforced with
``COUNT(*) FROM games WHERE ... game = 'roulette'`` (bot.py:14449 for the
hour, :14461 for the day) and that the new pipeline had only partially
via an in-memory limiter (resets on redeploy, per-process). One
append-only row per *completed* play lands in ``economy.game_plays``; the
cap check is three indexed reads over that table.

The widest window the policy layer asks about is 24h, and the cleanup
sweep prunes ``game_plays`` at 48h (``_GAME_PLAYS_RETENTION`` in
``scheduler/economy_cleanup.py``). Widening a cap window past the
retention would silently stop counting the older half of it, so the two
have to move together.

The repo is deliberately thin — pure persistence. The cap *policy* (the
180s / 8 / 25 numbers and the order they're evaluated) lives in
:class:`~telegram_invite_bot.services.game_limit_service.GameLimitService`
so the repo can be reused by any game without baking a roulette-specific
window in.

All datetimes are naive local time (``datetime.now()`` without tz),
matching the convention the rest of the economy DB uses for stored
datetimes (``InventoryItem.expires``, legacy ``games.date``) — mixing
naive and tz-aware values across the boundary would produce off-by-TZ
window errors. The caller injects ``now`` so tests pin boundaries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import CursorResult, delete, func, select

from telegram_invite_bot.db.models.game_limits import GamePlay

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class GameLimitsRepo:
    """``economy.game_plays`` access — record + windowed count + prune."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, user_id: int, *, game: str, now: datetime) -> None:
        """Append one completed-play stamp.

        Call ONLY after a play actually settled (mirrors legacy
        ``result.save()`` and the in-memory ``RouletteLimiter.record``):
        a rejected bet / insufficient-funds / blocked-by-cap attempt
        must not consume a slot or start a cooldown.
        """
        self._session.add(GamePlay(user_id=user_id, game=game, played_at=now))

    async def count_since(self, user_id: int, *, since: datetime) -> int:
        """Number of plays by ``user_id`` with ``played_at >= since``.

        The rolling-window cap primitive: the hour cap passes
        ``since = now - 1h``, the day cap ``since = now - 24h``. Counts
        across all games (the caps are shared today); a per-game
        divergence would add a ``game ==`` filter here.
        """
        stmt = (
            select(func.count())
            .select_from(GamePlay)
            .where(GamePlay.user_id == user_id, GamePlay.played_at >= since)
        )
        return int((await self._session.execute(stmt)).scalar() or 0)

    async def last_play_at(self, user_id: int) -> datetime | None:
        """Most recent ``played_at`` for ``user_id``, or ``None`` if never.

        Backs the cooldown check (``now - last >= COOLDOWN_SEC``). A
        single indexed read via the ``(user_id, played_at)`` index.
        """
        stmt = (
            select(GamePlay.played_at)
            .where(GamePlay.user_id == user_id)
            .order_by(GamePlay.played_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete_older_than(self, cutoff: datetime) -> int:
        """Prune stamps with ``played_at < cutoff``. Returns rows deleted.

        Housekeeping for the cleanup sweep (L-26): rows older than the
        widest cap window (a rolling day) can never affect a future cap
        decision, so deleting them keeps ``game_plays`` bounded by the
        active-player count × ``MAX_PER_DAY`` rather than growing
        forever. Pass ``cutoff = now - 1 day`` (or wider, for safety
        margin). Idempotent — a second pass with nothing due returns 0.
        """
        stmt = delete(GamePlay).where(GamePlay.played_at < cutoff)
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return int(result.rowcount or 0)
