"""``/rules`` must survive a rules blob written right up to its cap.

``_RULES_MAX_LEN`` counts Python code points; Telegram counts UTF-16
code units. An emoji outside the BMP is one of the former and two of the
latter, so a 4000-character rules card with an emoji per line measures
~4090 units — and the ``📜 Правила группы`` header pushes the send past
4096. Telegram answers 400 and the group sees nothing at all.
"""

from __future__ import annotations

import html
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message

from telegram_invite_bot.handlers import rules
from telegram_invite_bot.utils.render import (
    TELEGRAM_TEXT_LIMIT,
    parsed_length,
    utf16_length,
)


def _blob_at_cap(prefix: str = "") -> str:
    """4000 code points holding 90 astral characters — 4090 units.

    Under ``_RULES_MAX_LEN`` by the ruler ``/setrules`` uses, over
    ``4096 - header`` by the ruler Telegram uses: exactly the gap
    between the two.
    """
    blob = prefix + "\n".join(["🔥 " + "п" * 41 for _ in range(90)])
    blob += "п" * (rules._RULES_MAX_LEN - len(blob))
    assert len(blob) == rules._RULES_MAX_LEN
    assert utf16_length(blob) == 4090
    return blob


class _FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=-100, type="supergroup")
        self.from_user = SimpleNamespace(id=7, is_bot=False, first_name="Кто-то")
        self.text = "/rules"
        self.sent: list[str] = []

    async def answer(self, text: str, **_: Any) -> None:
        self.sent.append(text)


class _FakeUserService:
    async def touch(self, _user: Any) -> Any:
        return SimpleNamespace(user_id=7, language="ru")


@pytest.fixture
def _stored(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace the DB read — storage is not what is under test."""

    def _set(value: str | None) -> None:
        async def _fetch(_registry: Any, chat_id: int) -> str | None:
            assert chat_id == -100
            return value

        monkeypatch.setattr(rules, "_fetch_rules", _fetch)

    return _set


async def _run() -> _FakeMessage:
    message = _FakeMessage()
    await rules.handle_rules(
        cast("Message", message),
        cast("Any", _FakeUserService()),
        cast("Any", None),
    )
    return message


@pytest.mark.asyncio
async def test_an_emoji_heavy_card_at_the_cap_still_gets_delivered(_stored: Any) -> None:
    """Header + body is 4109 measured units; neither message may exceed 4096."""
    blob = _blob_at_cap()
    _stored(blob)

    message = await _run()

    assert message.sent, "the card must go out"
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in message.sent)
    assert html.unescape(message.sent[-1]) == blob, "no rule may be dropped"


@pytest.mark.asyncio
async def test_an_ordinary_card_stays_one_message(_stored: Any) -> None:
    """The everyday case must not grow a second message."""
    _stored("1. Не спамить\n2. Не ругаться")

    message = await _run()

    assert len(message.sent) == 1
    assert "Правила группы" in message.sent[0]
    assert "Не спамить" in message.sent[0]


@pytest.mark.asyncio
async def test_the_split_card_keeps_its_escaping(_stored: Any) -> None:
    """Splitting must not open a hole in the HTML escaping."""
    _stored(_blob_at_cap(prefix="<b>"))

    message = await _run()

    body = message.sent[-1]
    assert "<b>" not in body
    assert "&lt;b&gt;" in body
