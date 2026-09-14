"""Read-only repository for ``economy.user_achievements`` (A-07).

Serves the ``/achievements`` card: the user's earned-achievement rows
plus the three economy-user stats the card footers with (balance /
games_played / games_won). Both reads hit the same ``economy.db``
session the :class:`EconomyMiddleware` opens, so one round-trip's worth
of session setup covers the whole card.

This repo is the READ half only; the write half is ported too, it
just lives elsewhere:
:meth:`telegram_invite_bot.repositories.economy_repo.EconomyRepo.award_achievements`,
driven from the tail of ``EconomyRepo.record_game`` and from
``DailyService``. Named rather than numbered (#1476): both line
anchors this sentence used to carry had drifted about twenty lines
onto a neighbouring docstring, which sends a reader looking for the
write half into the wrong method. What is still unported is the
``notified`` flip and legacy's auto-message — the card renders rows, it
does not announce them. The 14 achievement *definitions* live in code
(:data:`telegram_invite_bot.core.achievements.DEFINITIONS`), so this
repo never touches the (usually empty) legacy ``achievements``
definition table.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from sqlalchemy import select

from telegram_invite_bot.db.models.economy import EconomyUser, UserAchievement

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class EarnedAchievement(NamedTuple):
    """One earned row: the achievement id and when it was unlocked."""

    achievement_id: str
    earned_date: datetime | None


class UserStats(NamedTuple):
    """The three ``economy.users`` columns the card footers with.

    Kept distinct from :class:`telegram_invite_bot.core.entities.wallet.Wallet`
    because ``Wallet`` deliberately omits ``games_played`` / ``games_won``
    (the ``/balance`` card never needed them). Rather than widen the
    shared entity, the achievements card reads exactly the three numbers
    it renders.
    """

    balance: int
    games_played: int
    games_won: int


class AchievementsRepo:
    """``economy.user_achievements`` + user-stats reads. Per-request session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def earned_for_user(self, user_id: int) -> list[EarnedAchievement]:
        """Return the user's earned rows, newest unlock first.

        Mirrors legacy ``SELECT achievement_id, earned_date FROM
        user_achievements WHERE user_id=? ORDER BY earned_date DESC``.
        Rows whose ``achievement_id`` is unknown to the code definitions
        are returned untouched — the handler filters/renders them
        gracefully so a stray legacy id can never crash the card.
        """
        stmt = (
            select(UserAchievement.achievement_id, UserAchievement.earned_date)
            .where(UserAchievement.user_id == user_id)
            .order_by(UserAchievement.earned_date.desc())
        )
        result = await self._session.execute(stmt)
        return [EarnedAchievement(str(aid), dt) for aid, dt in result.all()]

    async def stats_for_user(self, user_id: int) -> UserStats:
        """Return ``(balance, games_played, games_won)`` for the user.

        A missing wallet (user never touched the economy) collapses to
        all-zeros — same user-visible outcome as legacy, which renders 0s
        for a user with no ``users`` row rather than erroring.
        """
        stmt = select(
            EconomyUser.balance,
            EconomyUser.games_played,
            EconomyUser.games_won,
        ).where(EconomyUser.user_id == user_id)
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return UserStats(0, 0, 0)
        return UserStats(int(row[0]), int(row[1]), int(row[2]))
