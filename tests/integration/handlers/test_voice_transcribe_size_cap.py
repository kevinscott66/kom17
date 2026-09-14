"""The size cap on passive group transcription must be fail-closed (#119).

``_MAX_VOICE_BYTES`` existed, but it was checked against
``voice.file_size`` — an *optional* field in the Bot API. A message that
simply omits it walked straight past the guard, and whatever the download
produced was uploaded to Whisper, which bills by the second against the
operator's key. The cap has to be re-applied to the bytes we actually
hold; the declared size stays as a cheap way to skip the download.

Like ``test_voice_transcribe_limit``, these tests measure spend at the
only boundary that costs money: did the handler reach
``WhisperSttService.transcribe``?
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
from telegram_invite_bot.db.models.users import GroupSettings
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

#: Small enough that a test can exceed it without allocating megabytes.
_TEST_CAP = 1024


class _FakeVoice:
    file_id = "fid"
    file_unique_id = "uid"
    duration = 5

    def __init__(self, file_size: int | None) -> None:
        self.file_size = file_size


class _FakeChat:
    id = _CHAT_ID


class _FakeUser:
    id = _SPEAKER_ID


class _FakeMessage:
    chat = _FakeChat()
    from_user = _FakeUser()
    message_id = 500

    def __init__(self, file_size: int | None) -> None:
        self.voice = _FakeVoice(file_size)


class _FakeBot:
    """Serves ``payload`` bytes and records the download."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.downloads = 0

    async def download(self, _file_id: str, destination: Any) -> None:  # noqa: ANN401
        self.downloads += 1
        destination.write(self._payload)


class _Result:
    ok = False
    error = "network"
    text = None


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


async def _run(
    registry: EngineRegistry,
    monkeypatch: pytest.MonkeyPatch,
    *,
    declared: int | None,
    actual: int,
    api_key: str | None = "sk-test",
) -> tuple[_FakeBot, list[int]]:
    """Drive the handler once; return the bot and the transcribe sizes."""
    async with session_for(registry, DBName.USERS) as session:
        session.add(GroupSettings(group_id=_CHAT_ID, voice_transcription=1))

    transcribed: list[int] = []

    class _Stt:
        def __init__(self, *_a: object, **_kw: object) -> None: ...

        async def transcribe(self, audio: bytes, **_kw: object) -> _Result:
            transcribed.append(len(audio))
            return _Result()

    monkeypatch.setattr(vt, "WhisperSttService", _Stt)
    monkeypatch.setattr(vt, "_MAX_VOICE_BYTES", _TEST_CAP)

    bot = _FakeBot(b"x" * actual)
    await vt.handle_voice(
        cast("Message", _FakeMessage(declared)),
        cast("Bot", bot),
        registry,
        api_key,
        0,
    )
    return bot, transcribed


async def test_declared_oversize_is_skipped_without_downloading(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-check is still worth having: when Telegram does tell us
    the size, we spend neither bandwidth nor an API call."""
    bot, transcribed = await _run(
        registry, monkeypatch, declared=_TEST_CAP + 1, actual=_TEST_CAP + 1
    )
    assert bot.downloads == 0
    assert transcribed == []


async def test_missing_file_size_no_longer_bypasses_the_cap(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#119 proper: ``file_size is None`` used to mean "unchecked", and
    an oversized body reached the paid API. Now the download happens (we
    cannot know the size before it) but the upload does not."""
    bot, transcribed = await _run(registry, monkeypatch, declared=None, actual=_TEST_CAP + 1)
    assert bot.downloads == 1
    assert transcribed == []


async def test_a_lying_file_size_does_not_get_us_past_the_cap(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``file_size`` is upstream-supplied metadata, not a measurement.
    What we bill on is the byte count in hand."""
    bot, transcribed = await _run(registry, monkeypatch, declared=10, actual=_TEST_CAP + 1)
    assert bot.downloads == 1
    assert transcribed == []


async def test_an_ordinary_voice_note_still_goes_through(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not turn every unsized voice note into a no-op —
    that would silently switch group transcription off."""
    bot, transcribed = await _run(registry, monkeypatch, declared=None, actual=64)
    assert bot.downloads == 1
    assert transcribed == [64]


async def test_the_cap_is_inclusive(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly ``_MAX_VOICE_BYTES`` is allowed; the refusal starts one
    byte later. Pinned so a later ``>=`` does not quietly shrink the
    limit by one."""
    _bot, transcribed = await _run(registry, monkeypatch, declared=None, actual=_TEST_CAP)
    assert transcribed == [_TEST_CAP]


async def test_a_keyless_deployment_downloads_nothing(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#742: without ``OPENAI_API_KEY`` the handler must bail immediately.

    ``WhisperSttService`` refuses keyless on its own (``error="no_key"``,
    no network), but only after ``_admit`` has taken the chat lock, read
    the group settings, run both counter queries and booked the request
    against the daily allowance — and after the ogg bytes have been
    pulled down. On a deployment with no key that is a lock, four
    queries and a full media download for every group voice message,
    buying an allowance nothing can ever spend.
    """
    bot, transcribed = await _run(registry, monkeypatch, declared=None, actual=64, api_key=None)
    assert bot.downloads == 0
    assert transcribed == []
