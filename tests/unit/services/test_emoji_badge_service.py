"""``EmojiBadgeService`` — the equip/clear/render gates (#25).

The service is pure orchestration over two repos, so these tests use
lightweight stubs instead of a real DB: they pin the GATE ORDER (VIP
before set-membership), the render-time VIP re-check
(:meth:`active_badge` / :meth:`decorate_display_name`), and the
clear-is-ungated rule. The DB-backed mechanics live in
``tests/integration/repositories/test_emoji_badge_repo.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import OperationalError

from telegram_invite_bot.repositories.vip_repo import DEFAULT_VIP, VipProfile
from telegram_invite_bot.services.emoji_badge_service import (
    VIP_BADGE_SET,
    EmojiBadgeService,
    EquipOutcome,
)

_NOW = datetime(2026, 6, 5, 12, 0, 0)
_VALID = VIP_BADGE_SET[0]


class _StubBadges:
    """In-memory stand-in for :class:`EmojiBadgeRepo`."""

    def __init__(self) -> None:
        self.store: dict[int, str] = {}

    async def get(self, user_id: int) -> str | None:
        return self.store.get(user_id)

    async def upsert(self, *, user_id: int, emoji: str, now: datetime) -> None:
        self.store[user_id] = emoji

    async def clear(self, user_id: int) -> None:
        self.store.pop(user_id, None)


class _StubVip:
    """Stand-in for :class:`VipRepo` — VIP iff the id is in ``vip_ids``."""

    def __init__(self, vip_ids: set[int]) -> None:
        self.vip_ids = vip_ids

    async def get_active_profile(
        self, user_id: int, *, now: datetime, group_id: int | None = None
    ) -> VipProfile | None:
        return DEFAULT_VIP if user_id in self.vip_ids else None


def _make(vip_ids: set[int]) -> tuple[EmojiBadgeService, _StubBadges]:
    badges = _StubBadges()
    service = EmojiBadgeService(badges, _StubVip(vip_ids))  # type: ignore[arg-type]
    return service, badges


async def test_equip_non_vip_returns_not_vip_even_for_valid_emoji() -> None:
    """VIP gate fires FIRST — a valid emoji still gets the upsell."""
    service, badges = _make(vip_ids=set())
    outcome = await service.equip(7, _VALID, now=_NOW)
    assert outcome is EquipOutcome.NOT_VIP
    assert badges.store == {}  # nothing stored


async def test_equip_vip_invalid_emoji_returns_not_in_set() -> None:
    service, badges = _make(vip_ids={7})
    outcome = await service.equip(7, "🍕", now=_NOW)
    assert outcome is EquipOutcome.NOT_IN_SET
    assert badges.store == {}


async def test_equip_vip_valid_emoji_stores_and_returns_ok() -> None:
    service, badges = _make(vip_ids={7})
    outcome = await service.equip(7, _VALID, now=_NOW)
    assert outcome is EquipOutcome.OK
    assert badges.store == {7: _VALID}


async def test_clear_is_not_vip_gated() -> None:
    """A lapsed VIP can still tidy up their stored selection."""
    service, badges = _make(vip_ids=set())
    badges.store[7] = _VALID
    await service.clear(7)
    assert badges.store == {}


async def test_equipped_ignores_vip_status() -> None:
    """The raw read returns the stored badge regardless of VIP."""
    service, badges = _make(vip_ids=set())
    badges.store[7] = _VALID
    assert await service.equipped(7) == _VALID


async def test_active_badge_hidden_when_not_vip() -> None:
    """Render-time gate: stored badge but lapsed VIP → None."""
    service, badges = _make(vip_ids=set())
    badges.store[7] = _VALID
    assert await service.active_badge(7, now=_NOW) is None


async def test_active_badge_shown_when_vip() -> None:
    service, badges = _make(vip_ids={7})
    badges.store[7] = _VALID
    assert await service.active_badge(7, now=_NOW) == _VALID


async def test_active_badge_none_when_vip_but_no_badge() -> None:
    service, _ = _make(vip_ids={7})
    assert await service.active_badge(7, now=_NOW) is None


async def test_decorate_display_name_prepends_badge_for_vip() -> None:
    service, badges = _make(vip_ids={7})
    badges.store[7] = _VALID
    assert await service.decorate_display_name(7, "Alice", now=_NOW) == f"{_VALID} Alice"


async def test_decorate_display_name_unchanged_when_no_badge() -> None:
    service, _ = _make(vip_ids={7})
    assert await service.decorate_display_name(7, "Alice", now=_NOW) == "Alice"


async def test_decorate_display_name_unchanged_when_not_vip() -> None:
    service, badges = _make(vip_ids=set())
    badges.store[7] = _VALID
    assert await service.decorate_display_name(7, "Alice", now=_NOW) == "Alice"


async def test_safe_active_badge_matches_active_badge_on_happy_path() -> None:
    """The guarded reader is transparent when the lookup succeeds."""
    service, badges = _make(vip_ids={7})
    badges.store[7] = _VALID
    assert await service.safe_active_badge(7, now=_NOW) == _VALID


async def test_safe_active_badge_degrades_to_none_on_db_error() -> None:
    """A SQLAlchemyError (e.g. economy schema absent on the users-only
    /profile session) degrades to "no badge" rather than propagating —
    the card must still render."""

    class _BoomVip:
        async def get_active_profile(
            self, user_id: int, *, now: datetime, group_id: int | None = None
        ) -> VipProfile | None:
            raise OperationalError("no such table: users", {}, Exception())

    service = EmojiBadgeService(_StubBadges(), _BoomVip())  # type: ignore[arg-type]
    assert await service.safe_active_badge(7, now=_NOW) is None
