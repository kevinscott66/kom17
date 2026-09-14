"""#260 + #221 — the two ways the group-STT ceilings failed to bound spend.

Both are the same hole seen from either end. The ceilings are computed
from ``voice_transcriptions`` rows, and that row was written neither
often enough nor early enough:

* **#260** — ``WhisperSttService`` reports ``empty`` (silence or noise)
  and ``bad_response`` (an unreadable body) on an **HTTP 200**. OpenAI
  transcribed the audio and billed the seconds; only the payload was
  useless. No row was written, so neither ``count_since`` nor
  ``seconds_since`` moved and a group could spend the owner's key
  forever on 300-second voices that transcribe to nothing.
* **#221** — the counters were read, then three network calls happened
  before the row appeared. Every voice arriving in the same tick read
  the same "before", so the ceilings held only for strictly sequential
  traffic.

The spend boundary is ``bot.download``: anything short of it costs
nothing, so ``downloads`` is what these tests assert on, alongside the
rows the run left behind.

The concurrency half is deterministic rather than timed. A real
``asyncio.gather`` of N handlers proves nothing here: the fakes finish
in microseconds, so the winner is done — row written, booking released
— before the loser's gate is even evaluated, and the test would pass on
a build with no reservation at all. Instead the winner is parked inside
``bot.download`` on an :class:`asyncio.Event` and held there, which is
exactly the state #221 is about: a request that is committed and paid
for but has no row yet.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
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

_CHAT_ID = -1005550001111
_SPEAKER_ID = 99


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

    No reply surface on purpose: every transcribe stub here reports a
    failure, so the handler returns before touching Telegram and an
    ``AttributeError`` past that point would be a real regression rather
    than a gap in the fake.
    """

    chat = _FakeChat()
    from_user = _FakeUser()
    message_id = 901

    def __init__(self, duration: int) -> None:
        self.voice = _FakeVoice(duration)


class _FakeBot:
    """Counts the spend boundary, and can park early callers there.

    ``block`` holds the first ``hold`` downloads. Everything past that
    falls through, so a build without the reservation reaches the
    assertion with a raised count instead of deadlocking on the event.
    """

    def __init__(
        self,
        *,
        block: asyncio.Event | None = None,
        hold: int = 1,
        fail_download: bool = False,
    ) -> None:
        self.downloads = 0
        self._block = block
        self._hold = hold
        self._fail = fail_download

    async def download(self, _file_id: str, destination: Any) -> None:  # noqa: ANN401
        self.downloads += 1
        if self._block is not None and self.downloads <= self._hold:
            await self._block.wait()
        await asyncio.sleep(0)
        if self._fail:
            raise RuntimeError("download exploded")
        destination.write(b"ogg")


def _stt_returning(error: str) -> type:
    """A ``WhisperSttService`` stub whose ``transcribe`` fails with ``error``.

    ``model_used`` is part of the surface even though nothing is
    transcribed: :func:`~telegram_invite_bot.handlers.voice_transcribe._persist`
    stamps it onto the row it writes for a billed failure, and it
    swallows exceptions, so a stub without it turns #260 into a silent
    no-op.
    """

    class _Result:
        ok = False
        text = None
        processing_ms = 42

        def __init__(self) -> None:
            self.error = error

    class _Stt:
        def __init__(self, *_a: object, **_kw: object) -> None: ...

        async def transcribe(self, *_a: object, **_kw: object) -> _Result:
            await asyncio.sleep(0)
            return _Result()

        @staticmethod
        def model_used() -> str:
            return "whisper-1"

    return _Stt


async def _settle(predicate: Callable[[], bool]) -> None:
    """Run the loop until ``predicate`` holds, then return.

    A bounded wait on a condition, not a sleep on a guess: the loop
    exits as soon as the state is reached, and the ceiling is only there
    so a broken build fails instead of hanging.
    """
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition never became true")


