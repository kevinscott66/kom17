"""#2027: the captcha button must not lift a moderator's restriction.

:func:`group_events._restriction_is_captchas` decides whether the
restriction a captcha press is about to clear belongs to the captcha.
It used to decide by shape::

    if until is None:
        return True
    return until <= datetime.now(UTC)

— "no expiry, or an expiry already past, means ours". The premise was
true about *this bot's* own sanctions: ``/mute`` always computes a
deadline, antiflood always sends one, and the captcha's restrict passed
none. It was applied to every restriction Telegram reports.

A human moderator does not go through this bot. Telegram's own "Restrict
user" UI defaults to Forever, Telegram reports Forever as
``until_date = 0``, and aiogram surfaces that as the Unix epoch — which
is not ``None`` but is comfortably in the past. So a permanently muted
member could press the button on any still-rendered captcha notice, or
send the ``cap:<their own id>`` payload outright, and hand themselves
back ``_CAPTCHA_DEFAULT_PERMS``. The callback data carries the target id
and the handler only checks that the presser is that target, so no
notice had to exist and the captcha did not have to be switched on in
that group — the sanction simply evaporated.

The fix stops inferring ownership from shape. The captcha's mute now
carries an explicit ``until_date`` and the process records it in
:data:`_CAPTCHA_MUTE_UNTIL`; the probe lifts only a restriction whose
live expiry is the one it set. A moderator's Forever matches nothing,
and a moderator's *timed* mute landing mid-window overwrites the single
restriction row Telegram keeps per member, so its expiry stops matching
too — which is #671 closed exactly rather than argued about.

What the guard cannot assert here: that Telegram really reports epoch
for Forever. That is an API fact, not a code fact, and the fake below
encodes it the way the bug report did.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery

from telegram_invite_bot.handlers.group_events import (
    _CAPTCHA_MUTE_UNTIL,
    _PENDING_CAPTCHA,
    _RECENT_ONBOARDS,
    _restrict_for_captcha,
    _start_captcha,
    handle_captcha_confirm,
)
from telegram_invite_bot.keyboards.builders.captcha import CaptchaConfirm

_CHAT = -100931
_USER = 7331
_NOTICE_ID = 41
_TIMEOUT = 120

#: Epoch is what aiogram hands back for ``until_date = 0``.
_FOREVER = datetime(1970, 1, 1, tzinfo=UTC)

#: Nothing here touches a database — ``_start_captcha`` only arms the
#: timer and posts the notice, both served by the fake.
_REGISTRY = cast("Any", object())


@pytest.fixture(autouse=True)
def _clean_module_state() -> Any:
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
    """Telegram's one-restriction-row-per-member behaviour, and no more."""

    def __init__(self, *, restricted_until: datetime | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.restricted_until = restricted_until

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.calls.append(("probe", (chat_id, user_id)))
        if self.restricted_until is not None:
            return SimpleNamespace(status="restricted", until_date=self.restricted_until)
        return SimpleNamespace(status="member")

    async def restrict_chat_member(
        self, chat_id: int, user_id: int, *, permissions: Any, until_date: datetime | None = None
    ) -> None:
        self.calls.append(("restrict", (chat_id, user_id, permissions, until_date)))
        # The later call replaces the earlier deadline; it does not stack.
        self.restricted_until = until_date

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.calls.append(("send", (chat_id, text)))
        return SimpleNamespace(message_id=_NOTICE_ID)

    async def edit_message_text(self, text: str, *, chat_id: int, message_id: int) -> None:
        self.calls.append(("edit", (chat_id, message_id, text)))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class _FakeCallback:
    def __init__(self) -> None:
        self.from_user = SimpleNamespace(id=_USER, first_name="Spammer", username=None)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=_CHAT), message_id=_NOTICE_ID)
        self.answers: list[tuple[Any, ...]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))


