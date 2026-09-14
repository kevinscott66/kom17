"""End-to-end ``/ping`` + ``/botcheck`` (Stage 21).

What's worth proving:

* Both routes return a non-empty reply in any chat type.
* ``/ping`` calls ``get_me`` once; the reported RTT is from THAT call,
  not the broader handler runtime. If ``get_me`` raises, the reply
  still goes out with ``н/д`` instead of bubbling the exception (the
  whole point of ``/ping`` is to surface API issues, not relay them).
* ``/ping`` reports a non-negative lag even when the worker clock is
  ahead of ``message.date`` (clamp guard at heartbeat.py:_delivery_lag_ms).
* Every alias routes — ``/ping`` ``/kom_ping`` for ping;
  ``/botcheck`` ``/alive`` ``/kom_botcheck`` for botcheck.

Inline ``_capture`` stays (vs the shared ``capture_outgoing`` in
conftest.py): heartbeat needs to handle ``GetMe`` outbound calls and
count them — the shared helper covers only the common
``SendMessage`` / ``SendPhoto`` / ``SendDice`` shapes.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TelegramUser

from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update(
    text: str,
    *,
    chat_id: int = 7,
    chat_type: str = "private",
    language_code: str | None = None,
) -> Update:
    """File-local defaults: user 7 named ``Eve``, chat id 7. Delegates
    to the shared builder. ``language_code`` rides through so the
    ``LanguageMiddleware`` picks the caller's locale.
    """
    return make_message_update(
        text,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=7,
        first_name="Eve",
        language_code=language_code,
    )


def _capture(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[dict[str, Any]],
    *,
    get_me_raises: bool = False,
) -> dict[str, int]:
    """Patch the outbound session. Returns a counter dict so tests can
    assert ``get_me`` was called exactly once per ``/ping``.
    """
    counters = {"get_me": 0}

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "GetMe":
            counters["get_me"] += 1
            if get_me_raises:
                raise RuntimeError("simulated API outage")
            return TelegramUser(id=0, is_bot=True, first_name="bot")
        if name == "SendMessage":
            sink.append({"text": method.text})
            return Message(
                message_id=2,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)
    return counters


async def test_ping_reports_three_metrics(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    counters = _capture(bot, monkeypatch, sent)
    result = await dispatcher.feed_update(bot, _update("/ping"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "Понг" in body
    assert "Доставка апдейта" in body
    assert "RTT к Telegram API" in body
    assert "Обработка команды" in body
    # Exactly one get_me per /ping — not zero (would mean we report a
    # stale cached number), not two (would mean we re-check after the
    # initial call and the user sees the wrong RTT).
    assert counters["get_me"] == 1


async def test_ping_falls_back_when_api_unreachable(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole point of /ping is diagnosing outages — it must NOT raise
    when the very API call it's testing fails.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, get_me_raises=True)
    await dispatcher.feed_update(bot, _update("/ping"))
    body = sent[0]["text"]
    assert "н/д" in body
    assert "Понг" in body  # reply still sent


async def test_ping_lag_is_non_negative(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``message.date`` here is the year-2023 epoch in the fixture, so
    the delivery-lag number is huge — but never negative. A negative
    number would mean we forgot the ``max(0, ...)`` clamp at
    heartbeat.py:_delivery_lag_ms.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    await dispatcher.feed_update(bot, _update("/ping"))
    body = sent[0]["text"]
    assert "-" not in body.split("Доставка апдейта:")[1].split("ms")[0]


@pytest.mark.parametrize("alias", ["/ping", "/kom_ping"])
async def test_ping_aliases(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED
    assert sent


@pytest.mark.parametrize("alias", ["/botcheck", "/alive", "/kom_botcheck"])
async def test_botcheck_aliases(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED
    assert "На месте" in sent[0]["text"]


async def test_ping_card_answers_in_the_callers_language(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/ping`` is the first command an operator tries when something
    feels off — an English-speaking operator staring at a Russian card
    can't tell which of the three numbers is the bad one. The labels
    move to copy; the numbers stay where they were.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    await dispatcher.feed_update(bot, _update("/ping", language_code="en"))
    body = sent[0]["text"]
    assert "Pong" in body
    assert "Update delivery" in body
    assert "RTT to Telegram API" in body
    assert "Command processing" in body
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body), body


async def test_ping_api_fallback_is_translated(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``get_me``-failed branch renders its own literal, so it can
    leak Russian even when the surrounding card is English. Separate
    test because it is a separate substitution.
    """
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, get_me_raises=True)
    await dispatcher.feed_update(bot, _update("/ping", language_code="en"))
    body = sent[0]["text"]
    assert "n/a" in body
    assert "Pong" in body  # reply still sent
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body), body


async def test_botcheck_answers_in_the_callers_language(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    await dispatcher.feed_update(bot, _update("/botcheck", language_code="en"))
    body = sent[0]["text"]
    assert "running normally" in body
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body), body


async def test_ping_also_works_in_groups(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heartbeats are unguarded by chat type — same as legacy."""
    bot, dispatcher, _ = await make_wired()
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    result = await dispatcher.feed_update(
        bot, _update("/ping", chat_type="supergroup", chat_id=-100)
    )
    assert result is not UNHANDLED
    assert "Понг" in sent[0]["text"]
