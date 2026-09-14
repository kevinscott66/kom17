"""``/filter_list`` must fit inside Telegram's 4096-character ceiling.

The list used to go out as a single message. ``MAX_WORDS_PER_GROUP`` is
500, so this was not an abuse case: a couple hundred ordinary words
already pass 4096 and the reply came back a 400 the admin never saw —
and ``/filter_list`` is exactly where the /groupadmin Words panel sends
an admin to see the full list.

What these tests pin:

* every emitted page fits, measured on the PARSED text (entities and
  ``&amp;`` do not count toward Telegram's limit — measuring the raw
  HTML instead would silently under-fill every page);
* nothing is lost across the page boundaries in any list the ceiling
  actually permits;
* the page count stays bounded even for 500 maximum-length words, and
  the pathological case says so instead of dropping words silently;
* the handler actually SENDS every page — the pagination is worthless if
  the command still emits only the first one.
"""

from __future__ import annotations

import html
import re
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message

from telegram_invite_bot.handlers import wordfilter
from telegram_invite_bot.handlers.wordfilter import (
    MAX_WORD_LENGTH,
    MAX_WORDS_PER_GROUP,
    _list_pages,
)
from telegram_invite_bot.utils.render import PAGE_MAX, TELEGRAM_TEXT_LIMIT

_TAG = re.compile(r"<[^>]+>")


def _parsed_len(page: str) -> int:
    """Length Telegram actually measures: markup gone, entities collapsed.

    ``<code>`` becomes a message entity and contributes nothing, and
    ``&amp;`` is transmitted as one character. Asserting on ``len(page)``
    would test our own escaping instead of the limit that bites.
    """
    return len(html.unescape(_TAG.sub("", page)))


def _rendered_words(pages: list[str]) -> list[str]:
    """Every word rendered as a bullet, in order, across all pages."""
    out: list[str] = []
    for page in pages:
        for line in page.splitlines():
            if line.startswith("• "):
                out.append(html.unescape(_TAG.sub("", line)[2:]))
    return out


def test_short_list_still_fits_in_one_message() -> None:
    """The common case must not grow a second message."""
    words = [f"word{i}" for i in range(20)]

    pages = _list_pages(words, "ru")

    assert len(pages) == 1
    assert "20" in pages[0].splitlines()[0]
    assert _rendered_words(pages) == words


def test_a_list_past_the_limit_is_split_and_loses_nothing() -> None:
    """~300 ordinary words used to be a single 4600-character 400."""
    words = [f"badword{i:04d}" for i in range(300)]
    single = len(words) * (len("badword0000") + 3)
    assert single > TELEGRAM_TEXT_LIMIT, "fixture must exceed the ceiling"

    pages = _list_pages(words, "ru")

    assert len(pages) > 1
    assert all(_parsed_len(page) <= TELEGRAM_TEXT_LIMIT for page in pages)
    assert _rendered_words(pages) == words


def test_ampersand_words_are_budgeted_on_the_parsed_length() -> None:
    """``&`` escapes to five characters that Telegram counts as one.

    Budgeting on the escaped HTML would split this list into six times
    as many messages as it needs.
    """
    words = ["&" * 20 for _ in range(100)]

    pages = _list_pages(words, "ru")

    assert len(pages) == 1
    assert _parsed_len(pages[0]) <= TELEGRAM_TEXT_LIMIT
    assert len(pages[0]) > TELEGRAM_TEXT_LIMIT, "raw HTML must exceed it — that's the point"
    assert _rendered_words(pages) == words


def test_a_full_group_of_maximum_length_words_stays_bounded() -> None:
    """500 × 100 characters is 50 000 — bounded pages, and it says so."""
    words = [f"{'x' * (MAX_WORD_LENGTH - 4)}{i:04d}" for i in range(MAX_WORDS_PER_GROUP)]

    pages = _list_pages(words, "ru")

    assert len(pages) == PAGE_MAX
    assert all(_parsed_len(page) <= TELEGRAM_TEXT_LIMIT for page in pages)
    rendered = _rendered_words(pages)
    assert rendered == words[: len(rendered)]
    dropped = len(words) - len(rendered)
    assert dropped > 0
    assert str(dropped) in pages[-1].splitlines()[-1]


# ---------------------------------------------------------------------------
# handler wiring
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Records what the handler sent, in the order it sent it.

    ``reply`` and ``answer`` are recorded separately: the first page
    quotes the command, the continuations must not (a column of
    identical quote blocks is what a naive loop produces).
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


class _FakeWordRepo:
    def __init__(self, words: list[str]) -> None:
        self._words = words

    async def list(self, *, group_id: int) -> list[str]:  # noqa: A003 — repo API
        assert group_id == -100
        return self._words


@pytest.fixture
def _admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the two collaborators the list path only passes through.

    ``_require_admin`` is ``moderation``'s live-Telegram check and
    ``_resolve_lang`` a DB read; both are covered by their own suites and
    neither has anything to say about pagination.
    """

    async def _lang(*_: Any, **__: Any) -> str:
        return "ru"

    async def _ok(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(wordfilter, "_resolve_lang", _lang)
    monkeypatch.setattr(wordfilter, "_require_admin", _ok)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_admin")
async def test_handler_sends_every_page_not_just_the_first() -> None:
    """The split is pointless if the command drops the continuations."""
    words = [f"badword{i:04d}" for i in range(300)]
    message = _FakeMessage()

    await wordfilter.handle_filter_list(
        cast("Message", message),
        cast("Any", None),
        cast("Any", _FakeWordRepo(words)),
        cast("Any", None),
        cast("Any", None),
    )

    pages = _list_pages(words, "ru")
    assert len(pages) > 1, "fixture must paginate"
    assert message.replied == pages[:1]
    assert message.answered == pages[1:]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_admin")
async def test_handler_keeps_a_short_list_to_a_single_reply() -> None:
    """No follow-up message for a list that always fit."""
    message = _FakeMessage()

    await wordfilter.handle_filter_list(
        cast("Message", message),
        cast("Any", None),
        cast("Any", _FakeWordRepo(["spam", "scam"])),
        cast("Any", None),
        cast("Any", None),
    )

    assert len(message.replied) == 1
    assert message.answered == []
