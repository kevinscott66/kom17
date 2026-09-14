"""#109 — the STT ceiling counted requests; Whisper charges by the second.

``OPENAI_STT_GROUP_DAILY_LIMIT`` bounds how many voices a group may have
transcribed per day. Whisper's invoice is computed from seconds of
audio, and nothing bounded that: the only length-ish check in the
handler was a 25MB size cap, and 20MB of ogg/opus — Telegram's own
``getFile`` ceiling — is hours of speech. Two hundred long voice notes
therefore cost two orders of magnitude more than the two hundred short
ones the ceiling was sized against, and creating another group to start
a fresh allowance is free.

The duration arrives inside the update, before any download and before
any billed call, so both new gates are free to evaluate:

* a single voice longer than ``OPENAI_STT_MAX_VOICE_SECONDS`` is
  refused outright;
* ``OPENAI_STT_GROUP_DAILY_SECONDS`` budgets the audio a group may
  spend per UTC day, counted *including* the voice about to be sent.

Every test here asserts on ``bot.downloads`` — reaching the download is
the spend boundary, and anything short of it costs nothing.
"""

from __future__ import annotations

import asyncio
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
    OpenAiConfig,
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
from telegram_invite_bot.repositories.voice_transcription_repo import (
    VoiceTranscriptionRepo,
)

if TYPE_CHECKING:
    from pathlib import Path

    from aiogram import Bot
    from aiogram.types import Message

    from telegram_invite_bot.db import EngineRegistry

_CHAT_ID = -1009876543210
_SPEAKER_ID = 88

# Roughly what 20MB of ogg/opus at a typical voice-note bitrate holds —
# the length the old code would have sent to Whisper without blinking.
_THREE_HOURS = 3 * 60 * 60


class _FakeVoice:
    file_id = "fid"
    file_unique_id = "uid"
    file_size = 1024

    def __init__(self, duration: int) -> None:
        self.duration = duration


class _FakeChat:
    id = _CHAT_ID


class _FakeUser:
    id = _SPEAKER_ID


class _FakeMessage:
    """Enough of ``Message`` for the pre-spend half of the handler.

    No reply surface on purpose: these tests must never let the handler
    past the download, so an ``AttributeError`` past it is a failure
    signal rather than a gap.
    """

    chat = _FakeChat()
    from_user = _FakeUser()
    message_id = 900

    def __init__(self, duration: int) -> None:
        self.voice = _FakeVoice(duration)


class _FakeBot:
    """Records whether the handler spent anything."""

    def __init__(self) -> None:
        self.downloads = 0

    async def download(self, _file_id: str, destination: Any) -> None:  # noqa: ANN401
        self.downloads += 1
        destination.write(b"ogg")


class _SoftFailure:
    """Stops the handler right after the spend boundary.

    A transcribe that reports a soft failure returns before any Telegram
    reply, so ``downloads`` alone tells us whether the gate let the call
    through.
    """

    ok = False
    error = "network"
    text = None


class _Stt:
    def __init__(self, *_a: object, **_kw: object) -> None: ...

    async def transcribe(self, *_a: object, **_kw: object) -> _SoftFailure:
        return _SoftFailure()


@pytest.fixture(autouse=True)
def _no_real_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vt, "WhisperSttService", _Stt)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
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


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    settings = _settings(tmp_path)
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


async def _seed(
    registry: EngineRegistry,
    durations: list[int | None],
    *,
    offset: timedelta = timedelta(),
    group_id: int = _CHAT_ID,
) -> None:
    async with session_for(registry, DBName.USERS) as session:
        for duration in durations:
            session.add(
                VoiceTranscription(
                    group_id=group_id,
                    user_id=_SPEAKER_ID,
                    duration=duration,
                    created_at=vt._utc_midnight() + offset,  # noqa: SLF001
                )
            )


