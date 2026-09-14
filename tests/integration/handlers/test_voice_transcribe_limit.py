"""Per-group daily Whisper ceiling for passive group transcription.

Group STT is the one OpenAI-billed path with no user-side price: a group
admin flips ``voice_transcription`` on and every voice note in that chat
becomes a paid API call charged to the operator's key. ``/ai``, ``/ask``
and ``/voice`` are gated by ``AiQuotaService``; this path had nothing, so
one busy (or hostile) group could run the key down unattended.

These tests pin the gate at the level that matters for spend: does the
handler still reach ``bot.download`` / ``WhisperSttService.transcribe``?
Anything short of that costs nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
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
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import GroupSettings, VoiceTranscription
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers import voice_transcribe as vt

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram import Bot
    from aiogram.types import Message

    from telegram_invite_bot.db import EngineRegistry

_CHAT_ID = -1001234567890
_SPEAKER_ID = 77


class _FakeVoice:
    file_id = "fid"
    file_unique_id = "uid"
    duration = 5
    file_size = 1024


class _FakeChat:
    id = _CHAT_ID


class _FakeUser:
    id = _SPEAKER_ID


class _FakeMessage:
    """Just enough of ``Message`` for the pre-spend half of the handler.

    The tests never let the handler past the download, so the reply /
    routing surface is deliberately absent — if a regression made the
    handler reach it, the AttributeError is the failure signal.
    """

    voice = _FakeVoice()
    chat = _FakeChat()
    from_user = _FakeUser()
    message_id = 500


class _FakeBot:
    """Records whether the handler spent anything."""

    def __init__(self) -> None:
        self.downloads = 0

    async def download(self, _file_id: str, destination: Any) -> None:  # noqa: ANN401
        self.downloads += 1
        destination.write(b"ogg")


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    settings = Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc")),
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
    reg = build_registry(settings)
    async with reg.engine(DBName.USERS).begin() as conn:
        await conn.run_sync(UsersBase.metadata.create_all)
    try:
        yield reg
    finally:
        await reg.dispose()


async def _enable_transcription(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.USERS) as session:
        session.add(GroupSettings(group_id=_CHAT_ID, voice_transcription=1))


async def _seed_transcriptions(
    registry: EngineRegistry, count: int, *, offset: timedelta = timedelta()
) -> None:
    async with session_for(registry, DBName.USERS) as session:
        for _ in range(count):
            session.add(
                VoiceTranscription(
                    group_id=_CHAT_ID,
                    user_id=_SPEAKER_ID,
                    created_at=vt._utc_midnight() + offset,  # noqa: SLF001
                )
            )


async def _run(registry: EngineRegistry, limit: int) -> _FakeBot:
    bot = _FakeBot()
    await vt.handle_voice(
        cast("Message", _FakeMessage()),
        cast("Bot", bot),
        registry,
        "sk-test",
        limit,
    )
    return bot


async def test_group_at_the_ceiling_never_reaches_the_api(
    registry: EngineRegistry,
) -> None:
    await _enable_transcription(registry)
    await _seed_transcriptions(registry, 3)
    bot = await _run(registry, 3)
    assert bot.downloads == 0


async def test_group_under_the_ceiling_still_transcribes(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_transcription(registry)
    await _seed_transcriptions(registry, 2)

    # Stop right after the spend boundary: a transcribe that returns a
    # soft failure makes the handler return before any Telegram reply,
    # so the download counter alone tells us the gate let it through.
    class _Result:
        ok = False
        error = "network"
        text = None

    class _Stt:
        def __init__(self, *_a: object, **_kw: object) -> None: ...

        async def transcribe(self, *_a: object, **_kw: object) -> _Result:
            return _Result()

    monkeypatch.setattr(vt, "WhisperSttService", _Stt)
    bot = await _run(registry, 3)
    assert bot.downloads == 1


async def test_zero_means_unlimited(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``0`` is the project-wide "no cap" convention (cf. AiQuotaConfig)."""
    await _enable_transcription(registry)
    await _seed_transcriptions(registry, 50)

    class _Result:
        ok = False
        error = "network"
        text = None

    class _Stt:
        def __init__(self, *_a: object, **_kw: object) -> None: ...

        async def transcribe(self, *_a: object, **_kw: object) -> _Result:
            return _Result()

    monkeypatch.setattr(vt, "WhisperSttService", _Stt)
    bot = await _run(registry, 0)
    assert bot.downloads == 1


async def test_yesterdays_rows_do_not_eat_todays_allowance(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable_transcription(registry)
    await _seed_transcriptions(registry, 10, offset=-timedelta(days=1))

    class _Result:
        ok = False
        error = "network"
        text = None

    class _Stt:
        def __init__(self, *_a: object, **_kw: object) -> None: ...

        async def transcribe(self, *_a: object, **_kw: object) -> _Result:
            return _Result()

    monkeypatch.setattr(vt, "WhisperSttService", _Stt)
    bot = await _run(registry, 3)
    assert bot.downloads == 1


async def test_disabled_group_is_untouched_by_the_gate(
    registry: EngineRegistry,
) -> None:
    """No settings row → transcription off → no count query, no spend."""
    bot = await _run(registry, 1)
    assert bot.downloads == 0
