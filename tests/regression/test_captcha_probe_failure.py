"""#1945: a probe that cannot answer must not answer "lift it".

:func:`group_events._restriction_is_captchas` asks Telegram whether the
restriction a captcha button is about to clear really is the captcha's
own. On any exception it used to return ``True`` — lift — and
:func:`handle_captcha_confirm` then wrote ``UNRESTRICTED_PERMS`` over
whatever restriction actually existed. That is the self-service unmute
#342 and #671 were filed against, handed back by a Bot API hiccup.

Its docstring defended the fail-open with one argument: a restart drops
:data:`_PENDING_CAPTCHA` while the notice and its button keep rendering,
so refusing would strand a joiner nothing else will ever free. True —
but only where the dict has no entry. With a live timer nobody is
stranded: a refused press leaves the timer alone (``handle_captcha_
confirm`` probes BEFORE the pop, on purpose), so the captcha finishes
its normal life and the joiner can press again once the API answers.
The code failed open across a strictly wider set of states than the
comment defended.

So the probe is now tri-state — lift / refuse / "could not read" — and
the caller words the refusal accordingly. Telling a joiner "a moderator
restricted you" when the truth is "we could not check" would stop them
retrying, which is exactly what makes refusing safe.

The fakes are local rather than imported from
``tests/unit/handlers/test_captcha.py``: this file needs a bot whose
probe RAISES, and adding that flag to the shared fake would edit a file
none of these assertions belong in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from telegram_invite_bot.handlers.group_events import (
    _CAPTCHA_MUTE_UNTIL,
    _PENDING_CAPTCHA,
    _RECENT_ONBOARDS,
    _restriction_is_captchas,
    _start_captcha,
    handle_captcha_confirm,
)
from telegram_invite_bot.keyboards.builders.captcha import CaptchaConfirm

_CHAT = -100777
_USER = 5151
_NOTICE_ID = 88

#: Nothing here reaches a database: ``_start_captcha`` only arms the
#: timer and sends the notice, both of which the fake bot serves.
_REGISTRY = cast("Any", object())


@pytest.fixture(autouse=True)
def _clean_pending() -> Any:
    _PENDING_CAPTCHA.clear()
    _CAPTCHA_MUTE_UNTIL.clear()
    _RECENT_ONBOARDS.clear()
    yield
    for task in _PENDING_CAPTCHA.values():
        task.cancel()
    _PENDING_CAPTCHA.clear()
    _CAPTCHA_MUTE_UNTIL.clear()
    _RECENT_ONBOARDS.clear()


class _FakeBot:
    """Duck-typed Bot whose member probe can fail on demand."""

    def __init__(self, *, probe_raises: bool, restricted_until: datetime | None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._probe_raises = probe_raises
        self._restricted_until = restricted_until

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.calls.append(("probe", (chat_id, user_id)))
        if self._probe_raises:
            raise TelegramBadRequest(method=cast("Any", None), message="bad gateway")
        if self._restricted_until is not None:
            return SimpleNamespace(status="restricted", until_date=self._restricted_until)
        return SimpleNamespace(status="member")

    async def restrict_chat_member(
        self, chat_id: int, user_id: int, *, permissions: Any, until_date: datetime | None = None
    ) -> None:
        self.calls.append(("restrict", (chat_id, user_id, permissions)))
        # Telegram keeps one restriction row per member, so a later
        # restrict overwrites the earlier deadline rather than stacking.
        # The probe's whole test since #2027 is whether that deadline is
        # still the one we set, so a fake that dropped it would make
        # every case here look like the captcha's own.
        self._restricted_until = until_date

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.calls.append(("send", (chat_id, text)))
        return SimpleNamespace(message_id=_NOTICE_ID)

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int) -> None:
        self.calls.append(("edit", (chat_id, message_id, text)))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class _FakeCallback:
    def __init__(self, presser_id: int) -> None:
        self.from_user = SimpleNamespace(id=presser_id, first_name="Joiner", username=None)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=_CHAT), message_id=_NOTICE_ID)
        self.answers: list[tuple[Any, ...]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


def _joiner() -> Any:
    return SimpleNamespace(
        id=_USER,
        first_name="Joiner",
        username=None,
        language_code="ru",
        is_bot=False,
    )


async def _press(bot: _FakeBot) -> _FakeCallback:
    callback = _FakeCallback(_USER)
    await handle_captcha_confirm(
        cast("CallbackQuery", callback),
        CaptchaConfirm(user_id=_USER),
        cast("Bot", bot),
        "ru",
    )
    return callback


async def test_a_failed_probe_with_a_live_timer_does_not_lift() -> None:
    """The regression: a moderator's mute survives the API hiccup.

    The mute was imposed inside the captcha window — #671's scenario —
    so the timer is still pending when the button is pressed.
    """
    bot = _FakeBot(probe_raises=False, restricted_until=None)
    await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    # The moderator's mute lands after the captcha armed and overwrites
    # its deadline — #671's scenario, and the order matters: set before
    # arming, the captcha's own restrict would simply replace it.
    bot._restricted_until = datetime.now(UTC) + timedelta(minutes=10)  # noqa: SLF001
    arming_restricts = [c for c in bot.calls if c[0] == "restrict"]
    bot._probe_raises = True  # noqa: SLF001 — the fake is this file's own

    callback = await _press(bot)

    assert [c for c in bot.calls if c[0] == "restrict"] == arming_restricts
    assert "edit" not in bot.names()
    assert len(callback.answers) == 1
    assert callback.answers[0][1].get("show_alert") is True


async def test_a_refused_press_leaves_the_timer_to_arbitrate() -> None:
    """Refusing is only safe because the captcha still finishes.

    The probe runs before the pop, so the pending kick is untouched and
    the joiner can press again once Telegram answers.
    """
    bot = _FakeBot(probe_raises=False, restricted_until=None)
    await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), 120)
    bot._probe_raises = True  # noqa: SLF001

    await _press(bot)

    assert (_CHAT, _USER) in _PENDING_CAPTCHA


async def test_the_refusal_does_not_blame_a_moderator() -> None:
    """A joiner told "a moderator did this" would not retry.

    Distinct copy is the point of the tri-state: the two refusals mean
    different things to the person reading the alert.
    """
    unreadable = _FakeBot(probe_raises=False, restricted_until=None)
    await _start_captcha(cast("Bot", unreadable), _REGISTRY, _CHAT, _joiner(), 120)
    unreadable._probe_raises = True  # noqa: SLF001
    unreadable_press = (await _press(unreadable)).answers[0]

    _PENDING_CAPTCHA.clear()
    sanctioned = _FakeBot(probe_raises=False, restricted_until=None)
    await _start_captcha(cast("Bot", sanctioned), _REGISTRY, _CHAT, _joiner(), 120)
    # Same as above: the mute has to arrive after the captcha's restrict
    # to be the deadline the probe reads.
    sanctioned._restricted_until = datetime.now(UTC) + timedelta(minutes=10)  # noqa: SLF001
    sanctioned_press = (await _press(sanctioned)).answers[0]

    # Both are refusals — an alert, not the quiet success ack — and they
    # say different things. Checking only that the strings differ would
    # pass on the old code, where the unreadable press was ACCEPTED and
    # the "difference" was just the success text.
    assert unreadable_press[1].get("show_alert") is True
    assert sanctioned_press[1].get("show_alert") is True
    assert unreadable_press[0][0] != sanctioned_press[0][0]


async def test_a_failed_probe_without_a_timer_still_lifts() -> None:
    """The control, and the case the fail-open was written for.

    A restart dropped the timer. Nothing else will ever free this user,
    so an unreadable probe must not hold them either.
    """
    bot = _FakeBot(probe_raises=True, restricted_until=None)

    callback = await _press(bot)

    assert "restrict" in bot.names()
    assert "edit" in bot.names()
    assert len(callback.answers) == 1
    assert callback.answers[0][1].get("show_alert") is not True


async def test_the_probe_reports_the_three_states_apart() -> None:
    """Directly, so the caller's branch has something to branch on.

    ``has_timer`` is the caller's answer, not the dict's, since #2017:
    the confirm handler takes its pending entry out *before* it probes,
    so a function reading :data:`_PENDING_CAPTCHA` here would see the
    caller's own claim as "nobody will free this user" and fail open on
    every press.
    """
    readable = _FakeBot(probe_raises=False, restricted_until=None)
    assert (
        await _restriction_is_captchas(cast("Bot", readable), _CHAT, _USER, has_timer=False) is True
    )

    sanctioned = _FakeBot(
        probe_raises=False, restricted_until=datetime.now(UTC) + timedelta(minutes=10)
    )
    assert (
        await _restriction_is_captchas(cast("Bot", sanctioned), _CHAT, _USER, has_timer=False)
        is False
    )

    failing = _FakeBot(probe_raises=True, restricted_until=None)
    assert (
        await _restriction_is_captchas(cast("Bot", failing), _CHAT, _USER, has_timer=True) is None
    )
