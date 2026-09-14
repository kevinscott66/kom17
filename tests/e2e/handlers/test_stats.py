"""End-to-end stats flows: dispatcher → MessageStatsMiddleware → repo.

Two surfaces share the router (RR-1 #5):

* ``/stats`` / ``/статистика`` → the **per-user** PNG card + caption,
  reply-target aware, bots refused, groups only (legacy
  ``cmd_user_stats`` parity).
* ``/group_stats`` → the chat-wide per-day totals (Stage 11 surface).

Validates that the router opens its own ``message_stats.db`` session per
update (the middleware is attached at the router scope, not the
dispatcher), reads from ``MessageCount``, and renders legacy parity copy.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from telegram_invite_bot.config.settings import StatsConfig
from telegram_invite_bot.db.models.base import MessageStatsBase
from telegram_invite_bot.db.models.message_stats import MessageCount
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import (
    assert_unknown_form_hint,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot, Dispatcher

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

# Pick a TZ with no DST around the seeded dates so the e2e doesn't get
# off-by-one when run on different host clocks. ``UTC`` is intentional:
# the handler reads ``datetime.now(tz).date()`` so we'd otherwise need
# to freeze time to make assertions deterministic. Instead we seed
# rows relative to ``today`` computed the same way.
_TZ = "UTC"


def _today() -> date:
    return datetime.now(ZoneInfo(_TZ)).date()


def _update(
    text: str,
    *,
    chat_id: int = -100,
    chat_type: str = "supergroup",
    user_id: int = 9,
    first_name: str = "Eve",
    **kw: Any,
) -> Update:
    """File-local defaults: supergroup ``-100`` titled ``T``, user 9
    named ``Eve``. Delegates to the shared builder; ``**kw`` forwards the
    reply-envelope knobs (``reply_to_user_id`` & friends) unchanged.

    ``user_id`` / ``first_name`` are spelled out rather than left to
    ``**kw`` so a test can impersonate the anonymous-admin sender without
    colliding with the defaults this helper already passes.
    """
    return make_message_update(
        text,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        first_name=first_name,
        **kw,
    )


async def _seed_chat(
    registry: EngineRegistry,
    *,
    chat_id: int,
    rows: list[tuple[int, date, int]],
) -> None:
    sessionmaker = registry.session(DBName.MESSAGE_STATS)
    async with sessionmaker() as session:
        for user_id, day, count in rows:
            session.add(
                MessageCount(
                    user_id=user_id,
                    chat_id=chat_id,
                    date=day.isoformat(),
                    count=count,
                    last_message=datetime(2024, 1, 1, 12, 0, 0),
                )
            )
        await session.commit()


async def _wired(make_wired: WiredFactory) -> tuple[Bot, Dispatcher, EngineRegistry]:
    return await make_wired(
        schemas=[MessageStatsBase],
        stats_config=StatsConfig(STATS_PERIOD_DAYS=7, STATS_TIMEZONE=_TZ),
    )


# --- Chat-wide ``/group_stats`` (Stage 11 surface, unchanged) ---------------


async def test_group_stats_renders_zero_message_card_on_empty_chat(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await _wired(make_wired)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/group_stats"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    # Legacy copy for the empty case — must be byte-identical so users
    # don't see the new pipeline through a copy difference.
    assert sent[0]["text"].startswith("За последние 7 дн. сообщений нет.")


async def test_group_stats_aggregates_across_users_in_chat(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await _wired(make_wired)
    sent = capture_outgoing(bot)

    today = _today()
    # Two users, three days — handler must sum (3+4)=7 today and 10 yesterday.
    await _seed_chat(
        registry,
        chat_id=-100,
        rows=[
            (1, today, 3),
            (2, today, 4),
            (1, today - timedelta(days=1), 10),
            # Out-of-window noise that must NOT appear in the card.
            (1, today - timedelta(days=30), 9999),
        ],
    )

    await dispatcher.feed_update(bot, _update("/group_stats"))
    body = sent[0]["text"]
    assert "Всего сообщений: <b>17</b>" in body
    assert f"• {today.isoformat()}: 7" in body
    assert f"• {(today - timedelta(days=1)).isoformat()}: 10" in body
    assert "9999" not in body


async def test_group_stats_answers_in_the_callers_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The whole card — empty line, header, day rows — was Russian
    literals. Both branches are checked because the empty state is its
    own early return that never reaches the paginator.
    """
    bot, dispatcher, registry = await _wired(make_wired)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/group_stats", language_code="en"))
    empty = sent[0]["text"]
    assert "No messages in the last 7 d." in empty
    assert not any("Ѐ" <= ch <= "ӿ" for ch in empty), empty

    today = _today()
    await _seed_chat(registry, chat_id=-100, rows=[(1, today, 3), (2, today, 4)])
    sent.clear()

    await dispatcher.feed_update(bot, _update("/group_stats", language_code="en"))
    body = sent[0]["text"]
    assert "Activity over 7 d." in body
    assert "Messages total: <b>7</b>" in body
    # The day rows still carry the data, not just translated chrome.
    assert f"• {today.isoformat()}: 7" in body
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body), body


