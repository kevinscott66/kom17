"""``/aliases`` must fit inside Telegram's 4096-character ceiling.

The list went out as one message. ``MAX_ALIASES_PER_GROUP`` is 100 and
both sides of a mapping may be 32 characters, so a group that simply
used the feature to its documented limit produced a ~7000-character
reply that Telegram refused — and the admin saw nothing at all, because
the 400 lands in the log, not in the chat.

Pinned here: the split happens, it loses no mapping, every page fits
when measured on the PARSED text, and the handler actually sends the
continuations instead of only the first page.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message

from telegram_invite_bot.handlers import group_aliases
from telegram_invite_bot.handlers.group_aliases import (
    MAX_ALIASES_PER_GROUP,
    MAX_WORD_LENGTH,
)
from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length


class _FakeMessage:
    """Records what went out, and by which method.

    ``reply`` quotes the command; the continuations use ``answer`` so the
    chat doesn't grow a column of identical quote blocks.
    """

    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=-100)
        self.from_user = SimpleNamespace(id=7)
        self.replied: list[str] = []
        self.answered: list[str] = []

    async def reply(self, text: str, **_: Any) -> None:
        self.replied.append(text)

    async def answer(self, text: str, **_: Any) -> None:
        self.answered.append(text)


class _FakeAliasRepo:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    async def list(self, *, group_id: int) -> list[tuple[str, str]]:  # noqa: A003 — repo API
        assert group_id == -100
        return self._rows


@pytest.fixture
def _admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the live-Telegram admin check the list path only passes through."""

    async def _ok(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(group_aliases, "_require_admin", _ok)


def _full_group() -> list[tuple[str, str]]:
    """A group at the documented ceiling: 100 mappings, 32 chars a side."""
    return [
        (f"{'ы' * (MAX_WORD_LENGTH - 3)}{i:03d}", f"{'c' * (MAX_WORD_LENGTH - 3)}{i:03d}")
        for i in range(MAX_ALIASES_PER_GROUP)
    ]


def _rendered_pairs(pages: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for page in pages:
        for line in page.splitlines():
            if not line.startswith("• "):
                continue
            word, _, target = line[2:].partition(" → ")
            out.append(
                (
                    word.removeprefix("<code>").removesuffix("</code>"),
                    target.removeprefix("<code>/").removesuffix("</code>"),
                )
            )
    return out


@pytest.mark.asyncio
@pytest.mark.usefixtures("_admin")
async def test_a_full_alias_list_is_split_and_loses_nothing() -> None:
    """100 × 32/32 is ~7000 characters — one message could never carry it."""
    rows = _full_group()
    message = _FakeMessage()

    await group_aliases.handle_aliases_list(
        cast("Message", message),
        cast("Any", None),
        cast("Any", _FakeAliasRepo(rows)),
        cast("Any", None),
        "ru",
    )

    pages = message.replied + message.answered
    assert len(message.replied) == 1
    assert message.answered, "the continuations must be sent too"
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages)
    assert _rendered_pairs(pages) == rows


@pytest.mark.asyncio
@pytest.mark.usefixtures("_admin")
async def test_a_short_alias_list_stays_a_single_reply() -> None:
    """The common case must not grow a second message."""
    message = _FakeMessage()

    await group_aliases.handle_aliases_list(
        cast("Message", message),
        cast("Any", None),
        cast("Any", _FakeAliasRepo([("прив", "start"), ("бал", "balance")])),
        cast("Any", None),
        "ru",
    )

    assert len(message.replied) == 1
    assert message.answered == []
    assert "2" in message.replied[0].splitlines()[0]