async def _run(
    registry: EngineRegistry,
    *,
    duration: int,
    daily_limit: int = 200,
    daily_seconds: int = 0,
    max_voice_seconds: int = 0,
) -> _FakeBot:
    bot = _FakeBot()
    await vt.handle_voice(
        cast("Message", _FakeMessage(duration)),
        cast("Bot", bot),
        registry,
        "sk-test",
        daily_limit,
        daily_seconds,
        max_voice_seconds,
    )
    return bot


# --------------------------------------------------------------------
# the per-voice cap
# --------------------------------------------------------------------


async def test_a_three_hour_voice_never_reaches_the_api(
    registry: EngineRegistry,
) -> None:
    """The single worst case: one forward, hours of billable audio."""
    await _enable(registry)
    bot = await _run(registry, duration=_THREE_HOURS, max_voice_seconds=300)
    assert bot.downloads == 0


async def test_an_ordinary_voice_still_gets_transcribed(
    registry: EngineRegistry,
) -> None:
    """Guard the guard — the gates above can still say yes."""
    await _enable(registry)
    bot = await _run(registry, duration=30, daily_seconds=3600, max_voice_seconds=300)
    assert bot.downloads == 1


async def test_a_voice_exactly_at_the_cap_is_allowed(
    registry: EngineRegistry,
) -> None:
    """The cap is a ceiling, not a strict bound — pins the boundary."""
    await _enable(registry)
    bot = await _run(registry, duration=300, max_voice_seconds=300)
    assert bot.downloads == 1


# --------------------------------------------------------------------
# the daily audio budget
# --------------------------------------------------------------------


async def test_the_budget_counts_seconds_not_calls(
    registry: EngineRegistry,
) -> None:
    """The bug itself, stated as the difference between two gates.

    Ten voices is nowhere near the two-hundred-call ceiling, so the old
    gate waves this through; those ten voices are an hour of audio, so
    the budget stops it. Both halves are asserted — a fix that simply
    made the handler stricter everywhere would fail the first.
    """
    await _enable(registry)
    await _seed(registry, [360] * 10)  # one hour of audio, ten calls

    allowed_by_the_call_gate = await _run(registry, duration=30, daily_limit=200)
    assert allowed_by_the_call_gate.downloads == 1

    stopped_by_the_budget = await _run(registry, duration=30, daily_limit=200, daily_seconds=3600)
    assert stopped_by_the_budget.downloads == 0


async def test_the_budget_includes_the_voice_being_sent(
    registry: EngineRegistry,
) -> None:
    """Pre-spend, not post-hoc.

    Nothing has been spent today, so a gate that only asked "is the
    allowance already gone?" would let this through — and it carries
    twice the budget on its own. Checking ``spent + incoming`` is what
    makes the budget mean anything on the first call of the day.
    """
    await _enable(registry)
    bot = await _run(registry, duration=200, daily_seconds=100)
    assert bot.downloads == 0


async def test_a_voice_that_exactly_fills_the_budget_is_allowed(
    registry: EngineRegistry,
) -> None:
    await _enable(registry)
    await _seed(registry, [70])
    bot = await _run(registry, duration=30, daily_seconds=100)
    assert bot.downloads == 1


async def test_yesterdays_audio_does_not_eat_todays_budget(
    registry: EngineRegistry,
) -> None:
    await _enable(registry)
    await _seed(registry, [_THREE_HOURS], offset=-timedelta(days=1))
    bot = await _run(registry, duration=30, daily_seconds=3600)
    assert bot.downloads == 1


async def test_legacy_rows_without_a_duration_do_not_break_the_sum(
    registry: EngineRegistry,
) -> None:
    """``duration`` is nullable; rows predating it must not raise."""
    await _enable(registry)
    await _seed(registry, [None, 60, None])
    bot = await _run(registry, duration=30, daily_seconds=3600)
    assert bot.downloads == 1