async def test_group_stats_sends_every_page_of_a_year_long_window(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``STATS_PERIOD_DAYS`` is validated up to 365, and a year of day
    lines is ~7300 characters — one message Telegram refuses outright.
    The renderer pages it; this pins that the handler actually SENDS the
    tail instead of dropping everything after the first page.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[MessageStatsBase],
        stats_config=StatsConfig(STATS_PERIOD_DAYS=365, STATS_TIMEZONE=_TZ),
    )
    today = _today()
    days = [today - timedelta(days=offset) for offset in range(365)]
    await _seed_chat(registry, chat_id=-100, rows=[(1, day, 12345) for day in days])
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/group_stats"))

    assert len(sent) > 1
    body = "\n".join(item["text"] for item in sent)
    for day in (days[0], days[len(days) // 2], days[-1]):
        assert f"• {day.isoformat()}: 12345" in body


async def test_group_stats_in_private_says_where_the_numbers_live(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1253: ``/group_stats`` had no chat-type gate while ``/stats`` did.

    ``MessageActivityMiddleware`` records group traffic only, so
    ``chat_totals_by_date`` on a DM is guaranteed empty and the caller got
    the legacy empty-window line above — "there are no messages in the
    last 7 days", which reads as a broken counter rather than as the wrong
    place to ask. Nothing leaked either way; the answer was simply wrong
    about why it was empty.

    The refusal names ``/group_stats``. Reusing ``h_stats_group_only``
    would have been cheaper and would have told the caller to go type
    ``/stats`` — a different command from the one they typed.
    """
    bot, dispatcher, _ = await _wired(make_wired)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/group_stats", chat_id=9, chat_type="private"))

    assert len(sent) == 1
    assert sent[0]["kind"] == "text"
    assert "только в группах" in sent[0]["text"]
    assert "/group_stats" in sent[0]["text"]
    assert "/stats там" not in sent[0]["text"]


# --- Per-user ``/stats`` card (RR-1 #5) ------------------------------------


async def test_stats_renders_per_user_png_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/stats`` answers with a PNG whose caption carries the sender's
    own today/7d/30d/total figures — NOT the chat-wide list."""
    bot, dispatcher, registry = await _wired(make_wired)
    sent = capture_outgoing(bot)

    today = _today()
    await _seed_chat(
        registry,
        chat_id=-100,
        rows=[
            (9, today, 3),
            (9, today - timedelta(days=2), 5),
            (9, today - timedelta(days=20), 11),
            # All-time must exceed the 30d window, so a regression that
            # aliased "total" to ``count_for_days(30)`` can't pass.
            (9, today - timedelta(days=400), 100),
            # Another member's traffic must never leak into Eve's card.
            (2, today, 4444),
        ],
    )

    result = await dispatcher.feed_update(bot, _update("/stats"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert sent[0]["kind"] == "photo"
    caption = sent[0]["caption"]
    assert "Eve — активность в этой группе" in caption
    assert "Сегодня: <code>3</code>" in caption
    assert "За 7 дней: <code>8</code>" in caption  # 3 + 5
    assert "За 30 дней: <code>19</code>" in caption  # 3 + 5 + 11
    assert "Всего: <code>119</code>" in caption  # + the 400-day-old row
    assert "4444" not in caption


async def test_stats_does_not_leak_other_chats(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Every figure is scoped to ``(user_id, chat_id)`` — the same user's
    traffic in another group must not show up here."""
    bot, dispatcher, registry = await _wired(make_wired)
    sent = capture_outgoing(bot)

    today = _today()
    await _seed_chat(registry, chat_id=-100, rows=[(9, today, 3)])
    await _seed_chat(registry, chat_id=-200, rows=[(9, today, 500)])

    await dispatcher.feed_update(bot, _update("/stats"))
    caption = sent[0]["caption"]
    assert "Всего: <code>3</code>" in caption
    assert "500" not in caption


async def test_stats_caption_carries_densified_seven_day_breakdown(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The per-day table legacy only showed in its text fallback is
    promoted into the caption: exactly 7 ``MM-DD`` rows newest-first
    (quiet days explicitly zero) with bars, plus a best-day highlight."""
    bot, dispatcher, registry = await _wired(make_wired)
    sent = capture_outgoing(bot)

    today = _today()
    await _seed_chat(
        registry,
        chat_id=-100,
        rows=[(9, today, 3), (9, today - timedelta(days=3), 12)],
    )

    await dispatcher.feed_update(bot, _update("/stats"))
    caption = sent[0]["caption"]
    table = caption.split("<pre>")[1].split("</pre>")[0]
    lines = table.splitlines()
    # Densified: a full week, newest first, gaps filled with zeros.
    assert len(lines) == 7
    assert lines[0].startswith(today.isoformat()[5:])
    assert lines[6].startswith((today - timedelta(days=6)).isoformat()[5:])
    assert lines[1].split()[1] == "0"  # yesterday had no rows
    assert "▇" in table  # proportional bars, not a bare number column
    # Best day is derived from the same window.
    assert "Лучший день" in caption
    assert (today - timedelta(days=3)).isoformat()[5:] in caption.split("</pre>")[1]


async def test_stats_empty_history_shows_no_data_marker(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A user with no recorded messages gets an inviting empty state, not
    a table of seven zeros."""
    bot, dispatcher, _ = await _wired(make_wired)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/stats"))
    caption = sent[0]["caption"]
    assert "пока пусто" in caption
    assert "<pre>" not in caption
    assert "Всего: <code>0</code>" in caption


async def test_stats_by_reply_targets_the_replied_user(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/stats`` in reply reads the *replied* user's counters (legacy
    ``message.reply_to_message.from_user``), not the sender's — and does
    not sum the two."""
    bot, dispatcher, registry = await _wired(make_wired)
    sent = capture_outgoing(bot)

    today = _today()
    await _seed_chat(
        registry,
        chat_id=-100,
        rows=[(9, today, 3), (77, today, 42)],
    )

    await dispatcher.feed_update(
        bot,
        _update("/stats", reply_to_user_id=77, reply_to_first_name="Bob"),
    )
    caption = sent[0]["caption"]
    assert "Bob — активность в этой группе" in caption
    assert "Всего: <code>42</code>" in caption
    assert "Eve" not in caption
    assert "<code>45</code>" not in caption  # never the sum of both


async def test_stats_refuses_bot_targets(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Replying to a bot is refused with the legacy line — no card, no
    misleading zeroed counters."""
    bot, dispatcher, _ = await _wired(make_wired)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        _update("/stats", reply_to_user_id=1000, reply_to_is_bot=True),
    )
    assert len(sent) == 1
    assert sent[0]["kind"] == "text"
    assert "Боты статистику не копят" in sent[0]["text"]


async def test_stats_in_private_explains_groups_only(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Counters only exist for groups (``MessageActivityMiddleware``
    records nothing in DMs), so a private ``/stats`` must explain that
    rather than render a full-size card of guaranteed zeros."""
    bot, dispatcher, _ = await _wired(make_wired)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/stats", chat_id=9, chat_type="private"))
    assert len(sent) == 1
    assert sent[0]["kind"] == "text"
    assert "только в группах" in sent[0]["text"]


async def test_stats_name_is_html_escaped(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A display name carrying markup must land escaped — the bot sends
    with ``parse_mode=HTML`` (formatting / phishing injection seam)."""
    bot, dispatcher, _ = await _wired(make_wired)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/stats",
            chat_id=-100,
            chat_type="supergroup",
            user_id=9,
            first_name='<a href="http://evil">click</a>',
        ),
    )
    caption = sent[0]["caption"]
    assert "&lt;a href=" in caption
    assert '<a href="http://evil">' not in caption


async def test_stats_long_name_is_truncated_before_escaping(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The name cap runs on the raw string, so a slice can never cut an
    HTML entity in half and produce broken markup."""
    bot, dispatcher, _ = await _wired(make_wired)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/stats",
            chat_id=-100,
            chat_type="supergroup",
            user_id=9,
            first_name="<" * 60,
        ),
    )
    caption = sent[0]["caption"]
    assert caption.count("&lt;") == 48
    assert "&l;" not in caption and "&" not in caption.replace("&lt;", "")


async def test_stats_falls_back_to_text_when_png_fails(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A Pillow/font failure must degrade to the same numbers as text,
    never swallow the answer (legacy try/except parity)."""
    from telegram_invite_bot.handlers import stats as stats_module

    def _boom(*_args: Any, **_kwargs: Any) -> bytes:
        raise OSError("no font")

    monkeypatch.setattr(stats_module, "render_stats_card", _boom)

    bot, dispatcher, registry = await _wired(make_wired)
    sent = capture_outgoing(bot)
    await _seed_chat(registry, chat_id=-100, rows=[(9, _today(), 3)])

    await dispatcher.feed_update(bot, _update("/stats"))
    assert len(sent) == 1
    assert sent[0]["kind"] == "text"
    assert "Eve — активность в этой группе" in sent[0]["text"]
    assert "Сегодня: <code>3</code>" in sent[0]["text"]


async def test_stats_falls_back_to_text_when_send_photo_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A chat that refuses photos (media disabled for members, flood
    wait) must still get the numbers — the guard covers *delivery*, not
    just rasterisation."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendPhoto

    bot, dispatcher, registry = await _wired(make_wired)
    sent = capture_outgoing(bot)
    await _seed_chat(registry, chat_id=-100, rows=[(9, _today(), 3)])

    original = bot.__class__.__call__

    async def _refuse_photos(self: Any, method: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(method, SendPhoto):
            raise TelegramBadRequest(method=method, message="not enough rights to send photos")
        return await original(self, method, *args, **kwargs)

    monkeypatch.setattr(bot.__class__, "__call__", _refuse_photos)

    await dispatcher.feed_update(bot, _update("/stats"))
    assert [e["kind"] for e in sent] == ["text"]
    assert "Сегодня: <code>3</code>" in sent[0]["text"]


async def test_stats_does_not_repost_as_text_on_a_network_error(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: Any,
) -> None:
    """A timeout on the upload must NOT trigger the text fallback.

    ``TelegramNetworkError`` can be raised *after* Telegram accepted and
    delivered the photo — the response is what went missing, not the
    message. Falling back there would double-post the card. So only the
    400-class photo refusals are caught; this one is left to the global
    error middleware, and crucially the numbers are never re-sent.
    """
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendPhoto

    bot, dispatcher, registry = await _wired(make_wired)
    sent = capture_outgoing(bot)
    await _seed_chat(registry, chat_id=-100, rows=[(9, _today(), 3)])

    original = bot.__class__.__call__

    async def _timeout_on_photo(self: Any, method: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(method, SendPhoto):
            raise TelegramNetworkError(method=method, message="read timeout")
        return await original(self, method, *args, **kwargs)

    monkeypatch.setattr(bot.__class__, "__call__", _timeout_on_photo)

    await dispatcher.feed_update(bot, _update("/stats"))
    assert not any("Сегодня: <code>3</code>" in (e.get("text") or "") for e in sent)


async def test_stats_tells_anonymous_admins_why_not_bots(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """An admin posting anonymously arrives as ``GroupAnonymousBot``.

    They are self-targeting a bot id, so the generic "reply to a human"
    line would tell an actual human they are a bot. Same refusal (the
    shared id has no personal counters), different words.
    """
    bot, dispatcher, _ = await _wired(make_wired)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        _update("/stats", user_id=1087968824, sender_is_bot=True, first_name="Channel"),
    )
    assert len(sent) == 1
    assert "Ты пишешь анонимно" in sent[0]["text"]
    assert "Ответь на сообщение человека" not in sent[0]["text"]


async def test_stats_with_args_gets_the_unknown_form_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/stats 30`` — the windowed variant is still unported (#158).

    Still not answered with a 30-day card; now answered with a hint
    instead of with silence. The window argument was legacy's, and
    legacy is gone.
    """
    bot, dispatcher, _ = await _wired(make_wired)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/stats 30"))

    assert result is not UNHANDLED
    assert_unknown_form_hint(sent, command="stats")


async def test_stats_ru_alias_routes_to_the_user_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/статистика`` is an alias of ``/stats`` (per-user), while
    ``/group_stats`` keeps the chat-wide text card."""
    bot, dispatcher, _ = await _wired(make_wired)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/статистика"))
    assert result is not UNHANDLED
    assert sent[0]["kind"] == "photo"
    assert "активность в этой группе" in sent[0]["caption"]
