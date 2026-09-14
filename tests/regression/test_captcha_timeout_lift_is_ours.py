"""The captcha timeout may only lift a mute it placed itself (#2032).

One ending of the captcha timeout leaves the joiner in the chat: the one
where the removal never landed. The mute is still on, it has no timer
left to retry — the pending entry was popped before the kick — and #285
is the joiner silenced forever by a kick that never happened. The remedy
was to give the permissions back.

Unconditionally, though, which is the shape #2027 took out of the
confirm handler: ownership of a restriction inferred from the fact that
we are the ones looking at it. A moderator restricting a joiner during
the captcha window, in a chat where the bot has since lost the right to
ban, had their sanction lifted seconds later by a timer nobody could see.
Nothing had to be typed at the bot for it.

The probe that answers "is this restriction ours?" already exists —
:func:`_restriction_is_captchas`, matching the live ``until_date``
against the deadline this process chose. It could not be used here,
because :func:`_expire_captcha` dropped that deadline one line into the
act: by the time the lift ran, the only evidence of ownership was gone
and the probe could only have answered "not ours" for everyone,
stranding genuine joiners. So the two halves of this fix are inseparable
— each ending drops the deadline for itself, and the lift consults the
probe.

Reachability widened just before this landed: #2031 gave the kick a
membership probe whose own failure is also a ``FAILED``, so this path no
longer needs the ban to be refused outright.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.group_events import (
    _CAPTCHA_MUTE_UNTIL,
    _PENDING_CAPTCHA,
    _expire_captcha,
)

_CHAT = -1005550011
_USER = 6161
_BOT_ID = 1000
_NOTICE_ID = 99

#: The deadline this process would have chosen for its own captcha mute.
_OURS = datetime.now(UTC) + timedelta(seconds=120)
#: Telegram's "Forever", as aiogram surfaces ``until_date = 0``.
_FOREVER = datetime(1970, 1, 1, tzinfo=UTC)


class _FakeBot:
    """Duck-typed Bot whose ban always fails, so the lift path is taken.

    ``until`` is what the two probes read: the kick's membership check
    and the ownership check. ``None`` means the member is not restricted
    at all.
    """

    def __init__(self, *, until: datetime | None) -> None:
        self.id = _BOT_ID
        self.calls: list[str] = []
        self._until = until

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.calls.append("probe")
        status = ChatMemberStatus.RESTRICTED if self._until else ChatMemberStatus.MEMBER
        return SimpleNamespace(
            status=status, until_date=self._until, user=SimpleNamespace(id=user_id)
        )

    async def ban_chat_member(self, chat_id: int, user_id: int, **_kw: Any) -> None:
        self.calls.append("ban")
        raise TelegramBadRequest(method=cast("Any", None), message="not enough rights")

    async def unban_chat_member(self, chat_id: int, user_id: int, **_kw: Any) -> None:
        self.calls.append("unban")

    async def restrict_chat_member(self, chat_id: int, user_id: int, **_kw: Any) -> None:
        self.calls.append("restrict")

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.calls.append("delete")


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    _PENDING_CAPTCHA.clear()
    _CAPTCHA_MUTE_UNTIL.clear()
    yield
    _PENDING_CAPTCHA.clear()
    _CAPTCHA_MUTE_UNTIL.clear()


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'moderation.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(ModerationBase.metadata.create_all)
    reg = EngineRegistry(
        engines={DBName.MODERATION: engine},
        sessions={DBName.MODERATION: async_sessionmaker(engine, expire_on_commit=False)},
    )
    try:
        yield reg
    finally:
        await reg.dispose()


async def _expire(bot: _FakeBot, reg: EngineRegistry, *, ours: datetime | None = _OURS) -> None:
    """Arm one episode the way ``_arm_captcha`` would, then time it out."""
    _PENDING_CAPTCHA[(_CHAT, _USER)] = cast("Any", object())
    if ours is not None:
        _CAPTCHA_MUTE_UNTIL[(_CHAT, _USER)] = ours
    await _expire_captcha(cast("Any", bot), reg, _CHAT, _USER, _NOTICE_ID)


async def test_a_moderators_permanent_restriction_survives_the_timeout(
    registry: EngineRegistry,
) -> None:
    """Telegram's "Restrict → Forever", which is what a human moderator gets.

    The bot cannot ban here, so the joiner stays — and the restriction
    on them is the moderator's, not the captcha's.
    """
    bot = _FakeBot(until=_FOREVER)

    await _expire(bot, registry)

    assert "restrict" not in bot.calls


async def test_a_moderators_timed_mute_survives_it_too(
    registry: EngineRegistry,
) -> None:
    """#671's case: a mute landing on the joiner mid-window.

    It overwrites ours, so the live expiry is no longer the one we
    recorded — which is precisely how the probe tells the two apart.
    """
    bot = _FakeBot(until=datetime.now(UTC) + timedelta(hours=6))

    await _expire(bot, registry)

    assert "restrict" not in bot.calls


async def test_the_joiners_own_captcha_mute_is_still_lifted(
    registry: EngineRegistry,
) -> None:
    """The half that must not regress: #285 is the reason this lift exists.

    A joiner whose kick never landed is sitting muted with no timer left.
    Refusing them here — which is what gating the lift would do on its
    own, since the recorded deadline used to be dropped before the
    branch ran — would reinstate the permanent silence #285 closed.
    """
    bot = _FakeBot(until=_OURS)

    await _expire(bot, registry)

    assert "restrict" in bot.calls


async def test_an_unrestricted_joiner_is_not_refused(
    registry: EngineRegistry,
) -> None:
    """Nothing to own means nothing to protect; the lift is a no-op anyway."""
    bot = _FakeBot(until=None)

    await _expire(bot, registry)

    assert "restrict" in bot.calls


async def test_the_recorded_deadline_does_not_outlive_the_episode(
    registry: EngineRegistry,
) -> None:
    """Both endings drop it — a stale entry is a claim on a mute that is gone.

    Left behind, it would answer "ours" for whatever restriction next
    happens to carry that expiry, which is the evidence the probe rests
    on.
    """
    bot = _FakeBot(until=_FOREVER)

    await _expire(bot, registry)

    assert (_CHAT, _USER) not in _CAPTCHA_MUTE_UNTIL
