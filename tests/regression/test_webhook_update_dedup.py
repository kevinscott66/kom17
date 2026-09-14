"""#108 — a resent Telegram update must not run the handler twice.

Telegram's webhook contract is at-least-once. It resends an update
whenever a 2xx does not come back in time, and this endpoint answers
only after ``feed_update`` returns, so "in time" is bounded by the
slowest handler rather than by anything the HTTP layer controls. One
``/voice`` is allowed sixty seconds of speech synthesis
(``TtsConfig.timeout_seconds``) before the audio upload even begins,
and it pre-debits the wallet and *commits* that debit through a
checkpoint so the write does not hold SQLite's single writer slot.

Put together: a user sends one long ``/voice``, Telegram gives up
waiting and resends, and the second run debits the same wallet again
for the same message. Nothing in the codebase deduplicated
``update_id`` — the resend was indistinguishable from a new message.

The fix claims the id before dispatching. The ordering is the whole
point and is pinned here: the resend that matters is the one that
arrives *while the first delivery is still running*, so a claim taken
on completion would be taken exactly too late. The one branch that
deliberately asks Telegram to retry — the 500 on a dispatch failure —
hands the claim back, otherwise the guard would swallow the retry it
just requested.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dishka import make_async_container
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY

from telegram_invite_bot.app import Application
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
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache
from telegram_invite_bot.webhook import server as server_module
from telegram_invite_bot.webhook.server import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:abc"),
        webhook=WebhookConfig(WEBHOOK_PATH="/webhook", WEBHOOK_SECRET_TOKEN=None),
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
async def application(tmp_path: Path) -> AsyncIterator[Application]:
    settings = _settings(tmp_path)
    app = Application(
        container=make_async_container(),
        settings=settings,
        bot=Bot(
            token="123:abc",
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        ),
        dispatcher=Dispatcher(storage=MemoryStorage()),
        engines=build_registry(settings),
    )
    try:
        yield app
    finally:
        await app.close()


def _payload(update_id: int, text: str = "/voice привет") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": 7, "type": "private"},
            "from": {"id": 7, "is_bot": False, "first_name": "T"},
            "text": text,
        },
    }


def _record(application: Application, calls: list[int]) -> None:
    """Replace dispatch with a counter so a second run is visible."""

    async def _feed(_bot: Bot, update: Any) -> None:
        calls.append(update.update_id)

    application.dispatcher.feed_update = _feed  # type: ignore[method-assign]


def _client(application: Application) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=create_app(application)),
        base_url="http://test",
    )


def _counter(outcome: str) -> float:
    value = REGISTRY.get_sample_value("tib_updates_total", {"outcome": outcome})
    return 0.0 if value is None else value


# --------------------------------------------------------------------
# the claim
# --------------------------------------------------------------------


async def test_a_resent_update_is_dispatched_once(application: Application) -> None:
    """The bug, in its plainest form: same id twice, one execution."""
    calls: list[int] = []
    _record(application, calls)

    async with _client(application) as client:
        first = await client.post("/webhook", json=_payload(9001))
        second = await client.post("/webhook", json=_payload(9001))

    assert first.status_code == 200
    # 200, not 500: a resend is not an error, and answering anything
    # else would keep Telegram retrying the update forever.
    assert second.status_code == 200
    assert calls == [9001]


async def test_a_new_update_still_gets_dispatched(application: Application) -> None:
    """Guard the guard — the plumbing above can still say yes."""
    calls: list[int] = []
    _record(application, calls)

    async with _client(application) as client:
        for update_id in (9101, 9102, 9103):
            assert (await client.post("/webhook", json=_payload(update_id))).status_code == 200

    assert calls == [9101, 9102, 9103]


async def test_a_resend_during_the_first_run_is_suppressed(
    application: Application,
) -> None:
    """The scenario that actually happens in production.

    Telegram does not resend because the first delivery *finished* with
    a bad status — it resends because the first delivery is taking too
    long and it is still running. A claim recorded after dispatch would
    be recorded on the far side of exactly this window.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def _slow(_bot: Bot, update: Any) -> None:
        calls.append(update.update_id)
        started.set()
        await release.wait()

    application.dispatcher.feed_update = _slow  # type: ignore[method-assign]

    async with _client(application) as client:
        in_flight = asyncio.create_task(client.post("/webhook", json=_payload(9201)))
        await asyncio.wait_for(started.wait(), timeout=5)

        # The resend lands while the first one is still inside the
        # handler and must come back without waiting on it.
        resend = await asyncio.wait_for(client.post("/webhook", json=_payload(9201)), timeout=5)
        assert resend.status_code == 200
        assert calls == [9201]

        release.set()
        assert (await in_flight).status_code == 200

    assert calls == [9201]


