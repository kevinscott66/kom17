"""What ``/filter_add`` and ``/filter_remove`` actually say back.

The four confirmation lines are the only signal an admin gets that the
filter changed, and each of the four is a different sentence: added /
already there / removed / was not there. Until this file the command
path had no behavioural coverage at all — the unit suite tested
:func:`_arg` parsing, the regression suite tested that the commands are
registered, and the e2e Words suite drives the /groupadmin PANEL, which
is a different handler. So a mutation that swapped "added" for "already
in the filter" passed the whole suite.

Each test pins the rendered text against :func:`t` itself rather than a
hardcoded string: the assertion is that the handler picked the right key
and filled its ``{word}`` slot, not that a translator never touches the
wording. The extra ``"{" not in text`` check is the failure mode that
does not raise — ``_SafeFormat.__missing__`` renders a forgotten
placeholder as a literal brace, so an unfilled slot reaches the admin
looking like a broken bot and looking like nothing at all to the logs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message

from telegram_invite_bot.handlers import wordfilter
from telegram_invite_bot.i18n import t

_CHAT = -100
_LANG = "ru"


class _FakeMessage:
    """Records the handler's reply."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.caption: str | None = None
        self.chat = SimpleNamespace(id=_CHAT)
        self.from_user = SimpleNamespace(id=7)
        self.replied: list[str] = []

    async def reply(self, text: str, **_: Any) -> None:
        self.replied.append(text)


class _FakeWordRepo:
    """In-memory stand-in with the real repo's return contract: ``add``
    and ``remove`` report whether they changed anything."""

    def __init__(self, words: list[str] | None = None) -> None:
        self.words = list(words or [])

    async def list(self, *, group_id: int) -> list[str]:  # noqa: A003 — repo API
        assert group_id == _CHAT
        return list(self.words)

    async def add(self, *, group_id: int, word: str, added_by: int | None) -> bool:
        assert group_id == _CHAT
        if word in self.words:
            return False
        self.words.append(word)
        return True

    async def remove(self, *, group_id: int, word: str) -> bool:
        assert group_id == _CHAT
        if word not in self.words:
            return False
        self.words.remove(word)
        return True


@pytest.fixture
def _admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the two collaborators these paths only pass through.

    ``_require_admin`` is ``moderation``'s live-Telegram check and
    ``_resolve_lang`` a DB read; both have their own suites and neither
    has anything to say about which confirmation comes back.
    """

    async def _lang(*_: Any, **__: Any) -> str:
        return _LANG

    async def _ok(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(wordfilter, "_resolve_lang", _lang)
    monkeypatch.setattr(wordfilter, "_require_admin", _ok)


async def _add(repo: _FakeWordRepo, text: str) -> _FakeMessage:
    message = _FakeMessage(text)
    await wordfilter.handle_filter_add(
        cast("Message", message),
        cast("Any", None),
        cast("Any", repo),
        cast("Any", None),
        cast("Any", None),
    )
    return message


async def _remove(repo: _FakeWordRepo, text: str) -> _FakeMessage:
    message = _FakeMessage(text)
    await wordfilter.handle_filter_remove(
        cast("Message", message),
        cast("Any", None),
        cast("Any", repo),
        cast("Any", None),
        cast("Any", None),
    )
    return message


@pytest.mark.asyncio
@pytest.mark.usefixtures("_admin")
async def test_add_confirms_with_the_word_it_stored() -> None:
    repo = _FakeWordRepo()

    message = await _add(repo, "/filter_add спам")

    assert repo.words == ["спам"]
    assert message.replied == [t("h_wf_added", _LANG, word="спам")]
    assert "{" not in message.replied[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_admin")
async def test_add_of_a_known_word_says_already_not_added() -> None:
    """The two branches differ only in the key; nothing else distinguishes
    a no-op from a change for the admin reading the chat."""
    repo = _FakeWordRepo(["спам"])

    message = await _add(repo, "/filter_add СПАМ")

    assert repo.words == ["спам"], "normalisation must not create a duplicate"
    assert message.replied == [t("h_wf_already", _LANG, word="спам")]
    assert "{" not in message.replied[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_admin")
async def test_remove_confirms_with_the_word_it_deleted() -> None:
    repo = _FakeWordRepo(["спам", "скам"])

    message = await _remove(repo, "/filter_remove спам")

    assert repo.words == ["скам"]
    assert message.replied == [t("h_wf_removed", _LANG, word="спам")]
    assert "{" not in message.replied[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_admin")
async def test_remove_of_an_unknown_word_says_not_found() -> None:
    repo = _FakeWordRepo(["скам"])

    message = await _remove(repo, "/filter_remove спам")

    assert repo.words == ["скам"]
    assert message.replied == [t("h_wf_not_found", _LANG, word="спам")]
    assert "{" not in message.replied[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_admin")
async def test_the_word_is_escaped_before_it_goes_into_the_html_reply() -> None:
    """The confirmation is parse_mode=HTML and the word is attacker-chosen:
    an admin can be talked into filtering anything, and the template wraps
    it in ``<b>``. An unescaped ``<`` there is a broken message at best."""
    repo = _FakeWordRepo()

    message = await _add(repo, "/filter_add <b>x</b>")

    assert repo.words == ["<b>x</b>"]
    assert message.replied == [t("h_wf_added", _LANG, word="&lt;b&gt;x&lt;/b&gt;")]
    assert "<b>x</b>" not in message.replied[0]
