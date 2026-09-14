"""#1878: alias and word-filter writes are durable before the reply.

All four write sites here flush — ``GroupAliasRepo.upsert``
(group_aliases_repo.py:86) and ``WordFilterRepo.add``
(word_filter_repo.py:91) both call ``session.flush()``, and the two
``remove`` methods issue a guarded DELETE, which takes the write lock
even when it matches nothing. So in every case ``moderation.db`` is
under ``BEGIN IMMEDIATE`` while the handler makes its Telegram round
trip, and the session middleware is still free to roll the write back
if that round trip raises.

These are group-admin commands, so the cost of holding the lock is paid
by every other member of the group, not only by the admin who typed.

Stubs, not a session — what is under test is the ORDER of
``checkpoint()`` against the outgoing reply.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from telegram_invite_bot.handlers import group_aliases as alias_mod
from telegram_invite_bot.handlers import wordfilter as wf_mod

if TYPE_CHECKING:
    from collections.abc import Iterator

_CHAT = -100500
_ADMIN = 42


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.chat = SimpleNamespace(id=_CHAT, type="supergroup")
        self.from_user = SimpleNamespace(id=_ADMIN, username="admin", is_bot=False)
        self.text = text
        self.caption = None
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.replies.append(text)


class _AliasRepoStub:
    def __init__(self, *, removed: bool = True) -> None:
        self._removed = removed
        self.calls: list[str] = []

    async def mapping(self, *, group_id: int) -> dict[str, str]:  # noqa: ARG002
        self.calls.append("mapping")
        return {}

    async def upsert(self, **_kw: Any) -> bool:  # noqa: ANN401
        self.calls.append("upsert")
        return True

    async def remove(self, *, group_id: int, word: str) -> bool:  # noqa: ARG002
        self.calls.append("remove")
        return self._removed


class _WordFilterRepoStub:
    def __init__(self, *, count: int = 0, added: bool = True, removed: bool = True) -> None:
        self._count = count
        self._added = added
        self._removed = removed
        self.calls: list[str] = []

    async def list(self, *, group_id: int) -> list[str]:  # noqa: ARG002
        self.calls.append("list")
        return ["x"] * self._count

    async def add(self, **_kw: Any) -> bool:  # noqa: ANN401
        self.calls.append("add")
        return self._added

    async def remove(self, *, group_id: int, word: str) -> bool:  # noqa: ARG002
        self.calls.append("remove")
        return self._removed


def _recorder(message: _FakeMessage) -> tuple[Any, list[int]]:
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.replies))

    return checkpoint, fired


@pytest.fixture(autouse=True)
def _admin_everywhere(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Both modules gate on the live Telegram admin check; short it out."""

    async def _yes(*_a: object, **_kw: object) -> bool:
        return True

    async def _ru(*_a: object, **_kw: object) -> str:
        return "ru"

    monkeypatch.setattr(alias_mod, "_require_admin", _yes)
    monkeypatch.setattr(wf_mod, "_require_admin", _yes)
    monkeypatch.setattr(wf_mod, "_resolve_lang", _ru)
    yield


async def _run_alias(text: str, repo: _AliasRepoStub) -> tuple[_FakeMessage, list[int]]:
    message = _FakeMessage(text)
    checkpoint, fired = _recorder(message)
    await alias_mod.handle_alias(
        cast("Any", message),
        cast("Any", None),
        cast("Any", repo),
        cast("Any", None),
        "ru",
        checkpoint,
    )
    return message, fired


async def _run_wf(
    handler: Any,  # noqa: ANN401
    text: str,
    repo: _WordFilterRepoStub,
) -> tuple[_FakeMessage, list[int]]:
    message = _FakeMessage(text)
    checkpoint, fired = _recorder(message)
    await handler(
        cast("Any", message),
        cast("Any", None),
        cast("Any", repo),
        cast("Any", None),
        cast("Any", None),
        checkpoint,
    )
    return message, fired


async def test_an_added_alias_is_durable_before_the_confirmation() -> None:
    repo = _AliasRepoStub()
    message, fired = await _run_alias("/alias add привет start", repo)
    assert repo.calls == ["mapping", "upsert"]
    assert fired == [0]
    assert len(message.replies) == 1


async def test_an_alias_delete_commits_even_when_it_matched_nothing() -> None:
    """The guarded DELETE took the lock to report the miss; let it go."""
    repo = _AliasRepoStub(removed=False)
    message, fired = await _run_alias("/alias del привет", repo)
    assert repo.calls == ["remove"]
    assert fired == [0]
    assert len(message.replies) == 1


async def test_a_malformed_alias_never_reaches_the_checkpoint() -> None:
    """Refused on shape, before any DB call — nothing to commit."""
    repo = _AliasRepoStub()
    message, fired = await _run_alias("/alias add привет НЕ_КОМАНДА", repo)
    assert repo.calls == []
    assert fired == []
    assert len(message.replies) == 1


async def test_a_banned_word_is_durable_before_the_confirmation() -> None:
    repo = _WordFilterRepoStub()
    message, fired = await _run_wf(wf_mod.handle_filter_add, "/filter_add дурак", repo)
    assert repo.calls == ["list", "add"]
    assert fired == [0]
    assert len(message.replies) == 1


async def test_the_word_limit_refusal_never_reaches_the_checkpoint() -> None:
    """The ceiling is checked with a read; the refusal writes nothing."""
    repo = _WordFilterRepoStub(count=wf_mod.MAX_WORDS_PER_GROUP)
    message, fired = await _run_wf(wf_mod.handle_filter_add, "/filter_add дурак", repo)
    assert repo.calls == ["list"]
    assert fired == []
    assert len(message.replies) == 1


async def test_a_word_removal_commits_even_when_it_matched_nothing() -> None:
    repo = _WordFilterRepoStub(removed=False)
    message, fired = await _run_wf(wf_mod.handle_filter_remove, "/filter_remove дурак", repo)
    assert repo.calls == ["remove"]
    assert fired == [0]
    assert len(message.replies) == 1