async def test_the_dedup_window_expires(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claims are bounded in time, not held for the life of the process.

    Both offsets are written out rather than derived from
    ``_UPDATE_DEDUP_TTL_SECONDS``: a test that advances its own clock by
    whatever the constant happens to say would pass for any value of it,
    including one that never expires. The contract is stated
    independently — a claim outlives half an hour and does not outlive
    an hour — so changing the constant has to come with changing this.
    """
    clock = {"now": 1000.0}
    monkeypatch.setattr(server_module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    calls: list[int] = []
    _record(application, calls)

    async with _client(application) as client:
        await client.post("/webhook", json=_payload(9301))
        await client.post("/webhook", json=_payload(9301))
        assert calls == [9301]

        clock["now"] += 1800.0
        await client.post("/webhook", json=_payload(9301))
        assert calls == [9301], "a claim must survive Telegram's retry window"

        clock["now"] += 1801.0
        await client.post("/webhook", json=_payload(9301))

    assert calls == [9301, 9301]


async def test_the_duplicate_is_counted_as_its_own_outcome(
    application: Application,
) -> None:
    """Ops needs to see resends without inferring them from a gap."""
    _record(application, [])
    before_new = _counter("new")
    before_duplicate = _counter("duplicate")

    async with _client(application) as client:
        await client.post("/webhook", json=_payload(9401))
        await client.post("/webhook", json=_payload(9401))

    assert _counter("new") - before_new == 1
    assert _counter("duplicate") - before_duplicate == 1


# --------------------------------------------------------------------
# the release
# --------------------------------------------------------------------


async def test_a_dispatch_failure_releases_the_claim(
    application: Application,
) -> None:
    """The 500 branch asks for a retry; the guard must not eat it.

    ``webhook/server.py`` answers 500 on a dispatch failure precisely so
    Telegram sends the update again. If the failed attempt kept its
    claim, that retry would be suppressed and the update lost — the
    fix for a double-execution bug would have created a
    zero-execution one.
    """
    attempts: list[int] = []

    async def _flaky(_bot: Bot, update: Any) -> None:
        attempts.append(update.update_id)
        if len(attempts) == 1:
            raise RuntimeError("transient DB lock")

    application.dispatcher.feed_update = _flaky  # type: ignore[method-assign]

    async with _client(application) as client:
        failed = await client.post("/webhook", json=_payload(9501))
        assert failed.status_code == 500

        retried = await client.post("/webhook", json=_payload(9501))
        assert retried.status_code == 200

    assert attempts == [9501, 9501]


async def test_a_released_claim_is_not_a_free_pass(
    application: Application,
) -> None:
    """Releasing on failure re-arms the guard, it does not disable it.

    After the retry succeeds the id is claimed again, so a *third*
    delivery of the same update is still suppressed.
    """
    attempts: list[int] = []

    async def _flaky(_bot: Bot, update: Any) -> None:
        attempts.append(update.update_id)
        if len(attempts) == 1:
            raise RuntimeError("transient DB lock")

    application.dispatcher.feed_update = _flaky  # type: ignore[method-assign]

    async with _client(application) as client:
        await client.post("/webhook", json=_payload(9601))
        await client.post("/webhook", json=_payload(9601))
        third = await client.post("/webhook", json=_payload(9601))

    assert third.status_code == 200
    assert attempts == [9601, 9601]


# --------------------------------------------------------------------
# the primitive the claim leans on
# --------------------------------------------------------------------


def test_discard_drops_a_stored_key() -> None:
    cache: TTLLRUCache[int, bool] = TTLLRUCache(ttl=60.0, capacity=8)
    cache.put(1, True, now=0.0)
    assert cache.get(1, now=0.0) is True

    cache.discard(1)
    assert cache.get(1, now=0.0) is None


def test_discard_of_an_absent_key_is_silent() -> None:
    """The release path must not need to know if the claim survived.

    An entry can be evicted by capacity or expired by TTL between the
    claim and the release, and a ``KeyError`` there would turn a
    handled dispatch failure into an unhandled 500 from the error
    handler itself.
    """
    cache: TTLLRUCache[int, bool] = TTLLRUCache(ttl=60.0, capacity=8)
    cache.discard(404)  # must not raise
    assert cache.get(404, now=0.0) is None


# --------------------------------------------------------------------
# what the claim does NOT cover (#1976)
# --------------------------------------------------------------------


async def test_the_claim_does_not_survive_a_restart(application: Application) -> None:
    """The ledger is per-process, so a restart hands the resend through.

    ``create_app`` builds ``seen_updates`` as a local, so every process
    starts with an empty one. Telegram's retry window is minutes and
    ``runner/webhook.py`` cancels a straggler at twenty seconds on
    SIGTERM *so that* the update comes back — which means the redelivery
    lands on a ledger that has never heard of it.

    This test pins the gap rather than closing it: closing it needs a
    durable claim, and the money paths that cannot afford the gap
    (``webhook/payments.py``) already keep their own row in the
    database. What must not happen is the module docstring claiming
    otherwise, which is what
    :func:`test_the_module_docstring_names_the_restart_gap` guards.
    """
    calls: list[int] = []
    _record(application, calls)

    async with _client(application) as before_restart:
        assert (await before_restart.post("/webhook", json=_payload(9501))).status_code == 200
        assert (await before_restart.post("/webhook", json=_payload(9501))).status_code == 200

    assert calls == [9501], "within one process the claim holds"

    # The restart: a fresh ``create_app``, i.e. a fresh ledger.
    async with _client(application) as after_restart:
        assert (await after_restart.post("/webhook", json=_payload(9501))).status_code == 200

    assert calls == [9501, 9501], "a new process must not be assumed to remember the claim"


def test_the_module_docstring_names_the_restart_gap() -> None:
    """#1976: the prose must not promise what the cache cannot deliver.

    The docstring lists three reasons Telegram resends — a lost response
    packet, a restart mid-request, a handler that ran long — and then
    says the ``update_id`` claim is the answer. Two of the three are
    genuinely covered; the restart is not, and ``handlers/ai.py`` and
    ``handlers/voice_transcribe.py`` both already document that in their
    own comments. A reader who trusts this module instead of those two
    would add the next billed handler without its own guard.
    """
    doc = server_module.__doc__
    assert doc is not None
    assert "#1976" in doc, "the carve-out must be traceable to its issue"
    assert "NOT the restart" in doc, (
        "the docstring must say plainly which of the three causes the claim misses"
    )
    assert "dies with the process" in doc, "and why — the ledger is a local built inside create_app"
