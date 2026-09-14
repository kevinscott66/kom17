"""#1879: ``/fine`` lets go of the economy lock before it refuses.

``EconomyRepo.debit`` is a guarded ``UPDATE`` (``WHERE balance >= …``),
so it opens ``BEGIN IMMEDIATE`` on ``economy.db`` whether or not it
matches a row (``db/engines.py`` registers the listener on the
statement, not on the row count). The ``updated is None`` branch is
therefore a refusal that still holds the single writer slot, and the
``message.reply`` under it is a Telegram round-trip — a FloodWait there
used to stall every other economy write until ``busy_timeout`` gave up
(``db/pragma.py:63``, 5 s).

Nothing is at risk of being lost on that branch: the ``UPDATE`` matched
nothing, so committing and rolling back are the same outcome. What the
checkpoint buys is the lock, which is why the assertion below is about
ORDER, not about balances.

The success path already committed correctly before this ticket (the
``# #493`` comment on it); the second test pins that placement so a
later refactor cannot quietly drop it.

Recording stubs, not a real session — the order of ``checkpoint()``
against ``message.reply`` is exactly what a fake records.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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
from telegram_invite_bot.handlers.moderation import handle_fine

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram import Bot
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.moderation_repo import ModerationRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo

_DEV_ID = 123456789
_TARGET_ID = 555


class _FakeUser:
    def __init__(self, uid: int, *, is_bot: bool = False) -> None:
        self.id = uid
        self.is_bot = is_bot
        self.first_name = "Target" if uid == _TARGET_ID else "Dev"
        self.language_code = "ru"


class _FakeReply:
    def __init__(self, uid: int) -> None:
        self.from_user = _FakeUser(uid)


class _FakeMessage:
    """Records replies instead of calling the Bot API."""

    def __init__(self, text: str) -> None:
        self.chat = type("_Chat", (), {"id": -100, "type": "supergroup"})()
        self.from_user = _FakeUser(_DEV_ID)
        self.reply_to_message = _FakeReply(_TARGET_ID)
        self.text = text
        self.caption = None
        self.message_id = 7
        self.replies: list[str] = []

    async def reply(self, text: str, **_kw: object) -> None:
        self.replies.append(text)


class _Wallet:
    def __init__(self, balance: int) -> None:
        self.user_id = _TARGET_ID
        self.balance = balance


class _EconomyStub:
    """``debit`` returns ``None`` to model the lost race."""

    def __init__(self, *, debit_ok: bool) -> None:
        self._debit_ok = debit_ok
        self.calls: list[str] = []

    async def get(self, _uid: int) -> _Wallet:
        self.calls.append("get")
        return _Wallet(1000)

    async def debit(self, _uid: int, amount: int) -> _Wallet | None:
        self.calls.append("debit")
        return _Wallet(1000 - amount) if self._debit_ok else None


class _RecordingRepo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def record(self, **_kw: Any) -> None:  # noqa: ANN401
        self.calls.append("record")

    async def record_action(self, **_kw: Any) -> None:  # noqa: ANN401
        self.calls.append("record_action")


class _SettingsRepoStub:
    async def get_language(self, _uid: int) -> str:
        return "ru"


class _UsersRepoStub:
    async def get(self, _uid: int) -> None:
        return None


class _BotStub:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_message(self, uid: int, _text: str, **_kw: object) -> None:
        self.sent.append(uid)


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


async def _run(tmp_path: Path, *, debit_ok: bool) -> tuple[_FakeMessage, _EconomyStub, list[int]]:
    message = _FakeMessage("/fine 100 спам")
    economy = _EconomyStub(debit_ok=debit_ok)
    ledger = _RecordingRepo()
    moderation = _RecordingRepo()
    fired: list[int] = []

    async def checkpoint() -> None:
        fired.append(len(message.replies))

    await handle_fine(
        cast("Message", message),
        cast("Bot", _BotStub()),
        cast("ModerationRepo", moderation),
        cast("EconomyRepo", economy),
        cast("TransactionsRepo", ledger),
        cast("UsersRepo", _UsersRepoStub()),
        cast("UserSettingsRepo", _SettingsRepoStub()),
        _settings(tmp_path),
        cast("Checkpoint", checkpoint),
    )
    return message, economy, fired


async def test_a_lost_debit_race_commits_before_the_refusal(tmp_path: Path) -> None:
    """The 0-row ``UPDATE`` took the writer slot; the reply must not
    inherit it.

    Drop the checkpoint from that branch and ``fired`` becomes ``[]``.
    """
    message, economy, fired = await _run(tmp_path, debit_ok=False)
    assert economy.calls == ["get", "debit"]
    assert fired == [0]
    assert len(message.replies) == 1


async def test_the_landed_fine_commits_before_the_confirmation(tmp_path: Path) -> None:
    """#493's placement, pinned: the debit, the ledger row and the audit
    row are durable before either Telegram round-trip."""
    message, economy, fired = await _run(tmp_path, debit_ok=True)
    assert economy.calls == ["get", "debit"]
    assert fired == [0]
    assert len(message.replies) == 1
