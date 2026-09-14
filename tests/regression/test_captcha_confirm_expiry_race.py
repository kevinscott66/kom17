"""#2017: a press and the deadline must not both win.

``handle_captcha_confirm`` asks Telegram whether the live restriction is
really the captcha's own (:func:`_restriction_is_captchas`, #342/#671)
and only then pops the pending timer. The probe is a round trip, and
:func:`_expire_captcha` pops that same entry as *its* claim — so a press
landing within a round trip of the deadline found both claims granted:
the timer kicked the joiner while the press went on to restore
permissions and answer "passed" to someone who had just been removed,
leaving a ``moderation_log`` kick row against a user who did press the
button.

The ordering the two tests here pin is the one the module already
argued for and could not enforce with a single dict operation:

* the press claims the entry **before** the probe, so a deadline
  arriving mid-probe finds nothing to kick — and a refused press puts
  the still-running timer straight back, which is what
  ``tests/regression/test_captcha_probe_failure.py`` pins from the
  other side;
* a press arriving after the timer took its claim is acknowledged
  **silently**, because the only two things this bot could say — "you
  passed" or "a moderator restricted you" — are both false while a kick
  is in flight.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from telegram_invite_bot.handlers import group_events
from telegram_invite_bot.handlers.group_events import (
    _PENDING_CAPTCHA,
    _RECENT_ONBOARDS,
    _expire_captcha,
    _start_captcha,
    handle_captcha_confirm,
)
from telegram_invite_bot.keyboards.builders.captcha import CaptchaConfirm
from telegram_invite_bot.utils.telegram_kick import KickOutcome

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aiogram import Bot
    from aiogram.types import CallbackQuery

_CHAT = -100_4242
_USER = 606
_NOTICE_ID = 91

#: Nothing here reaches a database — the one write ``_expire_captcha``
#: makes is stubbed out below, for the same reason the sibling file
#: hands ``_start_captcha`` a bare object.
_REGISTRY = cast("Any", object())


@pytest.fixture(autouse=True)
def _clean_pending() -> Iterator[None]:
    _PENDING_CAPTCHA.clear()
    _RECENT_ONBOARDS.clear()
    yield
    for task in _PENDING_CAPTCHA.values():
        task.cancel()
    _PENDING_CAPTCHA.clear()
    _RECENT_ONBOARDS.clear()


class _FakeBot:
    """Duck-typed Bot whose member probe can be parked mid-flight."""

    id = 1

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.park_probe = False
        self.probing = asyncio.Event()
        self.release = asyncio.Event()

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.calls.append(("probe", (chat_id, user_id)))
        if self.park_probe:
            self.park_probe = False
            self.probing.set()
            # Bounded so a regression can never hang the suite.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.release.wait(), timeout=30.0)
        # "member" is the state a lifted-or-never-restricted joiner is
        # in, and the one the probe answers ``True`` for.
        return SimpleNamespace(status="member")

    async def restrict_chat_member(
        self, chat_id: int, user_id: int, *, permissions: Any, until_date: Any = None
    ) -> None:
        self.calls.append(("restrict", (chat_id, user_id)))

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.calls.append(("send", (chat_id, text)))
        return SimpleNamespace(message_id=_NOTICE_ID)

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int) -> None:
        self.calls.append(("edit", (chat_id, message_id, text)))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.calls.append(("delete", (chat_id, message_id)))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class _FakeCallback:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=_USER, first_name="Joiner", username=None)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=_CHAT), message_id=_NOTICE_ID)
        self.answers: list[tuple[Any, ...]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


def _joiner() -> Any:
    return SimpleNamespace(
        id=_USER, first_name="Joiner", username=None, language_code="ru", is_bot=False
    )


async def _press(bot: _FakeBot) -> _FakeCallback:
    callback = _FakeCallback()
    await handle_captcha_confirm(
        cast("CallbackQuery", callback),
        CaptchaConfirm(user_id=_USER),
        cast("Bot", bot),
        "ru",
    )
    return callback


@pytest.fixture
def kicks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record kicks instead of performing the ban+unban pair.

    ``_expire_captcha``'s audit write is stubbed with it: the row only
    exists when a kick landed, so recording the kick records the row.
    """
    recorded: list[tuple[int, int]] = []

    async def _kick(_bot: Any, chat_id: int, user_id: int) -> KickOutcome:
        recorded.append((chat_id, user_id))
        return KickOutcome.REMOVED

    async def _audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(group_events, "kick_member", _kick)
    monkeypatch.setattr(group_events, "_record_captcha_kick", _audit)
    return recorded


async def test_a_deadline_arriving_mid_probe_finds_the_entry_claimed(
    kicks: list[tuple[int, int]],
) -> None:
    """The press is already committed; the timer must stand down.

    Driven rather than raced: the press is frozen inside its probe, the
    deadline is then run to completion, and only afterwards is the press
    released. Unfixed, the pending entry is still sitting in the dict
    while the press waits on Telegram, so the timer claims it and kicks
    a joiner who did press the button.
    """
    bot = _FakeBot()
    await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    bot.park_probe = True

    press = asyncio.create_task(_press(bot))
    await bot.probing.wait()
    await _expire_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _USER, _NOTICE_ID)
    bot.release.set()
    callback = await press

    assert kicks == [], "the joiner was kicked while their press was in flight"
    assert "delete" not in bot.names(), "the notice was deleted under a press that succeeded"
    assert len(callback.answers) == 1
    assert callback.answers[0][1].get("show_alert") is not True


async def test_a_press_during_the_kick_is_not_told_it_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same instant, and the honest answer to it.

    Here the timer got there first and its kick is in flight. Nothing
    the bot could say is true — "you passed" least of all — so the press
    is acknowledged with no text, and the notice the timer is about to
    delete is the answer the joiner actually gets.
    """
    bot = _FakeBot()
    await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    kicking = asyncio.Event()
    release = asyncio.Event()

    async def _kick(_bot: Any, _chat_id: int, _user_id: int) -> KickOutcome:
        kicking.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(release.wait(), timeout=30.0)
        return KickOutcome.REMOVED

    async def _audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(group_events, "kick_member", _kick)
    monkeypatch.setattr(group_events, "_record_captcha_kick", _audit)

    expiry = asyncio.create_task(
        _expire_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _USER, _NOTICE_ID)
    )
    await kicking.wait()
    callback = await _press(bot)
    release.set()
    await expiry

    assert callback.answers == [((), {})], (
        f"a joiner being kicked was answered with copy: {callback.answers}"
    )
    assert "edit" not in bot.names(), "the notice was edited to 'passed' under a live kick"
