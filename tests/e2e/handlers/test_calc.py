"""End-to-end ``/calc`` — safe arithmetic evaluator.

Coverage:

* All three legacy aliases route (``/calc``, ``/калькулятор``,
  ``/kom_calc``) — case-insensitive.
* Simple arithmetic returns the rendered result with the expression
  echoed back HTML-escaped.
* Comma-as-decimal (``2,5``) is normalised — Russian users.
* Integer-valued results render without trailing ``.0``.
* Bare ``/calc`` (no expression) returns the hint.
* Invalid syntax / disallowed constructs collapse to the hint (the
  whole point of ``safe_calc`` is to return ``None`` for all error
  paths).
* Group calls work — no group-feature gate (see handler docstring).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED

from telegram_invite_bot.db.models.base import UsersBase
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot
    from aiogram.types import Update

    from tests.e2e.handlers.conftest import WiredFactory


def _update(text: str, *, chat_type: str = "private", user_id: int = 7777) -> Update:
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Calc",
        language_code="ru",
    )


@pytest.mark.parametrize("alias", ["/calc", "/калькулятор", "/kom_calc", "/CALC"])
async def test_aliases_evaluate(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update(f"{alias} 2+2"))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "2+2" in body
    assert "4" in body
    # Integer result must not be rendered with trailing ``.0``.
    assert "4.0" not in body


async def test_bare_command_renders_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/calc"))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "Напиши пример" in body
    assert "/calc" in body


async def test_comma_decimal_normalised(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """RU users write ``2,5`` — ``safe_calc`` normalises before parsing."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/calc 2,5*2"))
    body = sent[-1]["text"]
    assert "5" in body


async def test_non_integer_result_keeps_fraction(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/calc 7/2"))
    body = sent[-1]["text"]
    assert "3.5" in body


async def test_invalid_expression_returns_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Disallowed constructs (names, calls) and pure garbage both
    collapse to the hint — see ``safe_calc`` security model.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/calc abc"))
    body = sent[-1]["text"]
    assert "Напиши пример" in body


async def test_oversize_expression_returns_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Inputs > MAX_CALC_EXPR_LENGTH collapse to the hint."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    huge = "1+" * 200 + "1"
    await dispatcher.feed_update(bot, _update(f"/calc {huge}"))
    body = sent[-1]["text"]
    assert "Напиши пример" in body


async def test_group_calc_works(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No group-feature gate — calc runs in groups too."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, _update("/calc 10*3", chat_type="supergroup", user_id=-7001)
    )
    assert result is not UNHANDLED
    assert "30" in sent[-1]["text"]
