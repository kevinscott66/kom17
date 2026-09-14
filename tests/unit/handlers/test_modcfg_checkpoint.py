"""#1876: ``/modcfg key value`` commits the setting before it echoes it.

``GroupModConfigRepo.set_field`` is an UPSERT, so the write holds
``BEGIN IMMEDIATE`` on ``moderation.db``. The handler then makes TWO
Telegram round-trips — the short confirmation and the full config card
— and ``BaseSessionMiddleware`` only commits after the handler returns
(``middlewares/base.py:131-132``). Until this ticket both calls ran
under the writer lock, and a FloodWait on the first one rolled the
setting back: the admin saw nothing happen and nothing had.

``handlers.groupadmin`` already commits at the same point
(``groupadmin.py:1421``); this brings ``/modcfg`` in line.

Stubs, not a session: the contract under test is the ORDER of
``checkpoint()`` against the first outgoing message.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from telegram_invite_bot.handlers import modcfg as modcfg_mod

_ADMIN_ID = 4242
_GROUP_ID = -1001


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.chat = SimpleNamespace(id=_GROUP_ID, type="supergroup")
        self.from_user = SimpleNamespace(id=_ADMIN_ID, language_code="ru")
        self.text = text
        self.caption = None
        self.outgoing: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.outgoing.append(text)

    async def answer(self, text: str, **_kw: object) -> None:
        self.outgoing.append(text)


class _ConfigRepoStub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def set_field(self, **_kw: Any) -> object:  # noqa: ANN401
        self.calls.append("set_field")
        return object()


class _SettingsRepoStub:
    async def get_language(self, _uid: int) -> str:
        return "ru"


@pytest.fixture
def _no_admin_check(monkeypatch: pytest.MonkeyPatch) -> None:
    async def yes(*_a: Any, **_kw: Any) -> bool:  # noqa: ANN401
        return True

    monkeypatch.setattr(modcfg_mod, "_require_admin", yes)
    monkeypatch.setattr(modcfg_mod, "_render_config", lambda *_a, **_kw: "CARD")


async def _run(text: str) -> tuple[_FakeMessage, _ConfigRepoStub, list[int]]:
    message = _FakeMessage(text)
    repo = _ConfigRepoStub()
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.outgoing))

    await modcfg_mod.handle_modcfg(
        message,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        _SettingsRepoStub(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        checkpoint,  # type: ignore[arg-type]
    )
    return message, repo, fired


@pytest.mark.usefixtures("_no_admin_check")
async def test_the_setting_is_durable_before_either_round_trip() -> None:
    """Both messages go out after the commit, not under its lock.

    Drop the checkpoint and ``fired`` is ``[]`` — the confirmation and
    the echo then share the writer slot with every other group.
    """
    message, repo, fired = await _run("/modcfg antiflood on")
    assert repo.calls == ["set_field"]
    assert fired == [0]
    assert len(message.outgoing) == 2


@pytest.mark.usefixtures("_no_admin_check")
async def test_a_rejected_value_never_reaches_the_checkpoint() -> None:
    """Bad input is refused before ``set_field``, so no lock is taken
    and there is nothing to commit."""
    message, repo, fired = await _run("/modcfg antiflood maybe")
    assert repo.calls == []
    assert fired == []
    assert len(message.outgoing) == 1
