"""Async repository for ``economy.user_emoji_badge`` — the VIP cosmetic
emoji badge (#25).

One row per user = their currently-equipped badge. The surface is the
minimal CRUD the feature needs: read the equipped badge (for the display
resolver and the /emojis card), upsert a new selection (/emoji_set), and
clear it (bare /emoji_set). Validation that ``emoji`` is a member of the
curated set lives at the service layer (:class:`EmojiBadgeService`); the
repo trusts its arguments, same posture as every other repo here.

There is NO money path — equipping is free for VIP — so unlike
``EconomyRepo`` / ``ChecksRepo`` there are no guarded balance UPDATEs;
the upsert is a plain ``INSERT ... ON CONFLICT DO UPDATE`` on the
``user_id`` primary key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.economy import EconomyUser, UserEmojiBadge
from telegram_invite_bot.utils.time import to_naive_utc, unix_ts

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


class EmojiBadgeRepo:
    """``economy.user_emoji_badge`` access — get / upsert / clear."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> str | None:
        """Return the user's equipped badge emoji, or ``None``.

        A pure read — the caller (display resolver) decides whether to
        actually render it based on the user's *current* VIP status, so
        this never filters on VIP itself.
        """
        result = await self._session.execute(
            select(UserEmojiBadge.emoji).where(UserEmojiBadge.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def active_badges(self, user_ids: list[int], *, now: datetime) -> dict[int, str]:
        """Return ``{user_id: emoji}`` for the VIP-active subset of ``user_ids``.

        The render-time gate (only show a badge while the user is
        currently VIP) applied in *one* batched JOIN against
        ``economy.users.vip_till`` instead of N per-row calls — this is
        the leaderboard path (:mod:`handlers.top`), where decorating 50
        names one query at a time would be a needless fan-out.

        A user appears in the result iff they have a stored badge AND
        ``vip_till > now`` (strict, matching :meth:`VipRepo.get_active_profile`).
        Users with no badge, no wallet row, or a lapsed grant are simply
        absent — the caller renders their plain name.

        ``now`` must be AWARE. ``vip_till`` is a legacy ``time.time()``
        REAL, so the comparison goes through ``.timestamp()``, which
        reads a naive value in the host's local zone;
        :func:`utils.time.unix_ts` guards that and logs rather than
        letting a lapsed VIP keep its badge for the length of the
        host's UTC offset.
        """
        if not user_ids:
            return {}
        deadline = unix_ts(now, where="EmojiBadgeRepo.active_badges")
        stmt = (
            select(UserEmojiBadge.user_id, UserEmojiBadge.emoji)
            .join(EconomyUser, EconomyUser.user_id == UserEmojiBadge.user_id)
            .where(
                UserEmojiBadge.user_id.in_(user_ids),
                EconomyUser.vip_till.is_not(None),
                EconomyUser.vip_till > deadline,
            )
        )
        result = await self._session.execute(stmt)
        return {uid: emoji for uid, emoji in result.all()}

    async def upsert(self, *, user_id: int, emoji: str, now: datetime) -> None:
        """Set ``user_id``'s badge to ``emoji`` (insert or replace).

        ``user_id`` is the primary key, so a re-equip overwrites the
        previous selection in one statement under SQLite's write lock —
        no read-then-write race.

        ``set_at`` is a naive-UTC ``DateTime`` column while the caller's
        ``now`` is aware (the same value gates VIP through
        ``.timestamp()``, which requires awareness). The conversion
        happens here rather than in the handler so neither consumer gets
        a frame that is wrong for it.
        """
        set_at = to_naive_utc(now)
        stmt = sqlite_insert(UserEmojiBadge).values(user_id=user_id, emoji=emoji, set_at=set_at)
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={"emoji": emoji, "set_at": set_at},
        )
        await self._session.execute(stmt)

    async def clear(self, user_id: int) -> None:
        """Remove ``user_id``'s badge row (bare ``/emoji_set``)."""
        await self._session.execute(delete(UserEmojiBadge).where(UserEmojiBadge.user_id == user_id))
