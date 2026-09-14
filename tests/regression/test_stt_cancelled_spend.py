"""#1966: a deploy mid-Whisper spent the owner's key twice, silently.

The third instance of the shape #1954 (``/voice`` coins) and #1965
(``/ask`` quota slot) already closed, and the last one left untriaged.

``handle_voice`` sends the audio and only writes its
``voice_transcriptions`` row once ``transcribe`` returns. The upstream
timeout is sixty seconds (``WhisperSttService(api_key, timeout=60.0)``)
and uvicorn's ``timeout_graceful_shutdown`` is twenty
(``runner/webhook.py``), so an ordinary redeploy cancels the request
task with the audio already on the wire. ``CancelledError`` is a
``BaseException``, so none of the ``except Exception`` layers on the way
out see it, and ``webhook/server.py`` never writes a response — so
Telegram redelivers the same voice, the fresh process builds its
``seen_updates`` cache empty, and the audio is sent to OpenAI a SECOND
time. Two invoices for one voice message, and the day's ceilings — which
are computed from those rows — saw neither.

Why this is billed and the ``network`` error is not (#260 draws that
line): ``network`` means OpenAI never served the request, so charging
for it would let one outage close a group's transcription for the rest
of the day. A cancellation is the opposite case — WE walked away from a
request OpenAI had already accepted and is charging by the second, which
puts it with ``empty``/``bad_response`` rather than with ``network``.

The scope of the ``except`` is the whole point and the second test pins
it: it wraps ONLY the ``transcribe`` await. A cancel arriving earlier —
during the download, say — costs nothing, and recording a charge there
would close the group's budget for spend that never happened, i.e.
exactly what #260 warns about.
"""

from __future__ import annotations

import asyncio
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
from telegram_invite_bot.repositories.voice_transcription_repo import (
    VoiceTranscriptionRepo,
)

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram import Bot
    from aiogram.types import Message

    from telegram_invite_bot.db import EngineRegistry

_CHAT_ID = -1005550002222
_SPEAKER_ID = 77
_DURATION = 45


class _FakeVoice:
    file_id = "fid"
    file_unique_id = "uid"
    file_size = 1024
    duration = _DURATION


class _FakeChat:
    id = _CHAT_ID


class _FakeUser:
    id = _SPEAKER_ID


class _FakeMessage:
    """No reply surface: every run here dies before Telegram is touched."""

    chat = _FakeChat()
    from_user = _FakeUser()
    message_id = 902
    voice = _FakeVoice()


class _FakeBot:
    """Counts the spend boundary; can also be cancelled *at* it."""

    def __init__(self, *, cancel_in_download: bool = False) -> None:
        self.downloads = 0
        self._cancel = cancel_in_download

    async def download(self, _file_id: str, destination: Any) -> None:  # noqa: ANN401
        self.downloads += 1
        if self._cancel:
            raise asyncio.CancelledError
        destination.write(b"ogg")


class _CancellingStt:
    """``transcribe`` is cancelled with the audio already on the wire."""

    def __init__(self, *_a: object, **_kw: object) -> None: ...

    async def transcribe(self, *_a: object, **_kw: object) -> object:
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    @staticmethod
    def model_used() -> str:
        return "whisper-1"


@pytest.fixture(autouse=True)
def _clean_reservations() -> Any:  # noqa: ANN401 — pytest generator fixture
    """The booking must be released even on the way out through a cancel.

    ``_admit`` releases it in a ``finally``, so this holds today; it is
    asserted here because the fix adds an ``except`` clause on that same
    unwind path and an early ``return`` slipped in later would leak the
    slot for the life of the process.
    """
    vt._in_flight.clear()  # noqa: SLF001
    vt._user_in_flight.clear()  # noqa: SLF001
    yield
    assert vt._in_flight == {}, vt._in_flight  # noqa: SLF001
    assert vt._user_in_flight == {}, vt._user_in_flight  # noqa: SLF001


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


async def _enable(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.USERS) as session:
        session.add(GroupSettings(group_id=_CHAT_ID, voice_transcription=1))


async def _counters(registry: EngineRegistry) -> tuple[int, int]:
    """``(billed_requests, billed_seconds)`` for today, as the gate sees them."""
    async with session_for(registry, DBName.USERS) as session:
        repo = VoiceTranscriptionRepo(session)
        midnight = vt._utc_midnight()  # noqa: SLF001
        return (
            await repo.count_since(_CHAT_ID, midnight),
            await repo.seconds_since(_CHAT_ID, midnight),
        )


async def _run(registry: EngineRegistry, bot: _FakeBot) -> None:
    await vt.handle_voice(
        cast("Message", _FakeMessage()),
        cast("Bot", bot),
        registry,
        "sk-test",
    )


async def test_a_cancel_on_the_wire_is_recorded_as_billed(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invoice arrives whether or not we waited for the transcript."""
    await _enable(registry)
    monkeypatch.setattr(vt, "WhisperSttService", _CancellingStt)
    bot = _FakeBot()

    with pytest.raises(asyncio.CancelledError):
        await _run(registry, bot)

    assert bot.downloads == 1
    assert await _counters(registry) == (1, _DURATION)


async def test_a_cancel_before_the_audio_is_on_the_wire_records_nothing(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was sent, so nothing may be charged.

    This is the half that keeps the fix honest: a blanket ``except``
    around the handler body would bill a request that died in the
    download and close the group's day for spend that never happened.
    """
    await _enable(registry)
    monkeypatch.setattr(vt, "WhisperSttService", _CancellingStt)
    bot = _FakeBot(cancel_in_download=True)

    with pytest.raises(asyncio.CancelledError):
        await _run(registry, bot)

    assert await _counters(registry) == (0, 0)


async def test_a_failed_write_does_not_replace_the_cancellation(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller is waiting on a ``CancelledError`` — it must arrive.

    ``_persist`` swallows its own failures today, but that is its
    internal choice and a refactor could take it back; the cancellation
    path must not depend on it. Mirrors the same contract pinned for
    #1954 in ``test_voice_hold_survives_cancellation.py``.
    """
    await _enable(registry)
    monkeypatch.setattr(vt, "WhisperSttService", _CancellingStt)

    async def _explode(*_a: object, **_kw: object) -> None:
        raise RuntimeError("persist exploded")

    monkeypatch.setattr(vt, "_persist", _explode)

    with pytest.raises(asyncio.CancelledError):
        await _run(registry, _FakeBot())
