"""Regression pins for the outgoing-markup fallback.

The unit under test is the net that keeps a single stray angle bracket
from turning into a message the user never receives. Its whole value is
in *when it does not* fire — a fallback that swallows unrelated 400s
would hide real bugs — so the negative cases carry as much weight here
as the positive one.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage

from telegram_invite_bot.middlewares.api_parse_mode_fallback import (
    ParseModeFallbackMiddleware,
)


class _Recorder:
    """Fails the first call with ``error``, records every method seen."""

    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.seen: list[Any] = []

    async def __call__(self, bot: Any, method: Any) -> str:  # noqa: ANN401
        self.seen.append(method)
        if self.error is not None and len(self.seen) == 1:
            raise self.error
        return "ok"


async def test_parse_failure_is_retried_without_parse_mode() -> None:
    method = SendMessage(chat_id=1, text="<unset> is not a tag")
    rec = _Recorder(
        TelegramBadRequest(
            method=method,
            message='Bad Request: can\'t parse entities: Unsupported start tag "unset"',
        )
    )

    # ``Any``: the middleware's declared return type is
    # ``Response[TelegramType]``, but the stub downstream returns a
    # plain marker — the assertion is about *which* call answered,
    # not about the payload shape.
    result: Any = await ParseModeFallbackMiddleware()(rec, object(), method)  # type: ignore[arg-type]

    assert result == "ok"
    assert len(rec.seen) == 2, "the message must be resent exactly once"
    assert rec.seen[1].parse_mode is None
    assert rec.seen[1].text == method.text, "the body must survive the retry verbatim"


async def test_original_method_is_not_mutated() -> None:
    """The caller may still hold the object; hand it back untouched."""
    method = SendMessage(chat_id=1, text="<b>hi</b>")
    rec = _Recorder(TelegramBadRequest(method=method, message="Bad Request: can't parse entities"))

    await ParseModeFallbackMiddleware()(rec, object(), method)  # type: ignore[arg-type]

    assert method.parse_mode is not None


async def test_unrelated_bad_request_still_propagates() -> None:
    """A blocked user or a stale message id is not a markup problem."""
    method = SendMessage(chat_id=1, text="hi")
    rec = _Recorder(TelegramBadRequest(method=method, message="Bad Request: chat not found"))

    with pytest.raises(TelegramBadRequest, match="chat not found"):
        await ParseModeFallbackMiddleware()(rec, object(), method)  # type: ignore[arg-type]

    assert len(rec.seen) == 1, "no retry for a failure the fallback cannot fix"


async def test_method_without_parse_mode_propagates() -> None:
    """Nothing to switch off — retrying would fail identically."""
    method = AnswerCallbackQuery(callback_query_id="1", text="hi")
    rec = _Recorder(TelegramBadRequest(method=method, message="Bad Request: can't parse entities"))

    with pytest.raises(TelegramBadRequest):
        await ParseModeFallbackMiddleware()(rec, object(), method)  # type: ignore[arg-type]

    assert len(rec.seen) == 1


async def test_happy_path_does_not_touch_the_method() -> None:
    method = SendMessage(chat_id=1, text="hi")
    rec = _Recorder(None)

    result: Any = await ParseModeFallbackMiddleware()(rec, object(), method)  # type: ignore[arg-type]
    assert result == "ok"
    assert rec.seen == [method]


async def test_second_failure_is_not_retried_again() -> None:
    """One retry, not a loop: a still-failing plain send must surface."""

    class _AlwaysFails:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, bot: Any, method: Any) -> str:  # noqa: ANN401
            self.calls += 1
            raise TelegramBadRequest(method=method, message="Bad Request: can't parse entities")

    rec = _AlwaysFails()
    method = SendMessage(chat_id=1, text="<x>")

    with pytest.raises(TelegramBadRequest):
        await ParseModeFallbackMiddleware()(rec, object(), method)  # type: ignore[arg-type]

    assert rec.calls == 2
