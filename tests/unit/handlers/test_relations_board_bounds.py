"""The ``/marriages`` and ``/relations`` boards must fit in one message.

Neither ``list_active`` had a LIMIT and both handlers rendered every row
the chat had into a single ``reply``. A marriage line carries two names
(Telegram allows 64 characters each) plus category, level, date and
duration — around two dozen couples the reply passes the 4096-character
ceiling, Telegram refuses it, and the chat sees nothing at all.

Truncated rather than paginated: both queries order by experience DESC,
so what survives is the top of the leaderboard, which is what the
command is for. The dropped remainder gets its own closing line.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message

from telegram_invite_bot.handlers import relations
from telegram_invite_bot.handlers.relations import _BOARD_MAX
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length

_TAG = re.compile(r"<[^>]+>")

# Longest first_name Telegram accepts, to price the worst realistic row.
_LONG_NAME = "Я" * 64


class _FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=-100, type="supergroup")
        self.from_user = SimpleNamespace(id=7, is_bot=False, first_name="Кто-то")
        self.sent: list[str] = []

    async def reply(self, text: str, **_kw: Any) -> None:
        self.sent.append(text)


class _FakeBoardRepo:
    """Stands in for either bond repo — both expose the same two methods."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.counted = 0

    async def list_active(self, chat_id: int, *, limit: int) -> list[Any]:
        assert chat_id == -100
        return [
            SimpleNamespace(
                user1_id=1000 + i,
                user2_id=2000 + i,
                user1_name=_LONG_NAME,
                user2_name=_LONG_NAME,
                # Descending, like the repos' ``experience DESC`` ordering.
                experience=1_000_000 - i,
                created_at=datetime(2024, 1, 1),
                extra_days=0,
            )
            for i in range(min(self._count, limit))
        ]

    async def count_active(self, chat_id: int) -> int:
        assert chat_id == -100
        self.counted += 1
        return self._count


def _plain(text: str) -> str:
    return html.unescape(_TAG.sub("", text))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "kwarg"),
    [
        (relations.handle_marriages, "marriages_repo"),
        (relations.handle_relations, "relationships_repo"),
    ],
)
async def test_a_huge_board_stays_inside_the_message_limit(handler: Any, kwarg: str) -> None:
    """500 bonds used to be a reply Telegram threw away with a 400."""
    message = _FakeMessage()
    repo = _FakeBoardRepo(500)

    await handler(cast("Message", message), **{kwarg: cast("Any", repo)}, lang="ru")

    text = message.sent[0]
    assert parsed_length(text) <= TELEGRAM_TEXT_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "kwarg"),
    [
        (relations.handle_marriages, "marriages_repo"),
        (relations.handle_relations, "relationships_repo"),
    ],
)
async def test_the_truncated_remainder_is_reported(handler: Any, kwarg: str) -> None:
    """Dropping rows silently reads as "this is the whole chat"."""
    message = _FakeMessage()
    repo = _FakeBoardRepo(500)

    await handler(cast("Message", message), **{kwarg: cast("Any", repo)}, lang="ru")

    assert str(500 - _BOARD_MAX) in _plain(message.sent[0]).splitlines()[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "kwarg"),
    [
        (relations.handle_marriages, "marriages_repo"),
        (relations.handle_relations, "relationships_repo"),
    ],
)
async def test_a_short_board_is_untouched_and_costs_no_count(handler: Any, kwarg: str) -> None:
    """A board shorter than the cap is provably complete, so the extra
    COUNT must not run — an ordinary chat pays nothing for the fix."""
    message = _FakeMessage()
    repo = _FakeBoardRepo(3)

    await handler(cast("Message", message), **{kwarg: cast("Any", repo)}, lang="ru")

    text = message.sent[0]
    assert "ещё" not in text
    assert repo.counted == 0
    # Header plus one line per pair, nothing dropped.
    assert len([ln for ln in text.splitlines() if ln.startswith(("1.", "2.", "3."))]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "kwarg", "key"),
    [
        (relations.handle_marriages, "marriages_repo", "h_marriages_empty"),
        (relations.handle_relations, "relationships_repo", "h_relations_empty"),
    ],
)
async def test_an_empty_board_answers_without_counting(handler: Any, kwarg: str, key: str) -> None:
    message = _FakeMessage()
    repo = _FakeBoardRepo(0)

    await handler(cast("Message", message), **{kwarg: cast("Any", repo)}, lang="ru")

    assert message.sent == [t(key, "ru")]
    assert repo.counted == 0


@pytest.mark.asyncio
async def test_a_board_exactly_at_the_cap_reports_nothing_hidden() -> None:
    """The boundary: the COUNT runs (the list came back full) but there
    is genuinely nothing beyond it, so no "и ещё 0" line."""
    message = _FakeMessage()
    repo = _FakeBoardRepo(_BOARD_MAX)

    await relations.handle_marriages(cast("Message", message), cast("Any", repo), lang="ru")

    assert repo.counted == 1
    assert "ещё" not in message.sent[0]