@pytest.fixture(autouse=True)
def _clean_reservations() -> Any:  # noqa: ANN401 — pytest generator fixture
    """No reservation may outlive the run that booked it.

    Asserted for every test in the file rather than in one of them: a
    leaked booking is silent until the group's next voice is refused for
    a request that finished hours ago, and the leak would be introduced
    by any future early ``return`` added past the gate.
    """
    vt._in_flight.clear()  # noqa: SLF001
    yield
    assert vt._in_flight == {}, vt._in_flight  # noqa: SLF001
    assert len(vt._gate_locks) == 0  # noqa: SLF001


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


async def _run(
    registry: EngineRegistry,
    bot: _FakeBot,
    *,
    duration: int = 60,
    daily_limit: int = 0,
    daily_seconds: int = 0,
) -> None:
    await vt.handle_voice(
        cast("Message", _FakeMessage(duration)),
        cast("Bot", bot),
        registry,
        "sk-test",
        daily_limit,
        daily_seconds,
    )


# --------------------------------------------------------------------
# #260 — billed outcomes that produced no transcript
# --------------------------------------------------------------------


@pytest.mark.parametrize("error", ["empty", "bad_response"])
async def test_billed_failure_is_counted(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    """HTTP 200 with nothing usable in it still moves both counters.

    ``empty`` and ``bad_response`` are the two outcomes OpenAI charges
    for and we cannot use. Before #260 they left the table untouched.
    """
    monkeypatch.setattr(vt, "WhisperSttService", _stt_returning(error))
    await _enable(registry)

    bot = _FakeBot()
    await _run(registry, bot, duration=300)

    assert bot.downloads == 1
    assert await _counters(registry) == (1, 300)


@pytest.mark.parametrize("error", ["network", "http_429", "http_500", "no_key"])
async def test_unbilled_failure_is_not_counted(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    """No reply, or a reply OpenAI refused before working — no charge.

    Counting these would be worse than not counting them: the allowance
    resets only at UTC midnight, so one outage would close the group's
    transcription for the rest of the day with nothing to show for it.
    """
    monkeypatch.setattr(vt, "WhisperSttService", _stt_returning(error))
    await _enable(registry)

    bot = _FakeBot()
    await _run(registry, bot, duration=300)

    assert bot.downloads == 1
    assert await _counters(registry) == (0, 0)


async def test_the_ceiling_actually_stops_after_a_billed_empty(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticket's own complaint, end to end.

    With a ceiling of one request, a first voice that transcribes to
    silence must consume the whole allowance. Before #260 the second
    voice — and every voice after it — sailed through.
    """
    monkeypatch.setattr(vt, "WhisperSttService", _stt_returning("empty"))
    await _enable(registry)

    bot = _FakeBot()
    await _run(registry, bot, duration=300, daily_limit=1)
    await _run(registry, bot, duration=300, daily_limit=1)

    assert bot.downloads == 1


async def test_the_seconds_budget_stops_after_a_billed_empty(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same for the budget that tracks the invoice rather than the count."""
    monkeypatch.setattr(vt, "WhisperSttService", _stt_returning("empty"))
    await _enable(registry)

    bot = _FakeBot()
    await _run(registry, bot, duration=300, daily_seconds=400)
    await _run(registry, bot, duration=300, daily_seconds=400)

    assert bot.downloads == 1


async def test_a_billed_empty_is_invisible_to_the_stats_card(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator card counts transcriptions; the ceiling counts spend.

    Same table, two readings. A row with no text belongs to the second
    one only — folding it into the card would inflate "Всего" and point
    "Последняя" at a row whose preview is blank.
    """
    monkeypatch.setattr(vt, "WhisperSttService", _stt_returning("empty"))
    await _enable(registry)

    await _run(registry, _FakeBot(), duration=300)

    assert await _counters(registry) == (1, 300)
    async with session_for(registry, DBName.USERS) as session:
        stats = await VoiceTranscriptionRepo(session).stats(_CHAT_ID)
    assert stats.total == 0
    assert stats.last_text is None
    assert stats.last_created is None
    assert stats.avg_processing_ms is None


# --------------------------------------------------------------------
# #221 — a request in the air, with no row to show for it yet
# --------------------------------------------------------------------


async def test_a_request_in_flight_holds_its_share_of_the_call_ceiling(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe from the ticket, with the race pinned open.

    The winner is parked mid-download: OpenAI is committed to but the
    row does not exist, which is the whole window #221 is about. Four
    more voices arrive into that window against a ceiling of one. Before
    the fix every one of them read "0 used" and bought its own Whisper
    request.
    """
    monkeypatch.setattr(vt, "WhisperSttService", _stt_returning("network"))
    await _enable(registry)

    release = asyncio.Event()
    bot = _FakeBot(block=release)
    winner = asyncio.create_task(_run(registry, bot, duration=10, daily_limit=1))
    await _settle(lambda: bot.downloads == 1)

    await asyncio.gather(*(_run(registry, bot, duration=10, daily_limit=1) for _ in range(4)))
    assert bot.downloads == 1

    release.set()
    await winner
    assert bot.downloads == 1
    assert await _counters(registry) == (0, 0)


async def test_a_request_in_flight_holds_its_share_of_the_audio_budget(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same window, on the budget the invoice is actually computed from.

    Sixty seconds are in the air against a budget of one hundred, so the
    next sixty do not fit — even though nothing has been written down.
    """
    monkeypatch.setattr(vt, "WhisperSttService", _stt_returning("network"))
    await _enable(registry)

    release = asyncio.Event()
    bot = _FakeBot(block=release)
    winner = asyncio.create_task(_run(registry, bot, duration=60, daily_seconds=100))
    await _settle(lambda: bot.downloads == 1)

    await _run(registry, bot, duration=60, daily_seconds=100)
    assert bot.downloads == 1

    release.set()
    await winner
    assert bot.downloads == 1


async def test_the_reservation_bounds_the_allowance_without_swallowing_it(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Booked is not the same as blocked.

    A ceiling of three admits three concurrent voices; only the fourth
    is refused, and it is refused while all three are still in the air
    with nothing written down. A fix that simply serialised the group to
    one call at a time would pass the two tests above and fail this one.
    """
    monkeypatch.setattr(vt, "WhisperSttService", _stt_returning("network"))
    await _enable(registry)

    release = asyncio.Event()
    bot = _FakeBot(block=release, hold=3)
    parked = [
        asyncio.create_task(_run(registry, bot, duration=10, daily_limit=3)) for _ in range(3)
    ]
    await _settle(lambda: bot.downloads == 3)

    await _run(registry, bot, duration=10, daily_limit=3)
    assert bot.downloads == 3

    release.set()
    await asyncio.gather(*parked)
    assert bot.downloads == 3


async def test_a_failed_download_gives_its_booking_back(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that never reaches OpenAI must not hold the allowance.

    The booking is made before the download and the download here blows
    up, so the only thing that can release it is the context manager's
    ``finally``. The autouse fixture checks the ledger is empty; this
    test also proves the allowance is genuinely usable again.
    """
    monkeypatch.setattr(vt, "WhisperSttService", _stt_returning("network"))
    await _enable(registry)

    await _run(registry, _FakeBot(fail_download=True), duration=10, daily_limit=1)
    assert await _counters(registry) == (0, 0)

    survivor = _FakeBot()
    await _run(registry, survivor, duration=10, daily_limit=1)
    assert survivor.downloads == 1


async def test_a_disabled_group_books_nothing(registry: EngineRegistry) -> None:
    """The common case: no settings row, no spend, no ledger entry."""
    bot = _FakeBot()
    await _run(registry, bot, duration=10, daily_limit=1)
    assert bot.downloads == 0
