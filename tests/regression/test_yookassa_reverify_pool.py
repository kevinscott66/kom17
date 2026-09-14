"""#1439 — the YooKassa reverify must not spend the shared thread pool.

``parse_event`` on this route re-asks the merchant API whether the
payment in the body is real, over the SDK's blocking ``requests``.
#228 moved that off the event loop, which is why the bot no longer
freezes outright — but it moved it onto the loop's *default* executor,
and that pool is not this route's to spend. Its other users are every
image the bot draws: the profile card (``handlers/profile.py:930``),
``/stats`` (``handlers/stats.py:275``), ``/chatstats``
(``handlers/chatstats.py:563``) and the guide site's renderer
(``cms/guide_site/router.py:325``).

What makes that a security problem rather than a capacity note:
YooKassa does not sign its webhooks, so ``verify_signature`` on this
route is a shop-credentials shape check and nothing more. Anyone who
knows the URL can post to it. Enough concurrent posts and every
default-executor thread is parked inside a merchant-API round trip
that the attacker controls the duration of — and the bot stops drawing
pictures, for everybody, at no cost to them.

The fix is a pool of its own, so the only thing a reverify flood can
starve is the next reverify. Three properties, tested here in the
order they matter:

* the render survives the flood — the damage stays on this route;
* the flood cannot buy more than :data:`_REVERIFY_WORKERS` threads,
  because a delivery whose deadline passed while it sat in the queue
  is cancelled out of that queue and never reaches the merchant API;
* teardown does not poison the next delivery — a shut-down executor
  refuses new work forever, so the pool has to be rebuildable.

The default executor is per-*loop*, not per-process, so the flood and
its victim have to run on the same one. That is why these tests drive
the ASGI app in-process through :class:`~httpx.ASGITransport` rather
than through ``TestClient``, which would run the app on a portal loop
of its own and hide the whole effect.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Final

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dishka import make_async_container
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from telegram_invite_bot.app import Application
from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    PaymentsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.services.currency_service import CurrencyService
from telegram_invite_bot.webhook import payments as payments_mod
from telegram_invite_bot.webhook.payments import (
    _REVERIFY_WORKERS,
    shutdown_reverify_pool,
)
from telegram_invite_bot.webhook.reverify_throttle import _PER_CLIENT_CAPACITY, ReverifyThrottle
from telegram_invite_bot.webhook.server import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

_USER: Final[int] = 42
_SHOP: Final[str] = "shop-1"
_SECRET: Final[str] = "yoo-secret"

#: Stand-in for the shared pool the images render in. Pinned small and
#: installed explicitly, because the real default executor is sized
#: ``min(32, cpu_count + 4)`` and a test that has to saturate *that*
#: would pass or fail depending on the machine it ran on.
_VICTIM_WORKERS: Final[int] = 2

#: Deliveries in the starvation flood. One more than the victim pool
#: can hold, so on the unfixed route the render queues behind a
#: reverify instead of behind another render.
_FLOOD: Final[int] = _VICTIM_WORKERS + 1

#: Far below the production 8 s: every reverify in these tests is
#: gated open until the assertion is done, so the timeout is only
#: what stops a failing run from hanging.
_TIMEOUT_S: Final[float] = 2.0

#: How long the render is allowed to take. It does no work at all, so
#: anything it spends is queueing — but the bound is generous because
#: a loaded CI box still has to start a thread.
_RENDER_BUDGET_S: Final[float] = 1.0


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:abc"),
        webhook=WebhookConfig(WEBHOOK_PATH="/webhook", WEBHOOK_SECRET_TOKEN=SecretStr("dummy")),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
        payments=PaymentsConfig(
            YOOKASSA_SHOP_ID=_SHOP,
            YOOKASSA_SECRET_KEY=SecretStr(_SECRET),
        ),
    )


@pytest.fixture(autouse=True)
def _fresh_pool() -> Iterator[None]:
    """One pool per test, and none left running afterwards.

    The pool is process-global on purpose, so without this a test that
    parks four threads on a gate would hand the next test a pool with
    no free workers and the bound assertions would read as starvation.
    """
    shutdown_reverify_pool()
    yield
    shutdown_reverify_pool()


@pytest.fixture(autouse=True)
def _offline_fx(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route prices roubles through the dollar fix before it parses.

    Left alone that is a live HTTP call, which would put a second,
    unrelated blocking step in front of the one under test. Stubbed to
    ``None`` the service falls back to its offline table.
    """

    async def _none(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(CurrencyService, "_fetch", _none)


@pytest.fixture
async def application(tmp_path: Path) -> AsyncIterator[Application]:
    settings = _settings(tmp_path)
    engines = build_registry(settings)
    for base, db in ((EconomyBase, DBName.ECONOMY), (UsersBase, DBName.USERS)):
        async with engines.engine(db).begin() as conn:
            await conn.run_sync(base.metadata.create_all)
    app = Application(
        container=make_async_container(),
        settings=settings,
        bot=Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML)),
        dispatcher=Dispatcher(storage=MemoryStorage()),
        engines=engines,
    )
    async with engines.session(DBName.ECONOMY)() as session:
        session.add(EconomyUser(user_id=_USER, balance=0, language="ru"))
        await session.commit()
    try:
        yield app
    finally:
        await app.close()