def _joiner() -> Any:
    return SimpleNamespace(
        id=_USER, first_name="Spammer", username=None, language_code="ru", is_bot=False
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


def _lifts(bot: _FakeBot) -> list[Any]:
    """Restricts that HAND PERMISSIONS BACK, which is the damaging half."""
    return [c for c in bot.calls if c[0] == "restrict" and c[1][2].can_send_messages]


async def test_a_forever_restriction_survives_a_crafted_press() -> None:
    """The hole itself: no captcha ever ran here, and none had to.

    No pending timer, no notice, no captcha enabled in this group — just
    a permanently restricted member sending the callback payload that
    names themselves.
    """
    bot = _FakeBot(restricted_until=_FOREVER)

    callback = await _press(bot)

    assert _lifts(bot) == [], "the presser restored their own permissions"
    assert bot.restricted_until == _FOREVER
    assert "edit" not in bot.names()
    assert len(callback.answers) == 1
    assert callback.answers[0][1].get("show_alert") is True


async def test_a_forever_restriction_survives_a_press_inside_a_live_window() -> None:
    """And a live timer does not make it the captcha's either.

    A running episode says an episode is running, not that the
    restriction in front of the probe belongs to it — the conflation the
    old code made in the other direction, and the one a moderator's
    Forever landing mid-window would otherwise walk through.
    """
    bot = _FakeBot()
    assert await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), _TIMEOUT)
    # The moderator restricts them Forever while the window is open; the
    # single restriction row now holds the moderator's decision.
    bot.restricted_until = _FOREVER

    callback = await _press(bot)

    assert _lifts(bot) == []
    assert bot.restricted_until == _FOREVER
    assert callback.answers[0][1].get("show_alert") is True


async def test_a_timed_mute_inside_the_window_survives_too() -> None:
    """#671, now refused on ownership rather than on shape."""
    bot = _FakeBot()
    assert await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), _TIMEOUT)
    sanction = datetime.now(UTC) + timedelta(minutes=30)
    bot.restricted_until = sanction

    await _press(bot)

    assert _lifts(bot) == []
    assert bot.restricted_until == sanction


async def test_the_joiner_the_captcha_muted_is_still_let_through() -> None:
    """The control: refusing everything would be a worse bug than the one fixed."""
    bot = _FakeBot()
    assert await _start_captcha(cast("Bot", bot), _REGISTRY, _CHAT, _joiner(), _TIMEOUT)

    callback = await _press(bot)

    assert len(_lifts(bot)) == 1
    assert "edit" in bot.names()
    assert callback.answers[0][1].get("show_alert") is not True


async def test_the_captchas_own_mute_expires_on_its_own() -> None:
    """What replaces the fallback the fix gives up.

    The old probe lifted unconditionally when the timer was missing,
    because after a restart nothing else would ever free the joiner.
    That fallback is the hole. It is safe to drop only because the mute
    now carries its own deadline: Telegram releases the joiner a little
    after their window whether this process survives or not.
    """
    bot = _FakeBot()
    before = datetime.now(UTC)

    assert await _restrict_for_captcha(cast("Bot", bot), _CHAT, _joiner(), _TIMEOUT)

    assert bot.restricted_until is not None
    assert bot.restricted_until > before + timedelta(seconds=_TIMEOUT), (
        "the mute expires before the challenge does — a joiner reading the "
        "notice would be freed without answering it"
    )
    assert _CAPTCHA_MUTE_UNTIL[(_CHAT, _USER)] == bot.restricted_until


async def test_a_short_window_still_gets_a_usable_mute() -> None:
    """``captcha_timeout_sec`` floors at 10 (``handlers/modcfg.py:179``).

    Telegram treats an ``until_date`` less than 30 seconds out as
    permanent, so the shortest configurable window must not round down
    into "Forever" — which is now precisely the shape the probe refuses,
    and would strand the joiner it was meant to release.
    """
    bot = _FakeBot()
    before = datetime.now(UTC)

    assert await _restrict_for_captcha(cast("Bot", bot), _CHAT, _joiner(), 10)

    assert bot.restricted_until is not None
    assert bot.restricted_until >= before + timedelta(seconds=60)