async def test_zero_disables_both_new_gates(registry: EngineRegistry) -> None:
    """``0`` is the project-wide "no cap" convention (cf. AiQuotaConfig).

    Also the compatibility contract: callers that pass neither argument
    behave exactly as before this change.
    """
    await _enable(registry)
    await _seed(registry, [_THREE_HOURS] * 5)
    bot = await _run(registry, duration=_THREE_HOURS, daily_seconds=0, max_voice_seconds=0)
    assert bot.downloads == 1


async def test_the_budget_query_is_skipped_for_a_disabled_group(
    registry: EngineRegistry,
) -> None:
    """No settings row → transcription off → no gate work, no spend."""
    bot = await _run(registry, duration=30, daily_seconds=3600)
    assert bot.downloads == 0


# --------------------------------------------------------------------
# the ceilings as shipped
# --------------------------------------------------------------------


def test_the_shipped_defaults_actually_bound_the_bill() -> None:
    """A gate nobody turns on protects nobody.

    Every knob here treats ``0`` as unlimited, so shipping either
    default as ``0`` would leave the handler exactly as exposed as it
    was before — the code would be present and inert. The bands are
    written out rather than compared to the fields themselves: an
    assertion derived from the constant it is checking holds for every
    value of that constant, including the one that disables the gate.

    Upper bounds are order-of-magnitude sanity, not policy. Ten minutes
    is already far longer than anything anyone speaks into a group
    chat, and four hours of audio a day is comfortably more than a busy
    group produces while still costing cents rather than dollars.
    """
    config = OpenAiConfig()

    assert 0 < config.stt_max_voice_seconds <= 600
    assert 0 < config.stt_group_daily_seconds <= 4 * 60 * 60


def test_the_router_hands_both_ceilings_to_the_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gates are only as good as the wiring that feeds them.

    ``handle_voice`` defaults both new arguments to unlimited so that
    existing callers keep working, which means a router that forgot to
    pass them would fail silently and no other test here would notice.
    """
    seen: list[tuple[int, int, int, int]] = []

    async def _spy(
        _message: object,
        _bot: object,
        _registry: object,
        _api_key: object,
        daily_limit: int = 0,
        daily_seconds: int = 0,
        max_voice_seconds: int = 0,
        *,
        user_daily_seconds: int = 0,
    ) -> None:
        seen.append((daily_limit, daily_seconds, max_voice_seconds, user_daily_seconds))

    monkeypatch.setattr(vt, "handle_voice", _spy)

    settings = _settings(tmp_path)
    router = vt.build_router(build_registry(settings), settings)
    entry = router.message.handlers[0].callback

    asyncio.run(entry(None, None))

    assert seen == [
        (
            settings.openai.stt_group_daily_limit,
            settings.openai.stt_group_daily_seconds,
            settings.openai.stt_max_voice_seconds,
            settings.openai.stt_user_daily_seconds,
        )
    ]


# --------------------------------------------------------------------
# the repository sum
# --------------------------------------------------------------------


async def test_seconds_since_sums_only_this_group_and_this_day(
    registry: EngineRegistry,
) -> None:
    await _seed(registry, [10, 20])
    await _seed(registry, [1000], group_id=-100111)
    await _seed(registry, [4000], offset=-timedelta(days=1))

    async with session_for(registry, DBName.USERS) as session:
        total = await VoiceTranscriptionRepo(session).seconds_since(
            _CHAT_ID,
            vt._utc_midnight(),  # noqa: SLF001
        )

    assert total == 30


async def test_seconds_since_is_zero_for_a_group_with_no_rows(
    registry: EngineRegistry,
) -> None:
    """SUM over no rows is NULL in SQL; the caller must see an int."""
    async with session_for(registry, DBName.USERS) as session:
        total = await VoiceTranscriptionRepo(session).seconds_since(
            _CHAT_ID,
            vt._utc_midnight(),  # noqa: SLF001
        )

    assert total == 0