def _client(application: Application) -> AsyncClient:
    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    fastapi_app = create_app(application)
    fastapi_app.state.application.bot.send_message = _noop
    return AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test")


class _Reverify:
    """The merchant-API round trip, held open on command.

    ``threads`` is the whole point of the file: it records which OS
    thread each reverify actually ran on, so "its own pool" is
    something the tests observe rather than assume.
    """

    def __init__(self, *, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.threads: list[str] = []
        self._lock = threading.Lock()

    def find_one(self, payment_id: str) -> Any:
        with self._lock:
            self.threads.append(threading.current_thread().name)
        if self.gate is not None:
            # Generous: the gate is always opened by the test before it
            # finishes, so this only bounds a run that already failed.
            self.gate.wait(30.0)
        return types.SimpleNamespace(
            status="succeeded",
            amount=types.SimpleNamespace(value="499.00", currency="RUB"),
            metadata={"user_id": str(_USER), "coins": "4990"},
        )

    @property
    def entered(self) -> int:
        with self._lock:
            return len(self.threads)

    @property
    def distinct_threads(self) -> set[str]:
        with self._lock:
            return set(self.threads)


class _FrozenClockThrottle(ReverifyThrottle):
    """The production limiter, with every delivery seen at the same instant."""

    __slots__ = ()

    def admit(self, client: str, *, now: float) -> bool:
        del now
        return super().admit(client, now=1000.0)


def _install(monkeypatch: pytest.MonkeyPatch, reverify: _Reverify) -> None:
    """Put the fake SDK where the adapter imports it from."""
    fake = types.ModuleType("yookassa")

    class _Configuration:
        account_id: str = ""
        secret_key: str = ""

    class _Payment:
        find_one = staticmethod(reverify.find_one)

    fake.Configuration = _Configuration  # type: ignore[attr-defined]
    fake.Payment = _Payment  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yookassa", fake)


def _payload(payment_id: str) -> dict[str, Any]:
    return {
        "event": "payment.succeeded",
        "object": {
            "id": payment_id,
            "status": "succeeded",
            "amount": {"value": "499.00", "currency": "RUB"},
            "metadata": {"user_id": str(_USER), "coins": "4990"},
        },
    }


async def _until(predicate: Any, budget: float = 5.0) -> bool:
    """Wait for a condition a worker thread sets, without a sleep loop in it."""
    deadline = asyncio.get_running_loop().time() + budget
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


# --------------------------------------------------------------------
# the damage stays on this route
# --------------------------------------------------------------------


async def test_a_reverify_flood_does_not_starve_the_image_renderer(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug, with the victim present.

    ``asyncio.to_thread`` here is not a stand-in for the renderers: it
    is the exact call they make. The flood is three unauthenticated
    POSTs whose reverify never returns — and on the unfixed route that
    is enough to put every thread the renderers have inside the
    attacker's round trip, with a fourth request queued behind them.
    """
    monkeypatch.setattr(payments_mod, "_YOOKASSA_REVERIFY_TIMEOUT_S", _TIMEOUT_S)
    gate = threading.Event()
    reverify = _Reverify(gate=gate)
    _install(monkeypatch, reverify)

    victim = ThreadPoolExecutor(max_workers=_VICTIM_WORKERS, thread_name_prefix="shared")
    asyncio.get_running_loop().set_default_executor(victim)

    try:
        async with _client(application) as client:
            flood = [
                asyncio.ensure_future(client.post("/yookassa-webhook", json=_payload(f"PAY-{i}")))
                for i in range(_FLOOD)
            ]
            assert await _until(lambda: reverify.entered >= _VICTIM_WORKERS), (
                "the flood never reached the merchant API — the test proves nothing"
            )

            rendered = await asyncio.wait_for(asyncio.to_thread(lambda: "png"), _RENDER_BUDGET_S)
            assert rendered == "png"

            gate.set()
            await asyncio.gather(*flood)
    finally:
        gate.set()
        victim.shutdown(wait=False)


async def test_the_reverify_runs_on_a_thread_of_its_own(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structural half: name the pool the work landed in.

    The timing test above can only fail when the flood actually
    interleaves. This one fails the moment the call goes back to a
    shared executor, whatever the load happens to be.
    """
    reverify = _Reverify()
    _install(monkeypatch, reverify)

    async with _client(application) as client:
        response = await client.post("/yookassa-webhook", json=_payload("PAY-SOLO"))

    assert response.status_code == 200
    assert reverify.threads, "the reverify never ran"
    assert all(name.startswith("yookassa-reverify") for name in reverify.threads), (
        f"the reverify ran on somebody else's threads: {reverify.threads}"
    )


# --------------------------------------------------------------------
# the flood cannot buy more than the pool is wide
# --------------------------------------------------------------------


async def test_a_queued_delivery_is_dropped_instead_of_reverified(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three times the pool's width, and only its width gets a thread.

    The rest sit in the queue until their deadline passes, and
    cancelling an executor future that has not started yet means the
    work is never run at all — so an attacker's surplus deliveries do
    not become surplus merchant-API traffic later, they simply never
    happen.
    """
    monkeypatch.setattr(payments_mod, "_YOOKASSA_REVERIFY_TIMEOUT_S", _TIMEOUT_S)
    gate = threading.Event()
    reverify = _Reverify(gate=gate)
    _install(monkeypatch, reverify)

    surplus = _REVERIFY_WORKERS * 3
    try:
        async with _client(application) as client:
            responses = await asyncio.gather(
                *(
                    client.post("/yookassa-webhook", json=_payload(f"PAY-Q{i}"))
                    for i in range(surplus)
                )
            )
    finally:
        gate.set()

    # Every one of them asked to be redelivered — nothing was credited
    # on a reverify that did not finish (#1610).
    assert {r.status_code for r in responses} == {503}
    assert reverify.entered <= _REVERIFY_WORKERS, (
        f"{reverify.entered} of {surplus} deliveries reached the merchant API "
        f"through a pool {_REVERIFY_WORKERS} threads wide"
    )
    assert len(reverify.distinct_threads) <= _REVERIFY_WORKERS


# --------------------------------------------------------------------
# and a flood long enough is refused before it reaches the pool at all
# --------------------------------------------------------------------


async def test_a_flood_past_the_budget_is_refused_before_the_merchant_api(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool caps concurrency; the limiter caps the bill.

    Everything above is about one burst. A flood that keeps going is a
    different shape: each delivery waits its turn politely and every one
    of them still becomes an outbound call to the acquirer, forever, at
    whatever rate the attacker likes. So the surplus is refused in front
    of the pool — 429, which YooKassa redelivers for 24 hours, and no
    thread and no round trip spent on it.
    """
    reverify = _Reverify()
    _install(monkeypatch, reverify)
    # The webhook reads the wall clock, and a slow runner spends long
    # enough on the burst for the refill to admit one more delivery.
    # The limiter's arithmetic is not under test here, so stop its clock.
    monkeypatch.setattr(payments_mod, "_REVERIFY_THROTTLE", _FrozenClockThrottle())

    over = int(_PER_CLIENT_CAPACITY) + 5
    async with _client(application) as client:
        codes = [
            (await client.post("/yookassa-webhook", json=_payload(f"PAY-F{i}"))).status_code
            for i in range(over)
        ]

    assert codes.count(200) == int(_PER_CLIENT_CAPACITY)
    assert codes.count(429) == over - int(_PER_CLIENT_CAPACITY)
    assert reverify.entered == int(_PER_CLIENT_CAPACITY), (
        "a refused delivery still reached the merchant API"
    )


# --------------------------------------------------------------------
# teardown must not poison the next delivery
# --------------------------------------------------------------------


async def test_a_delivery_after_teardown_builds_a_fresh_pool(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``shutdown`` is permanent for an executor, so the global is dropped.

    One process hosts many applications in this suite, and in
    production a reload does the same thing on a smaller scale. A pool
    kept past its own shutdown would answer every later delivery with
    ``RuntimeError`` — which the route would spell as an uncredited
    payment.
    """
    reverify = _Reverify()
    _install(monkeypatch, reverify)

    shutdown_reverify_pool()
    shutdown_reverify_pool()  # idempotent: teardown runs twice in tests

    async with _client(application) as client:
        response = await client.post("/yookassa-webhook", json=_payload("PAY-AFTER"))

    assert response.status_code == 200
    assert reverify.entered == 1
