"""End-to-end VIP-stub routing.

Six commands, six static replies. The point of the test isn't
content correctness (covered by the source listing) — it's that
every alias matches the right handler and group chats fall through.

Migrated to the shared ``make_wired`` / ``capture_outgoing`` fixtures
at Stage 25 — see conftest.py for the rationale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, ShopItem, Transaction
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.vip import _MAX_PLANS
from telegram_invite_bot.keyboards.builders import ShopBuyPrompt
from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length
from tests.e2e.handlers.conftest import (
    assert_chat_scope_refusal,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


#: The ``STATS_TIMEZONE`` default the wiring fixture boots with (#1957).
#: The VIP card renders its expiry in that zone, same as ``/profile``, so
#: the expected date has to be computed there too — asserting a UTC date
#: made this test fail for the three hours a day the two disagree.
_STATS_TZ = ZoneInfo("Europe/Moscow")


def _update(text: str, *, chat_type: str = "private", language_code: str | None = None) -> Update:
    """File-local defaults: user 555. Delegates to the shared builder."""
    return make_message_update(text, chat_type=chat_type, user_id=555, language_code=language_code)


def _capture_with_markup(
    bot: Bot, monkeypatch: pytest.MonkeyPatch, sink: list[dict[str, Any]]
) -> None:
    """Record ``SendMessage`` text + ``reply_markup``.

    The shared ``capture_outgoing`` fixture deliberately drops
    ``reply_markup`` (see conftest), but the /vip_shop keyboard tests
    inspect the inline buttons — same posture as test_help.py's bespoke
    capture.
    """
    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "SendMessage":
            sink.append({"text": method.text, "reply_markup": method.reply_markup})
            return Message(
                message_id=1,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        raise AssertionError(f"unexpected Telegram call: {type(method).__name__}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


# (command, must-appear-substring). Every alias of every command goes
# through this table — a regression in Command(...) alias parsing or
# the F.chat.type filter would flip the relevant row.
_CASES = [
    # ``/vip`` left this static-stub table when richness-restore #20 turned
    # it into a live status card: it now reads ``users.vip_till`` to render
    # active/inactive + expiry + days-left, so it needs the economy schema
    # wired (which the bare ``make_wired()`` here does not provide). Its
    # active/inactive coverage lives in the dedicated tests below.
    # ``/emojis`` + friends (`/emoji_set`, `/emoji_buy`, `/emoji_preview`,
    # `/эмодзи`) left this static-stub table when #25 turned them into the
    # real VIP cosmetic-badge flow (handlers/emoji.py). They need the
    # economy schema wired (VIP gate + badge store), which the bare
    # ``make_wired()`` here does not provide — routing + equip/clear/
    # preview behaviour lives in test_emoji.py.
    ("/voice_settings", "Голосовые"),
    # ``/voice_stats`` left this static-stub table when it became a real
    # ledger-backed handler — its routing + zero-state + counts coverage
    # lives in the dedicated tests below (it needs the economy schema
    # wired, which the bare ``make_wired()`` here does not provide).
    # Stage 26 replaced the ``/lang`` stub here with a real handler in
    # the ``language`` router. The wiring assertion for ``/lang`` lives
    # in test_main_router_wiring.py; behaviour is in test_language.py.
]


@pytest.mark.parametrize(("command", "needle"), _CASES)
async def test_command_renders_expected_copy(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    command: str,
    needle: str,
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update(command))
    assert result is not UNHANDLED
    assert needle in sent[0]["text"]


async def test_group_vip_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A group ``/vip`` is answered with "DM only" (#123)."""
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/vip", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="vip")


# --- /vip status card (richness #20) ---------------------------------------


