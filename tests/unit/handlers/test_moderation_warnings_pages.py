"""``/warnings`` must fit inside Telegram's 4096-character ceiling.

``list_warnings`` returns up to 20 rows and ``_extract_reason`` clamps
each reason at 256 characters, so a moderator inspecting a heavily
warned user could ask for ~5700 characters in one message. Telegram
answers 400 and the moderator sees nothing at all.

``max_warns`` is configurable up to 20 and auto-ban can be switched off,
so a full list of long-reason warnings is a group's configuration, not
an abuse case.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message

from telegram_invite_bot.handlers import moderation
from telegram_invite_bot.repositories.moderation_repo import WarningRow
from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length

_REASON_MAX = moderation._REASON_MAX_LENGTH


class _FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=-100, type="supergroup")
        self.from_user = SimpleNamespace(id=7, is_bot=False, first_name="Admin")
        self.reply_to_message = None
        self.text = "/warnings 42"
        self.replied: list[str] = []
        self.answered: list[str] = []

    async def reply(self, text: str, **_: Any) -> None:
        self.replied.append(text)

    async def answer(self, text: str, **_: Any) -> None:
        self.answered.append(text)


class _FakeModerationRepo:
    def __init__(self, rows: list[WarningRow]) -> None:
        self._rows = rows

    async def list_warnings(self, *, user_id: int, chat_id: int) -> list[WarningRow]:
        assert (user_id, chat_id) == (42, -100)
        return self._rows


class _FakeModConfigRepo:
    async def get_or_default(self, chat_id: int) -> Any:
        assert chat_id == -100
        return SimpleNamespace(max_warns=20)


def _rows(count: int, reason: str) -> list[WarningRow]:
    return [
        WarningRow(
            id=i + 1,
            reason=reason,
            admin_id=7,
            date=datetime(2026, 1, 1, tzinfo=UTC),
            expires=None,
        )
        for i in range(count)
    ]


@pytest.fixture
def _moderator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the gate and the target lookup — neither concerns pagination."""

    async def _lang(*_: Any, **__: Any) -> str:
        return "ru"

    async def _verdict(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(allowed=True)

    async def _target(*_: Any, **__: Any) -> tuple[int, str]:
        return 42, "Нарушитель"

    monkeypatch.setattr(moderation, "_resolve_lang", _lang)
    monkeypatch.setattr(moderation, "_require_moderation", _verdict)
    monkeypatch.setattr(moderation, "_resolve_target", _target)


async def _run(message: _FakeMessage, rows: list[WarningRow]) -> None:
    await moderation.handle_warnings(
        cast("Message", message),
        cast("Any", None),
        cast("Any", _FakeModerationRepo(rows)),
        cast("Any", None),
        cast("Any", None),
        cast("Any", _FakeModConfigRepo()),
        cast("Any", None),
        cast("Any", None),
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_moderator")
async def test_a_full_warning_list_is_split_and_loses_nothing() -> None:
    """20 warnings at the reason cap is ~5700 characters."""
    rows = _rows(20, "ы" * _REASON_MAX)
    message = _FakeMessage()

    await _run(message, rows)

    pages = message.replied + message.answered
    assert len(message.replied) == 1
    assert message.answered, "the continuations must be sent too"
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages)
    body = "\n".join(pages)
    for row in rows:
        assert f"#{row.id} " in body


@pytest.mark.asyncio
@pytest.mark.usefixtures("_moderator")
async def test_a_couple_of_warnings_stay_a_single_reply() -> None:
    """The everyday case must not grow a second message."""
    message = _FakeMessage()

    await _run(message, _rows(3, "спам"))

    assert len(message.replied) == 1
    assert message.answered == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_moderator")
async def test_the_reason_is_escaped_on_every_page() -> None:
    """Splitting must not open a hole in the HTML escaping."""
    message = _FakeMessage()

    await _run(message, _rows(20, "<b>" + "&" * (_REASON_MAX - 3)))

    for page in message.replied + message.answered:
        assert "<b>" not in page
        assert "&lt;b&gt;" in page
