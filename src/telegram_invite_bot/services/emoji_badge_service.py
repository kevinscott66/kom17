"""VIP cosmetic emoji-badge feature (#25).

A "custom emoji" here is a **cosmetic badge** the bot stores and renders
next to a VIP's display name on surfaces it fully controls (the
``/profile`` card and the tops/leaderboards). It is deliberately NOT a
Telegram premium ``custom_emoji`` entity — those require Premium + a
Fragment username and cannot be applied to other users' messages. See
``docs/CUSTOM_EMOJI_VIP_SPEC.md`` for the full product rationale.

Key invariants:

* **Free for VIP.** There is no money path. Equipping is gated solely on
  an active *global* VIP grant (``VipRepo.get_active_profile`` reading
  ``users.vip_till``). ``/emoji_buy`` is an informational alias, not a
  debit.
* **Fixed curated set.** ``emoji`` may only be a member of
  :data:`VIP_BADGE_SET`; arbitrary user input is rejected.
* **Render-time VIP re-check.** :meth:`decorate_display_name` asks VIP
  status every render, so when a VIP grant lapses the badge simply stops
  showing while the stored selection persists (returns on renewal).
"""

from __future__ import annotations

from enum import Enum, auto
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from datetime import datetime

    from telegram_invite_bot.repositories.emoji_badge_repo import EmojiBadgeRepo
    from telegram_invite_bot.repositories.vip_repo import VipRepo

log = logger.bind(component="services.emoji_badge")


# The curated VIP badge set. Each entry is a single emoji grapheme;
# membership is the ONLY validation /emoji_set applies. Changing this
# tuple changes what every VIP can equip — a deliberate, reviewable edit
# (a removed emoji stays in any existing row but fails re-equip, and the
# display resolver still renders it since it trusts stored values).
VIP_BADGE_SET: tuple[str, ...] = (
    "👑",
    "💎",
    "🔥",
    "⭐",
    "🌟",
    "🦄",
    "🍀",
    "🎭",
    "🚀",
    "⚡",
    "🌈",
    "❤️",
)


class EquipOutcome(Enum):
    """Result of an :meth:`EmojiBadgeService.equip` attempt."""

    OK = auto()
    NOT_VIP = auto()
    NOT_IN_SET = auto()


class EmojiBadgeService:
    """Equip / clear / render the VIP cosmetic badge.

    Composed per request by :class:`EconomyMiddleware` over ONE
    ``economy.db`` session (middlewares/economy.py:99): ``emoji_badge_repo``
    reads ``user_emoji_badge`` and ``vip_repo`` reads
    ``EconomyUser.vip_till``. Both live in ``economy.db`` —
    ``EconomyUser.__tablename__`` is the ``users`` table INSIDE that file
    (db/models/economy.py:41), not the separate ``users.db``. This
    docstring used to say the two repos read different files and rest the
    "nothing to make atomic" conclusion on that; the conclusion holds, but
    only because the service reads and never writes. The middleware states
    it correctly at middlewares/economy.py:287-294.
    """

    def __init__(self, emoji_badge_repo: EmojiBadgeRepo, vip_repo: VipRepo) -> None:
        self._badges = emoji_badge_repo
        self._vip = vip_repo

    async def is_vip(self, user_id: int, *, now: datetime) -> bool:
        """``True`` iff ``user_id`` has an active *global* VIP grant."""
        return await self._vip.get_active_profile(user_id, now=now) is not None

    async def equipped(self, user_id: int) -> str | None:
        """Return the user's stored badge emoji (ignores VIP status)."""
        return await self._badges.get(user_id)

    async def equip(self, user_id: int, emoji: str, *, now: datetime) -> EquipOutcome:
        """Equip ``emoji`` for ``user_id`` after the VIP + set-membership gates.

        Order: VIP first (so a non-VIP gets the upsell, not a
        "not in set" message even for a valid emoji), then membership.
        """
        if not await self.is_vip(user_id, now=now):
            return EquipOutcome.NOT_VIP
        if emoji not in VIP_BADGE_SET:
            return EquipOutcome.NOT_IN_SET
        await self._badges.upsert(user_id=user_id, emoji=emoji, now=now)
        return EquipOutcome.OK

    async def clear(self, user_id: int) -> None:
        """Remove the user's badge (bare ``/emoji_set``). Idempotent.

        Not VIP-gated: clearing a cosmetic row is always harmless, and a
        lapsed-VIP user should still be able to tidy up their selection.
        """
        await self._badges.clear(user_id)

    async def active_badge(self, user_id: int, *, now: datetime) -> str | None:
        """Return the badge to RENDER for ``user_id`` right now, or ``None``.

        ``None`` when the user is not currently VIP OR has no badge
        equipped. This is the render-time gate: a lapsed VIP grant hides
        the badge here without touching the stored row, so it returns
        automatically on renewal. Callers that build the display name
        themselves (the ``/profile`` card, which HTML-escapes the name
        separately) use this and prepend the badge.
        """
        if not await self.is_vip(user_id, now=now):
            return None
        return await self._badges.get(user_id)

    async def safe_active_badge(self, user_id: int, *, now: datetime) -> str | None:
        """Like :meth:`active_badge`, but degrades to ``None`` on a DB error.

        The badge on the ``/profile`` card is a *non-critical display
        enhancement* — the card must render whether or not this lookup
        succeeds. The private ``/profile`` text path opens an economy
        session (via :class:`EconomyMiddleware`) but, by design, runs
        against a users-only deployment / test where the economy schema
        (``user_emoji_badge`` / ``users.vip_till``) may be absent. A
        :class:`SQLAlchemyError` there (missing table, transient lock)
        degrades to "no badge" rather than 500-ing the whole card.

        Read-only, so a swallowed read can never corrupt state; the
        worst case is a VIP momentarily not seeing their badge on the
        card, which a retry fixes. Mirrors
        :meth:`VipDisplayService.resolve`'s same-rationale guard
        (``vip_display.py``).
        """
        try:
            return await self.active_badge(user_id, now=now)
        except SQLAlchemyError:
            log.bind(uid=user_id).warning("emoji_badge: lookup failed; rendering without badge")
            return None

    async def decorate_display_name(self, user_id: int, base_name: str, *, now: datetime) -> str:
        """Return ``base_name`` prefixed with the badge when applicable.

        Convenience wrapper over :meth:`active_badge` for call sites that
        just want the combined string. ``base_name`` is the
        already-resolved (and, at the call site, HTML-escaped) display
        name; the badge is trusted (a :data:`VIP_BADGE_SET` member) so it
        needs no escaping.
        """
        badge = await self.active_badge(user_id, now=now)
        return f"{badge} {base_name}" if badge else base_name
