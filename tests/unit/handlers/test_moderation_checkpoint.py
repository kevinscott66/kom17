"""#1964: the seven moderation commands that lose their audit row.

Every sanction here follows the same two steps: write the
``moderation_log`` row, then tell the admin what happened. The write is
a real ``BEGIN IMMEDIATE`` on ``moderation.db``; the reply is a Telegram
round-trip that can raise for entirely ordinary reasons (FloodWait, the
admin deleted the chat, the bot lost send rights between the sanction
and the confirmation).

When it does raise, ``middlewares/base.py`` rolls the per-update session
back and ``handlers/errors.py`` consumes the exception and returns
``True``, so the webhook still answers 200 and nothing retries. The
Telegram-side sanction stands — the ban, the mute, the pin are applied
by the API and no rollback reaches them — while the row that records who
did it and why is gone. ``/warn`` is the worst of the seven: the warning
*count itself* rolls back, so the user is banned at the threshold and
the DB says they were never warned.

Three siblings already got this right and are the reference: ``/unban``
(#1866), ``/unwarn`` (#1875) and ``/fine`` (#493). Each takes a
``Checkpoint`` and commits between the last write and the first
round-trip; ``tests/unit/handlers/test_fine_checkpoint.py`` pins that
placement the same way this module does.

Recording stubs, not a real session: the order of ``checkpoint()``
against ``message.reply`` is exactly what a fake records, and ORDER is
the whole invariant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.handlers import moderation as mod

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram import Bot
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.db.engines import EngineRegistry
    from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigRepo
    from telegram_invite_bot.repositories.moderation_repo import ModerationRepo
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo
    from telegram_invite_bot.services.rank_service import RankService

_DEV_ID = 123456789
_TARGET_ID = 555


class _FakeUser:
    def __init__(self, uid: int, *, is_bot: bool = False) -> None:
        self.id = uid
        self.is_bot = is_bot
        self.first_name = "Target" if uid == _TARGET_ID else "Dev"
        self.username = None
        self.language_code = "ru"


class _FakeReply:
    def __init__(self, uid: int) -> None:
        self.from_user = _FakeUser(uid)
        self.message_id = 42
        self.text = "спам"
        self.caption = None


class _FakeMessage:
    """Records replies instead of calling the Bot API."""

    def __init__(self, text: str) -> None:
        self.chat = type("_Chat", (), {"id": -100, "type": "supergroup"})()
        self.from_user = _FakeUser(_DEV_ID)
        self.sender_chat = None
        self.reply_to_message: _FakeReply | None = _FakeReply(_TARGET_ID)
        self.text = text
        self.caption = None
        self.entities = None
        self.caption_entities = None
        self.message_id = 7
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.replies.append(text)


class _MemberStub:
    """A plain, unprotected member — the target guard's happy path."""

    status = "member"

    def __init__(self) -> None:
        self.user = _FakeUser(_TARGET_ID)