async def test_vip_inactive_shows_offer_with_concrete_perks(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No active grant → the offer card, with the real perk *values*
    (so the advertised numbers can't drift from DEFAULT_VIP)."""
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/vip"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "VIP" in body
    assert "/vip_shop" in body
    # Concrete perk values from DEFAULT_VIP rendered, not a static blurb.
    assert "+15%" in body
    assert "−50%" in body
    # Not the active card.
    assert "Действует до" not in body


async def test_vip_active_shows_expiry_and_days_left(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """An active grant → live status with the concrete expiry date and
    a positive days-left count."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    till = datetime.now(UTC) + timedelta(days=30)
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=0, language="ru", vip_till=till.timestamp()))
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/vip"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "Действует до" in body
    assert till.astimezone(_STATS_TZ).strftime("%d.%m.%Y") in body
    assert "Осталось" in body


# --- /vip_shop (the real flow) ---------------------------------------------


async def test_vip_shop_empty_catalog(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No VIP rows seeded → friendly empty-state, NOT silence."""
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/vip_shop"))
    assert result is not UNHANDLED
    assert "VIP-магазин" in sent[0]["text"]
    assert "недоступн" in sent[0]["text"].lower()


async def test_vip_shop_lists_only_vip_plans_in_price_order(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``type='vip'`` rows show, cheapest first, with a buy button each.

    A non-VIP row and a sold-out VIP row are seeded as negatives — they
    must not surface. The perk blurb (legacy ``bot.py:12332``) renders in
    the header.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add_all(
            [
                ShopItem(id=1, name="👑 VIP (3 месяца)", price=2500, type="vip", stock=-1),
                ShopItem(id=2, name="👑 VIP (1 месяц)", price=1000, type="vip", stock=-1),
                ShopItem(id=3, name="🎲 Большой подарок", price=50, type="luck", stock=-1),
                ShopItem(id=4, name="👑 VIP (sold out)", price=10, type="vip", stock=0),
            ]
        )
        await session.commit()

    sent: list[dict[str, Any]] = []
    _capture_with_markup(bot, monkeypatch, sent)
    result = await dispatcher.feed_update(bot, _update("/vip_shop"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    # Perk blurb present.
    assert "+15%" in body
    assert "−50%" in body
    # Non-VIP and sold-out VIP rows excluded.
    assert "подарок" not in body.lower()
    assert "sold out" not in body.lower()
    # Cheapest VIP first. The card shows the CURATED plan name (RR-2 #21),
    # not the raw catalog column, so the assertion is on that copy.
    assert 0 <= body.find("VIP на месяц") < body.find("VIP на 3 месяца")
    # Buy buttons carry the SAME ShopBuyPrompt the /shop keyboard uses,
    # one per visible VIP plan.
    markup = sent[0]["reply_markup"]
    assert markup is not None
    item_ids = sorted(
        ShopBuyPrompt.unpack(btn.callback_data).item_id
        for row in markup.inline_keyboard
        for btn in row
    )
    assert item_ids == [1, 2]


async def test_vip_shop_cards_show_perks_saving_and_stock(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """RR-2 #21: each plan renders its curated perk blurb, the per-day
    saving against the monthly plan, and the remaining stock.

    The port had collapsed every plan to "name — price", which is the
    one shape that cannot answer "why would I take the longer plan?".
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add_all(
            [
                ShopItem(id=1, name="👑 VIP (1 месяц)", price=1000, type="vip", stock=-1),
                ShopItem(id=2, name="👑 VIP (1 год)", price=9000, type="vip", stock=7),
            ]
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/vip_shop"))
    body = sent[0]["text"]
    # Curated blurb, with the perk numbers taken from DEFAULT_VIP.
    assert "365 дней" in body
    assert "−50% налога" in body
    # 9000/365 ≈ 24.7 per day vs 1000/30 ≈ 33.3 → ~26% cheaper.
    assert "Выгоднее месячного" in body
    assert "26%" in body
    # Limited batch surfaces its count; the unlimited plan must not
    # advertise a stock line at all.
    assert "Осталось: <b>7</b>" in body
    assert body.count("📦") == 1


async def test_vip_shop_english_card_has_no_russian_catalog_text(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """RR-2 #21: an EN user must never be shown the Russian catalog copy.

    The catalog columns are operator-written Russian; before the curated
    per-plan copy landed they were escaped straight into the card and the
    buy button, so ``/vip_shop`` in English read as a Russian menu.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            ShopItem(
                id=1,
                name="👑 VIP (3 месяца)",
                description="VIP статус на 90 дней",
                price=2500,
                type="vip",
                stock=-1,
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/vip_shop", language_code="en"))
    body = sent[0]["text"]
    assert "VIP for 3 months" in body
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body)


async def test_vip_shop_keeps_operator_copy_for_a_custom_russian_plan(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A non-canonical VIP row has no curated copy — RU users see the
    operator's own text rather than a generic blurb, and no saving badge
    is invented for a term we cannot compute."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            ShopItem(
                id=1,
                name="👑 VIP (2 недели)",
                description="Пробный VIP на две недели",
                price=400,
                type="vip",
                stock=-1,
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/vip_shop"))
    body = sent[0]["text"]
    assert "VIP (2 недели)" in body
    assert "Пробный VIP на две недели" in body
    assert "Выгоднее" not in body


async def test_vip_shop_windows_an_oversized_catalog(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list_by_type`` has no LIMIT and the row copy is operator text.

    Forty verbose hand-seeded plans is a card of tens of thousands of
    characters — one ``reply`` Telegram refuses with a 400, so the user
    gets nothing. The handler windows it, keeps the keyboard on the same
    slice, and says how many plans it left out.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add_all(
            [
                ShopItem(
                    id=index,
                    name=f"👑 VIP тариф {index} {'П' * 100}",
                    description="о" * 2000,
                    price=100 + index,
                    type="vip",
                    stock=-1,
                )
                for index in range(1, 41)
            ]
        )
        await session.commit()

    sent: list[dict[str, Any]] = []
    _capture_with_markup(bot, monkeypatch, sent)
    await dispatcher.feed_update(bot, _update("/vip_shop"))

    assert len(sent) == 1
    body = sent[0]["text"]
    assert parsed_length(body) <= TELEGRAM_TEXT_LIMIT
    markup = sent[0]["reply_markup"]
    assert markup is not None
    # Text and keyboard on the SAME window: a button for every plan the
    # body showed, and none for a plan it didn't.
    shown_ids = {
        ShopBuyPrompt.unpack(btn.callback_data).item_id
        for row in markup.inline_keyboard
        for btn in row
    }
    assert shown_ids == set(range(1, _MAX_PLANS + 1))
    assert len(markup.inline_keyboard) == _MAX_PLANS
    assert str(40 - _MAX_PLANS) in body
    assert "/shop" in body


async def test_vip_shop_html_escapes_plan_name(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Admin-supplied markup in a VIP row name must not render as HTML."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(ShopItem(id=1, name="<b>Pwn</b>", price=1, type="vip", stock=-1))
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/vip_shop"))
    body = sent[0]["text"]
    assert "&lt;b&gt;Pwn&lt;/b&gt;" in body
    assert "<b>Pwn</b>" not in body


async def test_vip_shop_buy_button_routes_through_shop_purchase(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A click on the VIP buy button reaches the shop router's prompt
    handler (shared ShopBuyPrompt callback) and renders the confirm card
    with the plan price + caller balance — proving /vip_shop reuses the
    existing atomic purchase path, not a new one.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(EconomyUser(user_id=555, balance=5000, language="ru"))
        session.add(ShopItem(id=7, name="👑 VIP (1 месяц)", price=1000, type="vip", stock=-1))
        await session.commit()

    sink = capture_callback_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_callback_update(ShopBuyPrompt(item_id=7).pack(), user_id=555),
    )
    assert result is not UNHANDLED
    # The shop router's prompt handler edited the message to a confirm
    # card surfacing the price and balance.
    edits = [e for e in sink if e["kind"] == "edit"]
    assert edits, "expected a confirm-card edit from the shop prompt handler"
    assert "1000" in edits[-1]["text"]


async def test_vip_shop_group_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/vip_shop`` is private-only — a group gets the refusal (#123)."""
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/vip_shop", chat_type="supergroup"))
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="vip_shop")


# --- /voice_stats (the real, ledger-backed flow) ---------------------------


async def test_voice_stats_zero_state_for_user_with_no_voice(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A user who never used /voice gets the friendly zero-state, NOT a
    wall of zeros and NOT silence."""
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/voice_stats"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "Статистика голосовых" in body
    assert "ни разу" in body
    # Zero-state must not render the counts card.
    assert "Всего озвучек" not in body


async def test_voice_stats_renders_netted_counts(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Three tts debits, one refunded, plus an unrelated row.

    Net successful voices = 3 debits − 1 refund = 2.
    Net coins = (10+10+10) − 10 = 20.
    The unrelated ``type='shop'`` row for the same user must NOT count.
    """
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    now = datetime(2026, 6, 5, 12, 0, 0)
    async with sessionmaker() as session:
        session.add_all(
            [
                # Three successful TTS debits (positive amount, from_id=user).
                Transaction(from_id=555, amount=10, type="tts", reason="vip_emoji_voice", date=now),
                Transaction(from_id=555, amount=10, type="tts", reason="vip_emoji_voice", date=now),
                Transaction(from_id=555, amount=10, type="tts", reason="vip_emoji_voice", date=now),
                # One of them was refunded (to_id=user, same magnitude).
                Transaction(to_id=555, amount=10, type="tts_refund", reason="synth fail", date=now),
                # Unrelated spend by the same user — must not be counted.
                Transaction(from_id=555, amount=99, type="shop", reason="hat", date=now),
                # Another user's voice — must not leak into 555's stats.
                Transaction(from_id=999, amount=10, type="tts", reason="vip_emoji_voice", date=now),
            ]
        )
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/voice_stats"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "Статистика голосовых" in body
    assert "Всего озвучек: <b>2</b>" in body
    assert "Потрачено монет: <b>20</b>" in body
    # The unrelated 99-coin shop spend did not inflate the coin total.
    assert "99" not in body


# ── I18N-4a: EN convergence — vip.py replies carry NO Cyrillic ──────────
#
# Pins the ru/en convergence fix for handlers/vip.py: an EN user
# (language_code="en") must never see Russian. The RU coverage above
# (default-ru user 555) still pins the byte-identical Russian copy.

import re as _re  # noqa: E402

_CYRILLIC = _re.compile(r"[А-Яа-яЁё]")


def _en_update(text: str, *, chat_type: str = "private") -> Update:
    return make_message_update(
        text, chat_type=chat_type, user_id=808, first_name="Bob", language_code="en"
    )


def _assert_no_cyrillic(text: str) -> None:
    assert text
    assert not _CYRILLIC.search(text), f"EN reply leaked Cyrillic: {text!r}"


async def test_vip_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/vip"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_vip_shop_empty_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/vip_shop"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_vip_shop_populated_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The perk header + per-plan buy line must be EN for an EN user."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(ShopItem(id=1, name="VIP 1mo", price=1000, type="vip", stock=-1))
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/vip_shop"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_voice_settings_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/voice_settings"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_voice_stats_zero_state_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/voice_stats"))
    _assert_no_cyrillic(sent[0]["text"])


async def test_voice_stats_counts_en_has_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The data-backed counts card must be EN for an EN user."""
    bot, dispatcher, registry = await make_wired(schemas=[EconomyBase])
    sessionmaker = registry.session(DBName.ECONOMY)
    now = datetime(2026, 6, 5, 12, 0, 0)
    async with sessionmaker() as session:
        session.add(Transaction(from_id=808, amount=10, type="tts", reason="voice", date=now))
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _en_update("/voice_stats"))
    _assert_no_cyrillic(sent[0]["text"])
