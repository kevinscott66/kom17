"""An antiflood auto-mute must leave an audit row (#253.18).

The middleware silences a flooder for minutes at a time entirely on its
own, and until #253 the only trace it left was a log line on the server.
Nothing an admin can reach from Telegram knew the mute had happened:
``/groupadmin → Статистика`` counted zero mutes, and the last-five action
list skipped straight over it. A user asking "why can't I write?" could
not be answered from the bot's own records.

The row is written with the plain ``"mute"`` action on purpose — see
``AntifloodMiddleware._record_automute`` — because ``groupadmin``'s
counter sums a fixed list of slugs, so a bespoke ``"automute"`` would
have been recorded honestly and displayed nowhere.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.db.models.moderation import ModerationLog
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.antiflood import AntifloodMiddleware
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigView

_CHAT = -1004440001
_USER = 909
_BOT_ID = 5000


class _FakeBot:
    """Duck-typed bot with an ``id`` — the audit row needs an ``admin_id``."""

    def __init__(self, *, fail_restrict: bool = False) -> None:
        self.id = _BOT_ID
        self.restrict_calls: list[dict[str, Any]] = []
        self._fail = fail_restrict

    async def restrict_chat_member(self, **kwargs: Any) -> bool:
        if self._fail:
            raise RuntimeError("no rights")
        self.restrict_calls.append(kwargs)
        return True


class _RecordingMessage(Message):
    """Real ``Message`` (the middleware's ``isinstance`` gate) that swallows
    the notice instead of sending it."""

    answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> Any:  # type: ignore[override]
        self.answers.append(text)
        return None


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


def _cfg() -> GroupModConfigView:
    return GroupModConfigView(
        group_id=_CHAT,
        automod_enabled=True,
        profanity_enabled=True,
        max_warns=3,
        mute_minutes=1440,
        autoban_enabled=True,
        antiflood_enabled=True,
        flood_max_msgs=3,
        flood_window_sec=10,
        flood_mute_minutes=15,
    )


def _message() -> _RecordingMessage:
    msg = _RecordingMessage.model_construct(
        message_id=1,
        date=cast("Any", None),
        chat=cast("Any", SimpleNamespace(id=_CHAT, type="supergroup")),
        from_user=cast("Any", SimpleNamespace(id=_USER, is_bot=False, full_name="Flooder")),
        sender_chat=None,
        text="spam",
    )
    object.__setattr__(msg, "answers", [])
    return msg


def _fake_settings(*developers: int) -> Any:
    """Minimal ``Settings`` stand-in: the middleware reads one predicate."""
    return SimpleNamespace(bot=SimpleNamespace(is_developer=lambda user_id: user_id in developers))


def _middleware(registry: EngineRegistry) -> AntifloodMiddleware:
    mw = AntifloodMiddleware(registry=registry, settings=cast("Any", _fake_settings()))
    cfg = _cfg()

    async def _fake_config_for(group_id: int, now: float) -> GroupModConfigView:
        return cfg

    mw._config_for = _fake_config_for  # type: ignore[method-assign]

    async def _fake_is_exempt(bot: Any, chat_id: int, user_id: int) -> bool:
        return False

    mw._is_exempt_admin = _fake_is_exempt  # type: ignore[method-assign]
    return mw


async def _flood(mw: AntifloodMiddleware, msg: Message, bot: _FakeBot, times: int) -> None:
    async def _handler(ev: Any, data: dict[str, Any]) -> str:
        return "ok"

    for _ in range(times):
        assert await mw(_handler, msg, {"bot": cast("Any", bot), "lang": "ru"}) == "ok"


async def _rows(registry: EngineRegistry) -> list[ModerationLog]:
    async with registry.session(DBName.MODERATION)() as session:
        result = await session.execute(select(ModerationLog))
        return list(result.scalars().all())


async def test_automute_is_written_to_the_moderation_log(registry: EngineRegistry) -> None:
    mw = _middleware(registry)
    bot = _FakeBot()
    msg = _message()

    await _flood(mw, msg, bot, times=4)  # 4th exceeds flood_max_msgs=3

    assert len(bot.restrict_calls) == 1
    rows = await _rows(registry)
    assert len(rows) == 1
    row = rows[0]
    # The slug groupadmin's counter and label map both recognise.
    assert row.action == "mute"
    assert row.user_id == _USER
    assert row.chat_id == _CHAT
    # The bot acted on its own; the same convention wordfilter's automod
    # ban uses (handlers/wordfilter.py).
    assert row.admin_id == _BOT_ID
    assert row.reason == "Антифлуд"
    # What separates this from a hand-typed /mute lives in the details.
    assert row.details == "antiflood msgs>3/10s mute=15m"


async def test_one_row_per_mute_not_per_flood_message(registry: EngineRegistry) -> None:
    """The mute window suppresses re-mutes; the audit must not double-count.

    Otherwise a long burst would inflate ``/groupadmin → Статистика`` by
    one mute per message and the number would be worse than no number.
    """
    mw = _middleware(registry)
    bot = _FakeBot()
    msg = _message()

    await _flood(mw, msg, bot, times=12)

    assert len(bot.restrict_calls) == 1
    assert len(await _rows(registry)) == 1


async def test_no_row_when_the_restrict_itself_failed(registry: EngineRegistry) -> None:
    """A mute that never landed must not be logged as one.

    The restrict failure path marks the window and returns early — it has
    to, or the bot retries on every subsequent burst message in a chat
    where it has no rights — but an admin reading the log would then see
    a mute for a user who is demonstrably still talking.
    """
    mw = _middleware(registry)
    bot = _FakeBot(fail_restrict=True)
    msg = _message()

    await _flood(mw, msg, bot, times=6)

    assert bot.restrict_calls == []
    assert await _rows(registry) == []


async def test_a_broken_audit_write_costs_neither_the_mute_nor_the_notice(
    registry: EngineRegistry,
) -> None:
    """The audit is best-effort and must stay the cheapest thing to lose.

    ``bot`` is duck-typed throughout this middleware, so a caller with no
    ``id`` is a real possibility; before #253's fix that ``AttributeError``
    escaped and swallowed the user-facing notice along with it.
    """
    mw = _middleware(registry)
    bot = _FakeBot()
    del bot.id  # a bot object without the attribute the audit needs
    msg = _message()

    await _flood(mw, msg, bot, times=4)

    assert len(bot.restrict_calls) == 1
    assert len(msg.answers) == 1
    assert await _rows(registry) == []
