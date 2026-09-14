"""A kick must not be able to leave the user banned — or muted (#269, #285).

Telegram has no "kick": it is a ban followed by an unban. Both the legacy
bot (``bot.py:9396-9397``) and the port put the two awaits in one ``try``
and treated the pair as atomic. It is not, and the asymmetry runs the
wrong way — the ban is the call that lands, the unban is the one that can
be lost. Three consequences, one per group of tests below:

* the unban was tried exactly once and the ban had no expiry, so a single
  lost round trip converted a kick into a permanent ban (#269.1);
* the captcha kick wrote nothing to ``moderation_log``, so ``/modlog``
  showed admins an empty history for the removals the bot performed by
  itself (#269.3);
* and when the *ban* was the call that failed, the captcha mute stayed
  on with no ``until_date`` and no timer left to retry — the pending
  entry is popped before the kick — so the joiner was silenced forever
  by a kick that never happened (#285).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.core.moderation_reasons import CAPTCHA_FAILED
from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.db.models.moderation import ModerationLog
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import group_events
from telegram_invite_bot.handlers.group_events import _PENDING_CAPTCHA, _expire_captcha
from telegram_invite_bot.utils.telegram_kick import (
    _BAN_SECONDS,
    KickOutcome,
    kick_member,
)

_CHAT = -1005550001
_USER = 4242
_BOT_ID = 1000
_NOTICE_ID = 77


class _FakeBot:
    """Duck-typed Bot recording the kick-relevant calls with their kwargs.

    The kwargs are the point: ``until_date`` on the ban and
    ``only_if_banned`` on the unban are the two arguments that make the
    difference between this and the pair it replaced, and a fake that
    dropped them would let the old shape pass.
    """

    def __init__(
        self,
        *,
        fail_ban: bool = False,
        fail_unbans: int = 0,
        status: str = ChatMemberStatus.MEMBER,
    ) -> None:
        self.id = _BOT_ID
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._fail_ban = fail_ban
        self._fail_unbans = fail_unbans
        self._status = status

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        # #2031: the membership probe the pair now opens with. Recorded
        # like the rest, so a test can say "only the probe ran".
        self.calls.append(("probe", (chat_id, user_id), {}))
        return SimpleNamespace(status=self._status, user=SimpleNamespace(id=user_id))

    async def ban_chat_member(self, chat_id: int, user_id: int, **kw: Any) -> None:
        self.calls.append(("ban", (chat_id, user_id), kw))
        if self._fail_ban:
            raise TelegramBadRequest(method=cast("Any", None), message="no rights")

    async def unban_chat_member(self, chat_id: int, user_id: int, **kw: Any) -> None:
        self.calls.append(("unban", (chat_id, user_id), kw))
        if self._fail_unbans > 0:
            self._fail_unbans -= 1
            raise TelegramBadRequest(method=cast("Any", None), message="flood")

    async def restrict_chat_member(
        self, chat_id: int, user_id: int, *, permissions: Any, until_date: Any = None
    ) -> None:
        self.calls.append(("restrict", (chat_id, user_id), {"perms": permissions}))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.calls.append(("delete", (chat_id, message_id), {}))

    def names(self) -> list[str]:
        return [name for name, _args, _kw in self.calls]

    def kwargs_of(self, name: str) -> list[dict[str, Any]]:
        return [kw for got, _args, kw in self.calls if got == name]


@pytest.fixture(autouse=True)
def _clean_pending() -> Any:
    _PENDING_CAPTCHA.clear()
    yield
    _PENDING_CAPTCHA.clear()


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


async def _log_rows(registry: EngineRegistry) -> list[ModerationLog]:
    async with registry.session(DBName.MODERATION)() as session:
        result = await session.execute(select(ModerationLog))
        return list(result.scalars().all())


def _arm(chat_id: int = _CHAT, user_id: int = _USER) -> None:
    """Seed a pending captcha entry.

    ``_expire_captcha`` only checks that the popped value is not ``None``,
    so the timer object itself is irrelevant here — but a ``None`` value
    would read as "already confirmed" and skip the whole body.
    """
    _PENDING_CAPTCHA[(chat_id, user_id)] = cast("Any", object())


# ---------------------------------------------------------------------------
# kick_member — the ban/unban pair itself (#269.1)
# ---------------------------------------------------------------------------


async def test_a_clean_kick_bounds_the_ban_and_unbans_only_if_banned() -> None:
    bot = _FakeBot()
    outcome = await kick_member(cast("Any", bot), _CHAT, _USER, backoff_sec=0)

    assert outcome is KickOutcome.REMOVED
    assert bot.names() == ["probe", "ban", "unban"]
    # The expiry is what makes a lost unban survivable at all — without
    # it the ban outlives the process that was supposed to lift it.
    until = bot.kwargs_of("ban")[0]["until_date"]
    assert isinstance(until, timedelta)
    # only_if_banned keeps a retry from lifting a ban somebody else put
    # there between our two calls.
    assert bot.kwargs_of("unban")[0]["only_if_banned"] is True


def test_the_ban_window_is_inside_telegrams_temporary_band() -> None:
    """Below 30 s (or above 366 d) Telegram silently makes the ban permanent.

    That is not a rounding detail — it would turn the expiry into a
    no-op and quietly restore exactly the bug this module exists to
    close, with every other test here still green.
    """
    assert 30 < _BAN_SECONDS < 366 * 24 * 3600


async def test_the_unban_is_retried_until_it_lands() -> None:
    bot = _FakeBot(fail_unbans=2)
    outcome = await kick_member(cast("Any", bot), _CHAT, _USER, backoff_sec=0)

    assert outcome is KickOutcome.REMOVED
    assert bot.names().count("unban") == 3


async def test_an_exhausted_unban_reports_the_user_left_banned() -> None:
    bot = _FakeBot(fail_unbans=99)
    outcome = await kick_member(cast("Any", bot), _CHAT, _USER, attempts=3, backoff_sec=0)

    # Not REMOVED: the caller has to be able to tell "gone" from "gone
    # and still banned", which a bool cannot express.
    assert outcome is KickOutcome.LEFT_BANNED
    assert bot.names().count("unban") == 3


async def test_a_failed_ban_never_reaches_the_unban() -> None:
    bot = _FakeBot(fail_ban=True)
    outcome = await kick_member(cast("Any", bot), _CHAT, _USER, backoff_sec=0)

    assert outcome is KickOutcome.FAILED
    assert "unban" not in bot.names()


# ---------------------------------------------------------------------------
# The captcha timeout path (#269.3, #285)
# ---------------------------------------------------------------------------


async def test_a_captcha_kick_is_written_to_the_moderation_log(
    registry: EngineRegistry,
) -> None:
    bot = _FakeBot()
    _arm()
    await _expire_captcha(cast("Any", bot), registry, _CHAT, _USER, _NOTICE_ID)

    rows = await _log_rows(registry)
    assert len(rows) == 1
    # #253: the plain "kick" slug, so groupadmin's label map and kick
    # counter both see it; the captcha origin lives in ``details``.
    assert rows[0].action == "kick"
    assert rows[0].details == f"captcha_timeout:{KickOutcome.REMOVED.value}"
    # #1346: a language-neutral slug, not the Russian sentence this used
    # to store. Both renderers print ``reason`` verbatim, so the literal
    # showed up untranslated on an English admin's card.
    assert rows[0].reason == CAPTCHA_FAILED
    assert rows[0].user_id == _USER
    assert rows[0].chat_id == _CHAT
    # Bot-initiated moderation is stamped with the bot's own account,
    # the convention wordfilter's automod ban established.
    assert rows[0].admin_id == _BOT_ID


async def test_a_captcha_kick_that_left_the_user_banned_is_still_audited(
    registry: EngineRegistry,
) -> None:
    """The removal happened, so the row is owed regardless of the unban."""
    bot = _FakeBot(fail_unbans=99)
    _arm()
    await _expire_captcha(cast("Any", bot), registry, _CHAT, _USER, _NOTICE_ID)

    rows = await _log_rows(registry)
    assert len(rows) == 1
    assert rows[0].details == f"captcha_timeout:{KickOutcome.LEFT_BANNED.value}"


async def test_a_captcha_kick_whose_ban_failed_lifts_the_mute(
    registry: EngineRegistry,
) -> None:
    """#285: the joiner must not be left muted by a kick that never happened.

    The pending entry is popped before the kick is attempted, so there
    is no second timer coming. The mute ``_restrict_for_captcha`` took
    carries no ``until_date`` either — nothing in the process would ever
    lift it again.
    """
    bot = _FakeBot(fail_ban=True)
    _arm()
    await _expire_captcha(cast("Any", bot), registry, _CHAT, _USER, _NOTICE_ID)

    lifts = bot.kwargs_of("restrict")
    assert len(lifts) == 1
    assert lifts[0]["perms"].can_send_messages is True
    # Nothing was moderated, so nothing is claimed in the audit log.
    assert await _log_rows(registry) == []


async def test_an_already_confirmed_joiner_is_neither_kicked_nor_audited(
    registry: EngineRegistry,
) -> None:
    """No pending entry means the button was pressed inside the race window."""
    bot = _FakeBot()
    await _expire_captcha(cast("Any", bot), registry, _CHAT, _USER, _NOTICE_ID)

    assert bot.calls == []
    assert await _log_rows(registry) == []


def test_the_captcha_path_no_longer_calls_the_raw_ban_unban_pair() -> None:
    """Guard the rewire itself.

    ``_expire_captcha`` reaching for ``bot.ban_chat_member`` directly
    again would reintroduce the single-``try`` pair with every behaviour
    test above still passing through :func:`kick_member`'s own suite.
    """
    source = Path(group_events.__file__).read_text(encoding="utf-8")
    assert "ban_chat_member" not in source
    assert "kick_member(" in source
