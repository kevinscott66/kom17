"""End-to-end ``/emojis`` + friends — VIP cosmetic emoji badge (#25).

The unit/integration suites pin the service gates and the repo CRUD.
This file pins the *wiring + copy*: that ``/emojis``, ``/emoji_set``,
``/emoji_preview`` and ``/emoji_buy`` reach the new emoji router, gate
on VIP, and persist the selection. Group calls must fall through to
legacy (private-only filter), matching every other ported economy stub.

VIP is seeded by giving the user an ``EconomyUser`` row with a future
``vip_till`` — that's exactly what :class:`VipRepo` reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from aiogram.dispatcher.event.bases import UNHANDLED

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, UserEmojiBadge
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.services.emoji_badge_service import VIP_BADGE_SET
from tests.e2e.handlers.conftest import assert_chat_scope_refusal, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory

_USER = 555
_VALID = VIP_BADGE_SET[0]
# Relative to wall-clock so the VIP window never goes stale as real time
# advances past a hardcoded date (the previous fixed 2026-07-05 silently
# expired once the clock rolled past it, failing every VIP-gated test).
_FUTURE_TS = (datetime.now(UTC) + timedelta(days=30)).timestamp()
_PAST_TS = (datetime.now(UTC) - timedelta(days=30)).timestamp()


def _update(text: str, *, user_id: int = _USER, chat_type: str = "private") -> Any:
    return make_message_update(text, chat_type=chat_type, user_id=user_id)


async def _seed_user(
    registry: Any, *, user_id: int = _USER, vip_till: float | None = _FUTURE_TS
) -> None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=user_id, balance=0, language="ru", vip_till=vip_till))
        await session.commit()


async def _read_badge(registry: Any, user_id: int = _USER) -> str | None:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(UserEmojiBadge, user_id)
        return row.emoji if row is not None else None


# --- /emojis (list) ---------------------------------------------------------


async def test_emojis_non_vip_gets_upsell(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_user(registry, vip_till=_PAST_TS)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/emojis"))
    assert result is not UNHANDLED
    assert "VIP" in sent[0]["text"]
    assert "/vip_shop" in sent[0]["text"]


async def test_emojis_vip_lists_set(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/эмодзи"))
    assert result is not UNHANDLED
    assert _VALID in sent[0]["text"]


# --- /emoji_set (equip / clear) --------------------------------------------


async def test_emoji_set_vip_equips_and_persists(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update(f"/emoji_set {_VALID}"))
    assert result is not UNHANDLED
    assert _VALID in sent[0]["text"]
    assert await _read_badge(registry) == _VALID


async def test_emoji_set_non_vip_rejected_and_not_stored(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_user(registry, vip_till=_PAST_TS)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update(f"/emoji_set {_VALID}"))
    assert result is not UNHANDLED
    assert "VIP" in sent[0]["text"]
    assert await _read_badge(registry) is None


async def test_emoji_set_invalid_emoji_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/emoji_set 🍕"))
    assert result is not UNHANDLED
    assert "набор" in sent[0]["text"].lower()
    assert await _read_badge(registry) is None


async def test_emoji_set_bare_clears(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Bare ``/emoji_set`` clears the stored selection (VIP-ungated)."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_user(registry)
    # Pre-seed a badge to clear.
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(UserEmojiBadge(user_id=_USER, emoji=_VALID, set_at=None))
        await session.commit()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/emoji_set"))
    assert result is not UNHANDLED
    assert "убран" in sent[0]["text"].lower()
    assert await _read_badge(registry) is None


# --- /emoji_preview ---------------------------------------------------------


async def test_emoji_preview_renders_name_with_badge(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_user(registry)
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(UserEmojiBadge(user_id=_USER, emoji=_VALID, set_at=None))
        await session.commit()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/emoji_preview"))
    assert result is not UNHANDLED
    assert _VALID in sent[0]["text"]


async def test_emoji_preview_no_badge_hints(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/emoji_preview"))
    assert result is not UNHANDLED
    assert "/emoji_set" in sent[0]["text"]


# --- /emoji_buy -------------------------------------------------------------


async def test_emoji_buy_vip_is_informational_no_charge(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/emoji_buy"))
    assert result is not UNHANDLED
    # No money path — copy points at /emoji_set, balance untouched.
    assert "/emoji_set" in sent[0]["text"]
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        user = await session.get(EconomyUser, _USER)
        assert user is not None
        assert user.balance == 0


# --- private-only filter (+ its #123 refusal twin) -----------------------------------------------


async def test_emoji_group_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Emoji commands are private-only — a group call is told so (#123).

    The router-level private filter still keeps the badge catalog out
    of the group; the exact match on the refusal is what proves it,
    since a leaked catalog would show up as a second sent message.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    await _seed_user(registry)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/emojis", chat_type="supergroup"))

    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="emojis")