class _BotStub:
    """Every sanction lands; only the recording order is under test."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_chat_member(self, _chat: int, _uid: int) -> _MemberStub:
        return _MemberStub()

    async def ban_chat_member(self, *_a: object, **_kw: object) -> None:
        self.calls.append("ban")

    async def unban_chat_member(self, *_a: object, **_kw: object) -> None:
        self.calls.append("unban")

    async def restrict_chat_member(self, *_a: object, **_kw: object) -> None:
        self.calls.append("restrict")

    async def pin_chat_message(self, *_a: object, **_kw: object) -> None:
        self.calls.append("pin")

    async def unpin_chat_message(self, *_a: object, **_kw: object) -> None:
        self.calls.append("unpin")


class _ModerationRepoStub:
    def __init__(self, *, warns_before: int = 0) -> None:
        self.calls: list[str] = []
        self._warns_before = warns_before

    async def record_action(self, **_kw: Any) -> None:  # noqa: ANN401
        self.calls.append("record_action")

    async def get_warning_count(self, **_kw: Any) -> int:  # noqa: ANN401
        return self._warns_before

    async def add_warning(self, **_kw: Any) -> tuple[int, int]:  # noqa: ANN401
        self.calls.append("add_warning")
        return (1, self._warns_before + 1)


class _GroupModConfigStub:
    async def get_or_default(self, _chat_id: int) -> Any:  # noqa: ANN401
        return type(
            "_Cfg",
            (),
            {
                "mute_minutes": 60,
                "max_warns": 3,
                "autoban_enabled": True,
                "warn_expire_days": 0,
            },
        )()


class _SettingsRepoStub:
    async def get_language(self, _uid: int) -> str:
        return "ru"


class _UsersRepoStub:
    async def get(self, _uid: int) -> None:
        return None


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=_DEV_ID),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )


async def _run(
    command: str,
    handler_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    warns_before: int = 0,
) -> tuple[_FakeMessage, _ModerationRepoStub, list[int]]:
    """Drive one handler and report where its checkpoint fired.

    ``fired`` holds ``len(message.replies)`` at each ``checkpoint()``
    call, so ``[0]`` means "committed before the admin was told" and
    ``[]`` means the handler never committed at all.
    """
    # ``/mute`` consults the inventory for a protection item; that read
    # needs a real registry and is beside the point here.
    monkeypatch.setattr(mod, "has_mute_protection", _no_mute_protection)

    message = _FakeMessage(command)
    moderation = _ModerationRepoStub(warns_before=warns_before)
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.replies))

    common = (
        cast("Message", message),
        cast("Bot", _BotStub()),
        cast("ModerationRepo", moderation),
    )
    users = cast("UsersRepo", _UsersRepoStub())
    settings_repo = cast("UserSettingsRepo", _SettingsRepoStub())
    cfg_repo = cast("GroupModConfigRepo", _GroupModConfigStub())
    ranks = cast("RankService", object())
    settings = _settings(tmp_path)
    cp = cast("Checkpoint", checkpoint)

    if handler_name == "mute":
        await mod.handle_mute(
            *common,
            users,
            settings_repo,
            cfg_repo,
            settings,
            ranks,
            cast("EngineRegistry", object()),
            cp,
        )
    elif handler_name == "warn":
        await mod.handle_warn(*common, users, settings_repo, cfg_repo, settings, ranks, cp)
    elif handler_name in {"pin", "unpin"}:
        handler = mod.handle_pin if handler_name == "pin" else mod.handle_unpin
        await handler(*common, settings_repo, settings, ranks, cp)
    else:
        handler = getattr(mod, f"handle_{handler_name}")
        await handler(*common, users, settings_repo, settings, ranks, cp)

    return message, moderation, fired


async def _no_mute_protection(_registry: object, _uid: int) -> bool:
    return False


@pytest.mark.parametrize(
    ("command", "handler_name", "write_call"),
    [
        ("/ban 1h спам", "ban", "record_action"),
        ("/kick спам", "kick", "record_action"),
        ("/mute 10m спам", "mute", "record_action"),
        ("/unmute", "unmute", "record_action"),
        # A first warning is below the threshold, so the only write is
        # the warning row itself — which is exactly the one whose loss
        # #1964 calls the worst case.
        ("/warn спам", "warn", "add_warning"),
        ("/pin", "pin", "record_action"),
        ("/unpin", "unpin", "record_action"),
    ],
)
async def test_the_audit_row_is_durable_before_the_confirmation(
    command: str,
    handler_name: str,
    write_call: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sanction landed on Telegram's side and the row that records
    it must survive a reply that raises.

    ``fired == [0]`` is the whole assertion: the commit happened, and it
    happened before the first round-trip. Remove the checkpoint from any
    of the seven and that becomes ``[]``.
    """
    message, moderation, fired = await _run(command, handler_name, tmp_path, monkeypatch)
    assert write_call in moderation.calls, moderation.calls
    assert len(message.replies) == 1, message.replies
    assert fired == [0]


async def test_the_auto_ban_row_is_durable_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warn that crosses the threshold writes twice, so it commits
    twice.

    The second write — the ``ban`` audit row for the automatic sanction —
    lands *after* the first checkpoint, on the far side of the
    ``ban_chat_member`` round-trip, and would otherwise ride out to the
    confirmation uncommitted like the six sanction commands did.
    ``[0, 0]`` says both commits happened and neither waited for the
    reply. ``_GroupModConfigStub`` puts the threshold at 3.
    """
    message, moderation, fired = await _run(
        "/warn спам", "warn", tmp_path, monkeypatch, warns_before=2
    )
    assert moderation.calls == ["add_warning", "record_action"]
    assert len(message.replies) == 1, message.replies
    assert fired == [0, 0]
