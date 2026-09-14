"""#1874: an RP verb commits its XP before it congratulates the pair.

``add_marriage_xp`` / ``add_relationship_xp`` and the joint-activity
row that follows are all guarded writes, so the bonds DB is locked from
the grant onwards. RP verbs are the commonest write a busy group makes
— every ``.обнять`` is one — and until this ticket the writer slot was
held across ``message.reply`` for all of them
(``middlewares/base.py:131-132`` commits only after the handler
returns). One FloodWait there queued every other couple behind it, and
a hard failure threw away XP the pair had earned.

The third checkpoint is the divorce race: ``add_marriage_xp`` returns
``None`` when the bond vanished between the read and the UPDATE, but
the guarded UPDATE still took ``BEGIN IMMEDIATE`` to say so, and
control then falls through to branches that speak.

Stubs, not a session: the contract is the ORDER of ``checkpoint()``
against ``message.reply``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from telegram_invite_bot.handlers import rp as rp_mod

_CHAT = -1001
_USER = 10
_PARTNER = 20


class _FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=_CHAT, type="supergroup")
        self.from_user = SimpleNamespace(id=_USER, first_name="Alice", is_bot=False)
        self.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=_PARTNER, first_name="Bob", is_bot=False)
        )
        self.text = ".обнять"
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.replies.append(text)


class _BondsStub:
    """Marriage / relationship shapes driven by the two flags."""

    def __init__(self, *, married: bool, marriage_xp: int | None, related: bool) -> None:
        self._married = married
        self._marriage_xp = marriage_xp
        self._related = related
        self.calls: list[str] = []

    async def get_marriage(self, _chat: int, _uid: int) -> object | None:
        self.calls.append("get_marriage")
        if not self._married:
            return None
        return SimpleNamespace(user1_id=_USER, user2_id=_PARTNER)

    async def add_marriage_xp(self, _chat: int, _uid: int, _xp: int) -> int | None:
        self.calls.append("add_marriage_xp")
        return self._marriage_xp

    async def log_marriage_activity(self, *_a: Any) -> None:  # noqa: ANN401
        self.calls.append("log_marriage_activity")

    async def get_relationship(self, _chat: int, _a: int, _b: int) -> object | None:
        self.calls.append("get_relationship")
        return SimpleNamespace(experience=10_000) if self._related else None

    def _rel_xp_to_level(self, _xp: int) -> int:
        return 11

    async def add_relationship_xp(self, _chat: int, _a: int, _b: int, _xp: int) -> int | None:
        self.calls.append("add_relationship_xp")
        return 42

    async def log_relationship_activity(self, *_a: Any) -> None:  # noqa: ANN401
        self.calls.append("log_relationship_activity")


@pytest.fixture
def _open_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module limiter is process-global; keep it out of the way."""
    monkeypatch.setattr(rp_mod._limiter, "allow", lambda *_a: True)  # noqa: SLF001


async def _run(bonds: _BondsStub) -> tuple[_FakeMessage, list[int]]:
    message = _FakeMessage()
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.replies))

    await rp_mod.handle_rp_action(
        message,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        bonds,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        "ru",
        None,  # type: ignore[arg-type]
        checkpoint,  # type: ignore[arg-type]
    )
    return message, fired


@pytest.mark.usefixtures("_open_limiter")
async def test_the_marriage_grant_is_durable_before_the_reply() -> None:
    """XP and the activity row land, then the couple is told."""
    bonds = _BondsStub(married=True, marriage_xp=110, related=False)
    message, fired = await _run(bonds)
    assert bonds.calls == ["get_marriage", "add_marriage_xp", "log_marriage_activity"]
    assert fired == [0]
    assert len(message.replies) == 1


@pytest.mark.usefixtures("_open_limiter")
async def test_the_relationship_grant_is_durable_before_the_reply() -> None:
    """Same contract on the unmarried-but-paired branch."""
    bonds = _BondsStub(married=False, marriage_xp=None, related=True)
    message, fired = await _run(bonds)
    assert bonds.calls == [
        "get_marriage",
        "get_relationship",
        "add_relationship_xp",
        "log_relationship_activity",
    ]
    assert fired == [0]
    assert len(message.replies) == 1


@pytest.mark.usefixtures("_open_limiter")
async def test_a_divorce_race_lets_go_before_falling_through() -> None:
    """``add_marriage_xp`` matched nothing but still took the lock, and
    the no-pair tail below speaks — so the lock is released first."""
    bonds = _BondsStub(married=True, marriage_xp=None, related=False)
    message, fired = await _run(bonds)
    assert bonds.calls == ["get_marriage", "add_marriage_xp", "get_relationship"]
    assert fired == [0]
    assert len(message.replies) == 1


@pytest.mark.usefixtures("_open_limiter")
async def test_no_pair_at_all_never_reaches_a_checkpoint() -> None:
    """Two SELECTs and a general card: nothing was written, nothing to
    commit."""
    bonds = _BondsStub(married=False, marriage_xp=None, related=False)
    message, fired = await _run(bonds)
    assert bonds.calls == ["get_marriage", "get_relationship"]
    assert fired == []
    assert len(message.replies) == 1
