"""Regression pins for the outgoing-length detector.

The guard exists to name a renderer that Telegram is about to reject.
Its value is entirely in accuracy: it must not stay quiet about a card
that will be lost (the failure that hid ``/admin_help`` and
``/admin_routes`` for the life of the deployment), and it must not cry
wolf about markup that fits, or the ERROR stops being worth reading.
"""

from __future__ import annotations

from typing import Any

from aiogram.methods import (
    AnswerCallbackQuery,
    SendMessage,
    SendPhoto,
    SendPoll,
)
from loguru import logger

from telegram_invite_bot.middlewares.api_length_guard import LengthGuardMiddleware
from telegram_invite_bot.utils.render import (
    TELEGRAM_CALLBACK_ANSWER_LIMIT,
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_POLL_QUESTION_LIMIT,
    TELEGRAM_TEXT_LIMIT,
)


class _Recorder:
    """Records every method that reached the transport."""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def __call__(self, bot: Any, method: Any) -> str:  # noqa: ANN401
        self.seen.append(method)
        return "ok"


class _Sink:
    """Collects ERROR records emitted while it is installed."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def __call__(self, message: Any) -> None:  # noqa: ANN401
        self.records.append(str(message))


async def _run(method: Any) -> tuple[_Recorder, list[str]]:  # noqa: ANN401
    rec = _Recorder()
    sink = _Sink()
    handler_id = logger.add(sink, level="ERROR", format="{message}")
    try:
        await LengthGuardMiddleware()(rec, object(), method)  # type: ignore[arg-type]
    finally:
        logger.remove(handler_id)
    return rec, sink.records


async def test_over_length_text_is_reported() -> None:
    method = SendMessage(chat_id=1, text="x" * (TELEGRAM_TEXT_LIMIT + 1))

    rec, records = await _run(method)

    assert len(records) == 1, records
    assert str(TELEGRAM_TEXT_LIMIT + 1) in records[0]
    assert "paginate_lines" in records[0], "the log must say what the fix is"
    # Reporting must not swallow the call: Telegram's own rejection is
    # still the caller's to see.
    assert len(rec.seen) == 1


async def test_markup_does_not_count_toward_the_limit() -> None:
    """A body that fits only after tag-stripping must not be reported.

    Under the bot-wide HTML default Telegram measures the parsed text,
    so counting raw characters would fire on cards that deliver fine —
    and a guard that cries wolf is a guard nobody reads.
    """
    body = "x" * (TELEGRAM_TEXT_LIMIT - 10)
    method = SendMessage(chat_id=1, text=f"<b>{body}</b>")

    _rec, records = await _run(method)

    assert records == []


async def test_plain_text_call_counts_the_tags() -> None:
    """With ``parse_mode=None`` those same tags are literal characters."""
    body = "x" * (TELEGRAM_TEXT_LIMIT - 3)
    method = SendMessage(chat_id=1, text=f"<b>{body}</b>", parse_mode=None)

    _rec, records = await _run(method)

    assert len(records) == 1, records


async def test_body_at_the_limit_is_not_reported() -> None:
    """The ceiling is inclusive — 4096 delivers."""
    method = SendMessage(chat_id=1, text="x" * TELEGRAM_TEXT_LIMIT)

    _rec, records = await _run(method)

    assert records == []


async def test_short_callback_toast_passes_through() -> None:
    method = AnswerCallbackQuery(callback_query_id="1", text="short")

    rec, records = await _run(method)

    assert records == []
    assert len(rec.seen) == 1


async def test_caption_is_measured_against_its_own_ceiling() -> None:
    """A caption gets 1024, not 4096.

    This is the case the first version of the guard missed: the body is
    comfortably under the message limit and still rejected, so checking
    it against 4096 reported nothing and the caption vanished anyway.
    """
    method = SendPhoto(chat_id=1, photo="f", caption="x" * (TELEGRAM_CAPTION_LIMIT + 1))

    _rec, records = await _run(method)

    assert len(records) == 1, records
    assert str(TELEGRAM_CAPTION_LIMIT) in records[0]


async def test_caption_at_its_ceiling_is_not_reported() -> None:
    method = SendPhoto(chat_id=1, photo="f", caption="x" * TELEGRAM_CAPTION_LIMIT)

    _rec, records = await _run(method)

    assert records == []


async def test_poll_question_is_measured_against_its_own_ceiling() -> None:
    method = SendPoll(
        chat_id=1,
        question="x" * (TELEGRAM_POLL_QUESTION_LIMIT + 1),
        options=["a", "b"],
    )

    _rec, records = await _run(method)

    assert len(records) == 1, records
    assert str(TELEGRAM_POLL_QUESTION_LIMIT) in records[0]


async def test_callback_toast_is_measured_against_two_hundred() -> None:
    """``text`` on a toast is not ``text`` on a message.

    Same field name, a twentieth of the room. The limit has to be
    looked up per method or this surface is guarded by a number twenty
    times too generous to ever fire.
    """
    method = AnswerCallbackQuery(
        callback_query_id="1", text="x" * (TELEGRAM_CALLBACK_ANSWER_LIMIT + 1)
    )

    _rec, records = await _run(method)

    assert len(records) == 1, records
    assert str(TELEGRAM_CALLBACK_ANSWER_LIMIT) in records[0]
    assert "AnswerCallbackQuery" in records[0], "the log must name the method"


async def test_callback_toast_counts_tags_literally() -> None:
    """A toast has no ``parse_mode`` — Telegram never parses it.

    So markup in a toast is not markup, it is characters, and stripping
    tags before measuring would under-count exactly the bodies most
    likely to be copied over from a card renderer.
    """
    body = "x" * (TELEGRAM_CALLBACK_ANSWER_LIMIT - 3)
    method = AnswerCallbackQuery(callback_query_id="1", text=f"<b>{body}</b>")

    _rec, records = await _run(method)

    assert len(records) == 1, records
