"""The no-reply ``/relationship`` list must stay inside Telegram's limits.

``list_relationships_for`` has no LIMIT, and every bond costs two text
lines plus an inline-keyboard row. Around 45 bonds the reply passes the
4096-character ceiling and Telegram refuses it outright — the user sees
nothing, not even a short list.

The list is truncated rather than paginated: the buttons belong to their
rows and cannot follow into a continuation message. The repo orders by
experience DESC, so what survives is the strongest bonds.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import InlineKeyboardMarkup, Message

from telegram_invite_bot.handlers import marriage
from telegram_invite_bot.handlers.marriage import _REL_LIST_MAX
from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length

_TAG = re.compile(r"<[^>]+>")


class _FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=-100, type="supergroup")
        self.from_user = SimpleNamespace(id=7, is_bot=False, first_name="Кто-то")
        self.sender_chat = None
        self.reply_to_message = None
        self.text = "/relationship"
        self.sent: list[tuple[str, InlineKeyboardMarkup | None]] = []

    async def reply(self, text: str, **kw: Any) -> None:
        self.sent.append((text, kw.get("reply_markup")))


class _FakeBondsRepo:
    """Only the four members the list path touches."""

    RELATIONSHIP_LEVEL_XP = marriage._RELATIONSHIP_LEVEL_XP

    def __init__(self, count: int) -> None:
        self._rels = [
            SimpleNamespace(
                user1_id=7,
                user2_id=1000 + i,
                # Descending, like the repo's ``experience DESC`` ordering.
                experience=1_000_000 - i,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for i in range(count)
        ]

    async def list_relationships_for(self, chat_id: int, user_id: int) -> list[Any]:
        assert (chat_id, user_id) == (-100, 7)
        return self._rels

    def _rel_xp_to_level(self, exp: int) -> int:
        return sum(1 for threshold in self.RELATIONSHIP_LEVEL_XP if exp >= threshold) - 1

    async def get_first_name(self, user_id: int) -> str:
        """Longest name Telegram allows, to price the worst realistic row."""
        return f"{'Я' * 60}{user_id}"


async def _run(count: int) -> _FakeMessage:
    message = _FakeMessage()
    await marriage.handle_relationship(
        cast("Message", message),
        cast("Any", _FakeBondsRepo(count)),
        "ru",
    )
    return message


@pytest.mark.asyncio
async def test_a_huge_bond_list_stays_inside_the_message_limit() -> None:
    """100 bonds used to be a ~9000-character reply Telegram threw away."""
    message = await _run(100)

    text, markup = message.sent[0]
    assert parsed_length(text) <= TELEGRAM_TEXT_LIMIT
    assert markup is not None
    assert len(markup.inline_keyboard) == _REL_LIST_MAX


@pytest.mark.asyncio
async def test_the_truncated_remainder_is_reported() -> None:
    """Dropping rows silently would read as "these are all my bonds"."""
    message = await _run(100)

    text, _ = message.sent[0]
    assert str(100 - _REL_LIST_MAX) in html.unescape(_TAG.sub("", text)).splitlines()[-1]


@pytest.mark.asyncio
async def test_a_list_within_the_cap_is_untouched() -> None:
    """No "and N more" line, no dropped rows, for an ordinary user."""
    message = await _run(3)

    text, markup = message.sent[0]
    assert markup is not None
    assert len(markup.inline_keyboard) == 3
    assert "ещё" not in text
