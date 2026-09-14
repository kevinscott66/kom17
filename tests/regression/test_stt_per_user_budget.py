"""#1938 — every STT ceiling was per-group, and a group costs nothing.

``OPENAI_STT_GROUP_DAILY_LIMIT`` and ``OPENAI_STT_GROUP_DAILY_SECONDS``
both count rows ``WHERE group_id = ?``. Group STT is also the one
OpenAI-billed path with no user-side price: the bill lands on the
operator's key and the only thing standing in front of it is those two
counters. Anyone can create a chat, add the bot, turn transcription on
from ``/voice_settings`` (admin-gated, and the creator is an admin),
and start on a fresh allowance — there is no allow-list of groups in
this port.

So fifty chats is fifty times the budget, with every per-group counter
inside its cap and no warning logged anywhere, because a warning only
fires when a cap is REACHED. ``OPENAI_STT_USER_DAILY_SECONDS`` closes
that by counting the SPEAKER across every group at once.

Like its sibling ``test_stt_audio_budget``, every test here asserts on
``bot.downloads``: reaching the download is the spend boundary and
anything short of it costs nothing.
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

_SPEAKER_ID = 4242
_OTHER_SPEAKER_ID = 4343
# The attacker's second chat: same person, a group the first one's
# counters know nothing about.
_FIRST_CHAT = -1001111111111
_SECOND_CHAT = -1002222222222


class _FakeVoice:
    file_id = "fid"
    file_unique_id = "uid"
    file_size = 1024

    def __init__(self, duration: int) -> None:
        self.duration = duration


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeMessage:
    """Enough of ``Message`` for the pre-spend half of the handler."""

    message_id = 900

    def __init__(self, chat_id: int, user_id: int, duration: int) -> None:
        self.chat = _FakeChat(chat_id)
        self.from_user = _FakeUser(user_id)
        self.voice = _FakeVoice(duration)


class _FakeBot:
    def __init__(self) -> None:
        self.downloads = 0

    async def download(self, _file_id: str, destination: Any) -> None:  # noqa: ANN401
        self.downloads += 1
        destination.write(b"ogg")


class _SoftFailure:
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


async def _enable(registry: EngineRegistry, *chat_ids: int) -> None:
    async with session_for(registry, DBName.USERS) as session:
        for chat_id in chat_ids:
            session.add(GroupSettings(group_id=chat_id, voice_transcription=1))


async def _seed(
    registry: EngineRegistry,
    durations: list[int | None],
    *,
    group_id: int,
    user_id: int = _SPEAKER_ID,
    offset: timedelta = timedelta(),
) -> None:
    async with session_for(registry, DBName.USERS) as session:
        for duration in durations:
            session.add(
                VoiceTranscription(
                    group_id=group_id,
                    user_id=user_id,
                    duration=duration,
                    created_at=vt._utc_midnight() + offset,  # noqa: SLF001
                )
            )


async def _run(
    registry: EngineRegistry,
    *,
    duration: int,
    chat_id: int = _SECOND_CHAT,
    user_id: int = _SPEAKER_ID,
    daily_seconds: int = 0,
    user_daily_seconds: int = 0,
) -> _FakeBot:
    bot = _FakeBot()
    await vt.handle_voice(
        cast("Message", _FakeMessage(chat_id, user_id, duration)),
        cast("Bot", bot),
        registry,
        "sk-test",
        0,
        daily_seconds,
        0,
        user_daily_seconds=user_daily_seconds,
    )
    return bot


# --------------------------------------------------------------------
# the hole itself
# --------------------------------------------------------------------


async def test_a_fresh_group_does_not_reset_the_speakers_budget(
    registry: EngineRegistry,
) -> None:
    """The whole finding, in one run.

    The speaker has already spent their day's audio in ``_FIRST_CHAT``
    and moves to a brand-new group. Every per-group counter in
    ``_SECOND_CHAT`` reads zero — which is exactly true and exactly
    useless, because the bill does not care which chat the ogg came
    from.
    """
    await _enable(registry, _FIRST_CHAT, _SECOND_CHAT)
    await _seed(registry, [600, 600, 600], group_id=_FIRST_CHAT)

    bot = await _run(registry, duration=60, daily_seconds=3600, user_daily_seconds=1800)

    assert bot.downloads == 0


async def test_the_group_budget_alone_lets_the_second_group_through(
    registry: EngineRegistry,
) -> None:
    """The control that makes the test above mean something.

    Same seeded spend, same fresh group, only the per-speaker ceiling
    turned off. The group budget admits it — so the assertion above is
    detecting the new gate and not some unrelated refusal.
    """
    await _enable(registry, _FIRST_CHAT, _SECOND_CHAT)
    await _seed(registry, [600, 600, 600], group_id=_FIRST_CHAT)

    bot = await _run(registry, duration=60, daily_seconds=3600, user_daily_seconds=0)

    assert bot.downloads == 1


async def test_another_speaker_in_the_same_group_is_unaffected(
    registry: EngineRegistry,
) -> None:
    """The ceiling is keyed on the person, not on the chat they are in.

    An ordinary member of a group whose loudest user has spent the day's
    audio must still be transcribed — otherwise the gate would hand any
    single user a way to mute everyone else.
    """
    await _enable(registry, _SECOND_CHAT)
    await _seed(registry, [1800], group_id=_SECOND_CHAT)

    bot = await _run(registry, duration=60, user_id=_OTHER_SPEAKER_ID, user_daily_seconds=1800)

    assert bot.downloads == 1


async def test_the_budget_includes_the_voice_being_sent(
    registry: EngineRegistry,
) -> None:
    """Same rule as the group budget: a request is priced by what it carries.

    "You were under the limit when you started" would wave through a
    single voice of any length, which is the failure ``max_voice_seconds``
    and the group budget were both written against.
    """
    await _enable(registry, _SECOND_CHAT)
    await _seed(registry, [1700], group_id=_FIRST_CHAT)

    bot = await _run(registry, duration=200, user_daily_seconds=1800)

    assert bot.downloads == 0


async def test_a_voice_that_exactly_fills_the_budget_is_allowed(
    registry: EngineRegistry,
) -> None:
    """The boundary belongs to the user, as it does for the group."""
    await _enable(registry, _SECOND_CHAT)
    await _seed(registry, [1700], group_id=_FIRST_CHAT)

    bot = await _run(registry, duration=100, user_daily_seconds=1800)

    assert bot.downloads == 1


async def test_yesterdays_audio_does_not_eat_todays_budget(
    registry: EngineRegistry,
) -> None:
    await _enable(registry, _SECOND_CHAT)
    await _seed(registry, [1800], group_id=_FIRST_CHAT, offset=timedelta(minutes=-30))

    bot = await _run(registry, duration=60, user_daily_seconds=1800)

    assert bot.downloads == 1


async def test_zero_disables_the_gate(registry: EngineRegistry) -> None:
    """``0`` is unlimited here too, the project-wide convention."""
    await _enable(registry, _SECOND_CHAT)
    await _seed(registry, [99_999], group_id=_FIRST_CHAT)

    bot = await _run(registry, duration=60, user_daily_seconds=0)

    assert bot.downloads == 1


async def test_the_query_is_skipped_for_a_disabled_group(
    registry: EngineRegistry,
) -> None:
    """A group that never turned STT on must stay one SELECT.

    The counter query is cheap, but it is per voice message in every
    group the bot sits in, and almost none of them have transcription
    enabled.
    """
    await _seed(registry, [99_999], group_id=_FIRST_CHAT)
    calls = 0
    original = VoiceTranscriptionRepo.seconds_since_for_user

    async def _counted(self: VoiceTranscriptionRepo, *a: object, **kw: object) -> int:
        nonlocal calls
        calls += 1
        return await original(self, *a, **kw)  # type: ignore[arg-type]

    VoiceTranscriptionRepo.seconds_since_for_user = _counted  # type: ignore[method-assign]
    try:
        bot = await _run(registry, duration=60, user_daily_seconds=1800)
    finally:
        VoiceTranscriptionRepo.seconds_since_for_user = original  # type: ignore[method-assign]

    assert bot.downloads == 0
    assert calls == 0


# --------------------------------------------------------------------
# the ceiling as shipped
# --------------------------------------------------------------------


def test_the_shipped_default_actually_bounds_the_bill() -> None:
    """A gate nobody turns on protects nobody.

    Written out rather than compared against the field itself: an
    assertion derived from the constant it checks holds for every value
    of that constant, ``0`` — the one that disables the gate — included.

    The upper bound is order-of-magnitude sanity. A spoken message runs
    ten to thirty seconds, so an hour of audio a day from ONE person is
    already far past ordinary use.
    """
    config = OpenAiConfig()

    assert 0 < config.stt_user_daily_seconds <= 60 * 60


async def test_seconds_since_for_user_spans_groups_and_stops_at_midnight(
    registry: EngineRegistry,
) -> None:
    """The repository half, asserted directly.

    The handler tests above can only see the gate's verdict; this pins
    the two properties the verdict rests on — that the sum crosses group
    boundaries and that it does not cross the day boundary.
    """
    await _seed(registry, [100, None], group_id=_FIRST_CHAT)
    await _seed(registry, [200], group_id=_SECOND_CHAT)
    await _seed(registry, [10_000], group_id=_FIRST_CHAT, offset=timedelta(minutes=-30))
    await _seed(registry, [7000], group_id=_FIRST_CHAT, user_id=_OTHER_SPEAKER_ID)

    async with session_for(registry, DBName.USERS) as session:
        total = await VoiceTranscriptionRepo(session).seconds_since_for_user(
            _SPEAKER_ID, vt._utc_midnight()
        )

    assert total == 300
