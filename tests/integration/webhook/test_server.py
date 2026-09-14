"""End-to-end webhook server: httpx AsyncClient drives the FastAPI app.

T-011 (2026-05-26) removed the legacy ``TelebotFallback``; updates now
go through aiogram's dispatcher exclusively. Tests that previously
asserted "fallback was/was not called" become "dispatch was/was not
attempted" — the security/back-pressure contracts (oversize bodies,
bad JSON, missing secret) are unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dishka import make_async_container
from httpx import ASGITransport, AsyncClient
from loguru import logger
from pydantic import SecretStr

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
from telegram_invite_bot.services.payments.fx import FX_TIMEOUT_SECONDS
from telegram_invite_bot.webhook.server import _build_fx_service, create_app


def _settings(tmp_path: Path, *, secret: str | None, origin: str = "") -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:abc"),
        webhook=WebhookConfig(
            WEBHOOK_PATH="/webhook",
            WEBHOOK_URL=origin,
            WEBHOOK_SECRET_TOKEN=SecretStr(secret) if secret is not None else None,
        ),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )


async def _stamp_schemas(engines: Any) -> None:
    """Give every DB the one table a migrated deployment always has.

    ``/readyz`` has probed for a schema since #682, so a fixture that
    leaves five empty files behind is no longer a stand-in for a live
    deployment — it is the exact misconfiguration the probe exists to
    catch. ``alembic_version`` is what ``alembic stamp`` writes, and it
    is the only table present in all five: ``ActivityBase`` maps no
    models at all, so ``create_all`` could not have covered activity.db.
    """
    from sqlalchemy import text as sa_text

    from telegram_invite_bot.db.names import ALL_DBS

    for db in ALL_DBS:
        engine = engines.engine(db)
        async with engine.begin() as conn:
            await conn.execute(
                sa_text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))")
            )


async def _build(
    tmp_path: Path, *, secret: str | None = None, origin: str = "", stamp: bool = True
) -> Application:
    settings = _settings(tmp_path, secret=secret, origin=origin)
    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    engines = build_registry(settings)
    if stamp:
        await _stamp_schemas(engines)
    container = make_async_container()  # empty — tests don't resolve from it
    return Application(
        container=container,
        settings=settings,
        bot=bot,
        dispatcher=dispatcher,
        engines=engines,
    )


@pytest.fixture
async def application(tmp_path: Path) -> AsyncIterator[Application]:
    app = await _build(tmp_path)
    try:
        yield app
    finally:
        await app.close()


@pytest.fixture
async def secured_application(tmp_path: Path) -> AsyncIterator[Application]:
    app = await _build(tmp_path, secret="topsecret")
    try:
        yield app
    finally:
        await app.close()


def _msg_payload() -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "T"},
            "text": "/ping",
        },
    }


async def _client(app: Application) -> AsyncClient:
    fastapi_app = create_app(app)
    # ASGITransport drives the lifespan manually; we wrap in
    # ``LifespanManager`` only if we want startup events to fire — for
    # these tests the routes don't depend on lifespan state.
    return AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test")


async def test_healthz_is_pure_liveness_probe(application: Application) -> None:
    """M-I-7: ``/healthz`` is liveness only — returns 200 as long as
    the event loop answers, never touches the DB. A transient DB lock
    used to flip ``/healthz`` to 503 and bounce the pod; readiness
    moved to ``/readyz`` so liveness stays cheap.
    """
    async with await _client(application) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload == {"status": "ok"}


async def test_readyz_returns_anonymised_db_slots(application: Application) -> None:
    """M-I-7: readiness body uses ``db1`` .. ``dbN`` slot names — never
    the concrete file names — so the response can't be used to
    enumerate the deployment's storage layout.
    """
    async with await _client(application) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    # Slot names only — none of the real DB file names leak.
    assert all(k.startswith("db") and k[2:].isdigit() for k in payload["databases"])
    forbidden = {"users", "economy", "activity", "moderation", "message_stats"}
    assert forbidden.isdisjoint(payload["databases"].keys())
    assert payload["ready"] == payload["total"]
    assert all(payload["databases"].values())


async def test_readyz_returns_503_when_a_db_is_broken(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single failing DB demotes ``/readyz`` to 503 and bumps
    ``tib_health_check_failures_total{probe="readyz"}``. Production
    triage relies on the orchestrator pulling the pod out of rotation
    rather than serving users with a half-working DB layer.
    """
    from telegram_invite_bot.db.names import DBName
    from telegram_invite_bot.webhook import health as health_module
    from telegram_invite_bot.webhook import server as server_module
    from telegram_invite_bot.webhook.metrics import HEALTH_CHECK_FAILURES

    real_check = health_module.check_databases

    async def broken_check(registry: Any) -> dict[DBName, bool]:
        results = await real_check(registry)
        results[DBName.ECONOMY] = False
        return results

    monkeypatch.setattr(health_module, "check_databases", broken_check)

    before = HEALTH_CHECK_FAILURES.labels(probe="readyz")._value.get()

    async with await _client(application) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "degraded"
    # Body remains anonymised even in the failure case.
    assert "economy" not in payload["databases"]
    assert "users" not in payload["databases"]
    assert any(v is False for v in payload["databases"].values())

    after = HEALTH_CHECK_FAILURES.labels(probe="readyz")._value.get()
    assert after == before + 1

    # Silence the unused-import warning while keeping the symbol
    # documented as the patch target for server-side callers.
    _ = server_module


async def test_readyz_is_503_when_a_db_file_has_no_schema(tmp_path: Path) -> None:
    """#682, end-to-end against real SQLite files.

    No monkeypatching here — this is the production failure verbatim: a
    wrong ``DATABASE_DIR`` (or an unmounted volume) means SQLite invents
    the files on first connect, every ``SELECT 1`` succeeds, and the old
    probe reported 200 while the bot was one query away from ``no such
    table``. The fixture above stamps ``alembic_version`` precisely so
    the green case stays green; this application deliberately skips it.
    """
    app = await _build(tmp_path, stamp=False)
    try:
        async with await _client(app) as client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "degraded"
        assert payload["ready"] == 0
        assert not any(payload["databases"].values())
    finally:
        await app.close()


async def test_managed_telegram_webhook_drives_setup_and_teardown(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``manage_telegram_webhook=True`` ⇒ the lifespan calls Telegram
    ``setWebhook`` on startup, and ``deleteWebhook`` on shutdown when
    ``unregister_webhook_on_shutdown`` is asked for as well (#1568 —
    the two are separate flags now, and production only takes the
    first).

    The default (off) is what every other test uses so unit suites
    don't hit the network. That left the *managed* branch — the only
    one production ever runs — uncovered. We monkeypatch
    ``setup_webhook`` / ``teardown_webhook`` so the assertion is
    "lifecycle hooks fired in order", not "real Telegram API works".

    The lifespan is driven manually via the ASGI lifespan protocol so
    we don't have to add ``asgi_lifespan`` as a dep just for this case.
    """
    from telegram_invite_bot.webhook import lifespan as lifespan_module

    calls: list[str] = []

    async def fake_setup(app: Application) -> None:
        calls.append("setup")

    async def fake_teardown(app: Application) -> None:
        calls.append("teardown")

    monkeypatch.setattr(lifespan_module, "setup_webhook", fake_setup)
    monkeypatch.setattr(lifespan_module, "teardown_webhook", fake_teardown)

    fastapi_app = create_app(
        application, manage_telegram_webhook=True, unregister_webhook_on_shutdown=True
    )

    # Drive the ASGI lifespan protocol by hand:
    # startup → app expects ``lifespan.startup`` then sends
    # ``lifespan.startup.complete``; shutdown is the mirror.
    from collections.abc import MutableMapping

    sent: list[MutableMapping[str, Any]] = []
    inbox: list[MutableMapping[str, Any]] = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]

    async def receive() -> MutableMapping[str, Any]:
        return inbox.pop(0)

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await fastapi_app({"type": "lifespan"}, receive, send)

    assert calls == ["setup", "teardown"]
    # ASGI contract sanity: app must signal completion for both events.
    sent_types = [m["type"] for m in sent]
    assert "lifespan.startup.complete" in sent_types
    assert "lifespan.shutdown.complete" in sent_types


async def test_unmanaged_lifespan_yields_without_touching_telegram(
    application: Application,
) -> None:
    """Mirror of the managed-lifespan test: with the default
    ``manage_telegram_webhook=False`` the lifespan must skip both
    setup/teardown entirely and still complete cleanly. Locks the
    ``else: yield`` branch so a regression that drops it (and
    silently no-ops the whole lifespan) is caught.
    """
    from collections.abc import MutableMapping

    fastapi_app = create_app(application)  # default: not managed

    sent: list[MutableMapping[str, Any]] = []
    inbox: list[MutableMapping[str, Any]] = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]

    async def receive() -> MutableMapping[str, Any]:
        return inbox.pop(0)

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await fastapi_app({"type": "lifespan"}, receive, send)

    sent_types = [m["type"] for m in sent]
    assert sent_types == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


async def _drive_lifespan(fastapi_app: Any) -> list[str]:
    """Run one full ASGI startup→shutdown cycle, return the event types.

    The ASGI lifespan protocol is the only teardown hook uvicorn calls
    while its SIGTERM handler is still installed, so #279's fix lives
    there and its tests have to drive it directly.
    """
    from collections.abc import MutableMapping

    sent: list[MutableMapping[str, Any]] = []
    inbox: list[MutableMapping[str, Any]] = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]

    async def receive() -> MutableMapping[str, Any]:
        return inbox.pop(0)

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    await fastapi_app({"type": "lifespan"}, receive, send)
    return [str(m["type"]) for m in sent]


async def test_lifespan_closes_the_application_when_asked(
    application: Application,
) -> None:
    """``close_application=True`` ⇒ shutdown tears the graph down (#279).

    This is the whole point of the flag. The webhook runner used to
    rely on ``finally: await application.close()`` around
    ``server.serve()``, but uvicorn's ``capture_signals`` restores
    ``SIG_DFL`` and re-raises the captured SIGTERM as ``serve()``
    unwinds, so that ``finally`` never executed on a real
    ``systemctl stop``: the FSM storage, the bot session, every SQLite
    engine and the DI container were all left to the process dying.

    Asserted on ``_closed`` rather than a monkeypatched ``close`` —
    :class:`Application` is a ``slots=True`` dataclass, so per-instance
    patching is not even possible, and the latch is the same thing the
    idempotence guard reads.
    """
    fastapi_app = create_app(application, close_application=True)
    types = await _drive_lifespan(fastapi_app)

    assert application._closed is True
    assert types == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


async def test_lifespan_leaves_the_application_alone_by_default(
    application: Application,
) -> None:
    """The default must stay ``False``.

    Two dozen test call sites build a throwaway app, drive requests
    through it and close the graph themselves; a factory that closed it
    from under them would tear down the engines mid-test.
    """
    fastapi_app = create_app(application)  # default
    await _drive_lifespan(fastapi_app)

    assert application._closed is False


async def test_lifespan_closes_the_application_even_if_teardown_webhook_raises(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Telegram outage must not cost us the graph teardown (#279).

    ``deleteWebhook`` is a network call and Telegram is allowed to be
    down while we are shutting down. Losing every SQLite handle and the
    DI container because of that would be strictly the worse failure,
    so the two steps are nested rather than sequential.
    """
    from telegram_invite_bot.webhook import lifespan as lifespan_module

    calls: list[str] = []

    async def fake_setup(app: Application) -> None:
        calls.append("setup")

    async def fake_teardown(app: Application) -> None:
        # Ordering proof: deleteWebhook travels on the bot session that
        # ``close()`` shuts, so it has to run while the graph is still
        # open.
        assert app._closed is False
        calls.append("teardown")
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(lifespan_module, "setup_webhook", fake_setup)
    monkeypatch.setattr(lifespan_module, "teardown_webhook", fake_teardown)

    fastapi_app = create_app(
        application,
        manage_telegram_webhook=True,
        close_application=True,
        unregister_webhook_on_shutdown=True,
    )

    with pytest.raises(RuntimeError, match="telegram is down"):
        await _drive_lifespan(fastapi_app)

    assert calls == ["setup", "teardown"]
    assert application._closed is True


async def test_failed_startup_still_tears_the_application_down(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A startup that raises half-way must release what it acquired.

    Before #279 the teardown ``try`` opened *after* ``setup_webhook``,
    so a setWebhook that exhausted its retries left the background
    tasks running and the graph open until the process was killed.

    What it must NOT release is Telegram's webhook registration
    (#1394). ``setup_webhook`` continues startup on the previous
    deploy's live registration when Telegram is briefly unreachable,
    and that check reads ``getWebhookInfo`` — so it can only ever match
    if the failing process left the registration alone. Deleting it
    here blanked the URL and drove systemd into the very restart loop
    the fallback exists to prevent.
    """
    from telegram_invite_bot.webhook import lifespan as lifespan_module

    calls: list[str] = []

    async def exploding_setup(app: Application) -> None:
        calls.append("setup")
        raise RuntimeError("setWebhook exhausted its retries")

    async def fake_teardown(app: Application) -> None:
        calls.append("teardown")

    monkeypatch.setattr(lifespan_module, "setup_webhook", exploding_setup)
    monkeypatch.setattr(lifespan_module, "teardown_webhook", fake_teardown)

    fastapi_app = create_app(application, manage_telegram_webhook=True, close_application=True)

    with pytest.raises(RuntimeError, match="setWebhook exhausted"):
        await _drive_lifespan(fastapi_app)

    assert calls == ["setup"]
    assert application._closed is True


async def test_start_background_failure_surfaces_its_own_error(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1468 — the first startup step must not be masked by teardown.

    ``startup_complete`` used to be bound *after* ``start_background``,
    while the ``finally`` below reads it unconditionally. A failure in
    that first step therefore left the teardown raising
    ``UnboundLocalError``, which is what reached the journal instead of
    the real cause — and systemd restarts every ten seconds, so the
    wrong error is what an operator would have chased.

    The flag being false also has to keep meaning what it means: a
    startup that never registered the webhook must not unregister the
    previous deploy's registration on the way out (#1394).
    """
    from telegram_invite_bot.webhook import lifespan as lifespan_module

    calls: list[str] = []

    async def exploding_start(*_args: Any, **_kwargs: Any) -> None:
        calls.append("start_background")
        raise RuntimeError("economy_cleanup import blew up")

    async def fake_setup(app: Application) -> None:
        calls.append("setup")

    async def fake_teardown(app: Application) -> None:
        calls.append("teardown")

    # Patched on the class: ``Application`` uses ``__slots__``, so the
    # instance refuses a new binding for a method name.
    monkeypatch.setattr(type(application), "start_background", exploding_start)
    monkeypatch.setattr(lifespan_module, "setup_webhook", fake_setup)
    monkeypatch.setattr(lifespan_module, "teardown_webhook", fake_teardown)

    fastapi_app = create_app(application, manage_telegram_webhook=True, close_application=True)

    with pytest.raises(RuntimeError, match="economy_cleanup import blew up"):
        await _drive_lifespan(fastapi_app)

    assert calls == ["start_background"]
    assert application._closed is True


async def test_metrics_endpoint_exposes_prometheus(application: Application) -> None:
    async with await _client(application) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert "tib_updates_total" in response.text


async def test_webhook_accepts_update(application: Application) -> None:
    """Smoke test: valid update payload → 200. With the empty
    dispatcher fixture there's no handler to match, but the dispatch
    call still succeeds (aiogram silently no-ops unmatched updates)."""
    async with await _client(application) as client:
        response = await client.post("/webhook", json=_msg_payload())
    assert response.status_code == 200


async def test_webhook_returns_500_when_dispatch_raises(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatcher exceptions must surface as 500 so Telegram retries.

    R-FIX-005: pre-fix behaviour was to ack 200 on ANY exception escaping
    ``dispatcher.feed_update`` — but the per-handler error router
    (``handlers/errors.py``) already absorbs handler exceptions and
    returns ``True``. Anything still escaping ``feed_update`` is an
    aiogram-internal failure (middleware bug, router corruption) or a
    transient infra fault (DB lock that the error router itself
    couldn't recover from). Acking 200 in that case silently dropped
    the update — Telegram considers it delivered and never retries.
    Now we return 500 so Telegram's exponential-backoff retry kicks in,
    preserving at-least-once semantics.
    """
    from telegram_invite_bot.webhook.metrics import UPDATES_TOTAL

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("simulated dispatch failure")

    monkeypatch.setattr(application.dispatcher, "feed_update", boom)

    dispatch_error_counter = UPDATES_TOTAL.labels(outcome="dispatch_error")
    before = dispatch_error_counter._value.get()

    async with await _client(application) as client:
        response = await client.post("/webhook", json=_msg_payload())

    assert response.status_code == 500
    assert dispatch_error_counter._value.get() == before + 1


async def test_webhook_returns_200_when_parse_raises(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Update.model_validate`` failures must still ack 200.

    R-FIX-005 differentiates parse errors (permanent — the payload
    itself is wrong) from dispatch errors (transient). A schema drift
    where Telegram sends a new update type our pydantic models don't
    know about would otherwise loop forever: every retry parses the
    same way and fails the same way. We log + bump the
    ``parse_error`` counter and ack 200 so the poison payload is
    discarded, not amplified.
    """
    from telegram_invite_bot.webhook import server as server_module
    from telegram_invite_bot.webhook.metrics import UPDATES_TOTAL

    class _BadUpdate:
        @staticmethod
        def model_validate(*_args: Any, **_kwargs: Any) -> Any:
            raise ValueError("simulated parse failure")

    monkeypatch.setattr(server_module, "Update", _BadUpdate)

    parse_error_counter = UPDATES_TOTAL.labels(outcome="parse_error")
    before = parse_error_counter._value.get()

    async with await _client(application) as client:
        response = await client.post("/webhook", json=_msg_payload())

    assert response.status_code == 200
    assert parse_error_counter._value.get() == before + 1


async def test_webhook_rejects_invalid_json(application: Application) -> None:
    async with await _client(application) as client:
        response = await client.post(
            "/webhook",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 400


async def test_webhook_rejects_oversized_payload(application: Application) -> None:
    """Content-Length above the 1MB ceiling → 413, without ever parsing
    the body. Defense-in-depth on top of the secret-token check — if a
    secret ever leaks, an attacker still can't make us allocate
    arbitrary memory by POSTing a multi-GB JSON.
    """
    async with await _client(application) as client:
        # We don't actually need to send 2MB; the header alone trips
        # the gate. httpx requires a body matching Content-Length, so
        # use a tiny body + manual header override.
        response = await client.post(
            "/webhook",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": str(2 * 1024 * 1024),  # 2MB, > 1MB ceiling
            },
        )
    assert response.status_code == 413


async def test_webhook_rejects_non_integer_content_length(
    application: Application,
) -> None:
    """A malformed Content-Length (``"abc"``) must surface as 400, not
    as a 500 from an unhandled ``ValueError`` deeper in the stack.
    """
    async with await _client(application) as client:
        response = await client.post(
            "/webhook",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": "not-a-number",
            },
        )
    # httpx may strip a malformed Content-Length client-side before
    # send; treat either 400 (gate caught it) or 200 (header dropped,
    # body went through normally) as acceptable — both are non-500.
    # The point is no internal-server-error.
    assert response.status_code in (200, 400)


async def test_webhook_rejects_oversized_streaming_payload(
    application: Application,
) -> None:
    """Streaming check (the real enforcement): a client that omits
    ``Content-Length`` and chunk-streams a giant body must still be
    cut off at the 1MB ceiling, not buffered into memory until it
    OOMs the process. This is the attack the Content-Length-only
    guard could not catch — a chunked-encoded request never sets CL.

    The check happens inside ``async for chunk in request.stream()``
    so the response is 413 emitted mid-read, before ``json.loads`` ever
    sees the bytes.
    """
    big_body = b'{"x":"' + b"A" * (2 * 1024 * 1024) + b'"}'  # ~2MB
    async with await _client(application) as client:
        # Send without Content-Length by using a generator body —
        # httpx switches to chunked transfer encoding automatically.
        async def _gen() -> AsyncIterator[bytes]:
            yield big_body

        response = await client.post(
            "/webhook",
            content=_gen(),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413


async def test_webhook_stream_read_failure_yields_400(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If reading the request stream raises mid-flight (a torn TCP
    connection, a malformed chunk frame), the handler must surface a
    400, not let the exception escape as an unhandled 500. The legacy
    Flask path swallowed these silently; we want a clear category in
    the metrics counter.
    """
    from starlette.requests import Request

    async def _bad_stream(self: Request) -> AsyncIterator[bytes]:
        yield b"{"
        raise RuntimeError("simulated torn connection")

    monkeypatch.setattr(Request, "stream", _bad_stream)

    async with await _client(application) as client:
        response = await client.post("/webhook", json={"update_id": 1})
    assert response.status_code == 400


async def test_webhook_rejects_non_object_payload(application: Application) -> None:
    """A valid-JSON-but-not-an-object payload (e.g. a list) must 400.
    Without the explicit guard, ``payload.get("update_id")`` would raise
    ``AttributeError`` and we'd serve a confusing 500 to whatever
    well-meaning client got the schema wrong.
    """
    async with await _client(application) as client:
        response = await client.post("/webhook", json=[1, 2, 3])
    assert response.status_code == 400


async def test_webhook_rejects_missing_secret(secured_application: Application) -> None:
    async with await _client(secured_application) as client:
        response = await client.post("/webhook", json=_msg_payload())
    assert response.status_code == 403


async def test_webhook_counts_a_rejected_secret_as_forbidden(
    secured_application: Application,
) -> None:
    """#820: a 403 has to move a counter, or alerting cannot see it.

    Every other refusal in this route bumps ``UPDATES_TOTAL``; this one
    did not, so the single failure that stops the bot dead — a secret
    rotation that leaves Telegram 403-ing every real update — reached
    Prometheus as nothing at all, indistinguishable from a quiet night.
    ``forbidden`` is its own outcome rather than ``error`` because the
    two want different alerts: this one is either a forger who learned
    the URL, or a config mistake, and never a malformed payload.
    """
    from telegram_invite_bot.webhook.metrics import UPDATES_TOTAL

    forbidden_counter = UPDATES_TOTAL.labels(outcome="forbidden")
    before = forbidden_counter._value.get()

    async with await _client(secured_application) as client:
        response = await client.post(
            "/webhook",
            json=_msg_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )

    assert response.status_code == 403
    assert forbidden_counter._value.get() == before + 1


async def _build_with_main_router(tmp_path: Path) -> Application:
    """Build an Application with the real main router
    (start/weather/profile/help/economy) wired through the dispatcher —
    same way ``di/providers.py`` does it in prod. This is the
    integration backstop for the ported handlers."""
    from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
    from telegram_invite_bot.db.names import DBName
    from telegram_invite_bot.middlewares.session import SessionMiddleware
    from telegram_invite_bot.middlewares.throttling import ThrottlingMiddleware
    from telegram_invite_bot.routers.main_router import build_main_router

    settings = Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:abc"),
        webhook=WebhookConfig(WEBHOOK_PATH="/webhook"),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )
    engines = build_registry(settings)
    # Schemas for the DBs the ported handlers actually touch.
    for base, db in ((UsersBase, DBName.USERS), (EconomyBase, DBName.ECONOMY)):
        engine = engines.engine(db)
        async with engine.begin() as conn:
            await conn.run_sync(base.metadata.create_all)

    bot = Bot(token="123:abc", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.message.outer_middleware(SessionMiddleware(engines))
    throttle = ThrottlingMiddleware(settings.throttling)
    dispatcher.include_router(
        build_main_router(
            engines,
            settings,
            throttle=throttle,
            get_dispatcher=lambda: dispatcher,
        )
    )
    container = make_async_container()
    return Application(
        container=container,
        settings=settings,
        bot=bot,
        dispatcher=dispatcher,
        engines=engines,
    )


@pytest.fixture
async def live_application(tmp_path: Path) -> AsyncIterator[Application]:
    app = await _build_with_main_router(tmp_path)
    try:
        yield app
    finally:
        await app.close()


async def test_smoke_ported_command_routes_through_dispatcher(
    live_application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the real main router wired, a private ``/start`` must be
    served by the new code path and produce a reply. This is the
    integration backstop for every ported handler: if any of them
    silently breaks the routing (e.g. forgets to register on the
    router, or the dispatcher wiring drifts), this test fails.
    """
    sent: list[dict[str, Any]] = []

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "SendMessage":
            sent.append({"text": method.text})
            # Synthesise the response shape aiogram expects.
            from datetime import datetime

            from aiogram.types import Chat, Message
            from aiogram.types import User as TelegramUser

            return Message(
                message_id=1,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        raise AssertionError(f"unexpected Telegram call: {type(method).__name__}")

    monkeypatch.setattr(live_application.bot.session, "make_request", fake_make_request)

    payload = {
        "update_id": 42,
        "message": {
            "message_id": 1,
            "date": 1_700_000_000,
            "chat": {"id": 4242, "type": "private"},
            "from": {
                "id": 4242,
                "is_bot": False,
                "first_name": "Smoke",
                "language_code": "ru",
            },
            "text": "/start",
        },
    }
    async with await _client(live_application) as client:
        response = await client.post("/webhook", json=payload)
    assert response.status_code == 200
    # Handler produced a reply.
    assert len(sent) == 1
    assert "Привет" in sent[0]["text"] or "С возвращением" in sent[0]["text"]


async def test_webhook_accepts_matching_secret(secured_application: Application) -> None:
    async with await _client(secured_application) as client:
        response = await client.post(
            "/webhook",
            json=_msg_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "topsecret"},
        )
    assert response.status_code == 200


# --------------------------------------------------------------------
# T-020 R11 — the FX service behind the rouble top-up price.
# --------------------------------------------------------------------


def test_fx_service_shares_the_withdraw_anchor(application: Application) -> None:
    """Buy side and sell side must read off the same coin anchor.

    The webhook builds its own ``CurrencyService`` (the one in
    ``build_main_router`` lives in a closure it cannot reach), so the
    anchor is wired twice and could drift. If it ever did, a rouble
    deposit would mint coins at one price that the withdraw desk buys
    back at another — exactly the arbitrage R11 closed.
    """
    fx = _build_fx_service(application)
    assert fx._coins_per_usdt == application.settings.withdraw.coins_per_usdt


def test_fx_service_times_out_before_its_caller_gives_up(
    application: Application,
) -> None:
    """The service must lose the race to its own timeout, not the caller's.

    On its own timeout it falls back and *caches* for the full TTL, so a
    hanging upstream costs one short wait an hour. If the caller's
    ``wait_for`` fired first it would cancel the fetch mid-flight, the
    cache would stay empty, and every later webhook would pay the wait
    again.
    """
    fx = _build_fx_service(application)
    assert fx._timeout < FX_TIMEOUT_SECONDS
    assert fx._timeout <= application.settings.currency.timeout_seconds


# --------------------------------------------------------------------
# Guide site — the mounted page's call to action
# --------------------------------------------------------------------


async def test_guide_page_links_to_the_real_bot(tmp_path: Path) -> None:
    """The page's primary button is "open the bot in Telegram".

    ``create_app`` used to pass ``bot_username=None`` because the
    factory is synchronous and ``get_me`` is not — which silently
    shipped a button pointing at a bare ``https://t.me``, a dead end
    for every visitor who tapped it. The bootstrap already calls
    ``get_me``; the username rides along on the ``Application``, and
    this asserts it survives the trip into the rendered page.
    """
    application = await _build(tmp_path)
    application.bot_username = "kom17bot"
    try:
        async with await _client(application) as client:
            body = (await client.get("/commands")).text
    finally:
        await application.close()
    assert "https://t.me/kom17bot" in body


# --------------------------------------------------------------------
# Legal documents — mounted unconditionally
# --------------------------------------------------------------------


async def test_legal_documents_are_served_by_the_real_app(tmp_path: Path) -> None:
    """All six pages, from the app the deploy actually runs.

    The unit tests build the router directly; this one proves the mount
    in ``create_app`` exists and is not behind the guide's feature flag —
    an acquiring bank's condition is that the documents stay reachable,
    so "someone set GUIDE_SITE_ENABLED=0" must not be able to take the
    offer offline.
    """
    application = await _build(tmp_path)
    application.bot_username = "kom17bot"
    try:
        async with await _client(application) as client:
            pages = {
                path: await client.get(path)
                for path in (
                    "/privacy",
                    "/privacy/en",
                    "/terms",
                    "/terms/en",
                    "/support",
                    "/support/en",
                )
            }
    finally:
        await application.close()

    assert [r.status_code for r in pages.values()] == [200] * 6
    # Six distinct documents, not one page served six times.
    assert len({r.text for r in pages.values()}) == 6
    assert "[[" not in "".join(r.text for r in pages.values())


# --------------------------------------------------------------------
# The 404 page — installed after the site's routers
# --------------------------------------------------------------------


async def test_a_miss_gets_the_page_and_a_webhook_client_keeps_json(tmp_path: Path) -> None:
    """The wiring guard for #182, from the app the deploy actually runs.

    The unit tests install the handler on a bare ``FastAPI``. This one
    proves ``create_app`` installs it at all, that it runs inside the
    middleware stack — so the page arrives with its own policy rather
    than the ``style-src 'none'`` fallback that would strip it to bare
    text — and, the half that would break silently, that a client which
    did not ask for HTML still receives the JSON body its tooling has
    always parsed.
    """
    application = await _build(tmp_path)
    application.bot_username = "kom17bot"
    try:
        async with await _client(application) as client:
            reader = await client.get("/en/privacy", headers={"Accept": "text/html"})
            program = await client.get("/en/privacy", headers={"Accept": "application/json"})
    finally:
        await application.close()

    assert reader.status_code == 404
    assert reader.headers["content-type"].startswith("text/html")
    assert "No such address" in reader.text
    assert "[[" not in reader.text
    assert reader.headers["cache-control"] == "no-store"
    assert "sha256-" in reader.headers["content-security-policy"]

    assert program.status_code == 404
    assert program.json() == {"detail": "Not Found"}


# --------------------------------------------------------------------
# What a crawler asks for first
# --------------------------------------------------------------------


async def test_the_site_answers_for_itself_about_robots_and_sitemap(tmp_path: Path) -> None:
    """The wiring guard for #184, from the app the deploy actually runs.

    The unit tests build the router directly. This one proves
    ``create_app`` mounts it, and that the sitemap it mounts describes
    *this* deployment: the flags the other routers were mounted under
    decide which pages exist, and the sitemap has to have read the same
    ones. The contact form is off here — no admin chat is configured in
    ``_settings`` — so a sitemap that still advertised ``/contact``
    would be pointing a crawler at the 404 page installed just above.
    """
    origin = "https://tgbot.delabs.space"
    application = await _build(tmp_path, origin=origin)
    application.bot_username = "kom17bot"
    try:
        async with await _client(application) as client:
            robots = await client.get("/robots.txt")
            sitemap = await client.get("/sitemap.xml")
    finally:
        await application.close()

    assert robots.status_code == 200
    assert robots.headers["content-type"].startswith("text/plain")
    assert f"Sitemap: {origin}/sitemap.xml" in robots.text

    assert sitemap.status_code == 200
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert f"<loc>{origin}/</loc>" in sitemap.text
    assert f"{origin}/contact" not in sitemap.text


async def test_a_deployment_without_an_origin_advertises_no_sitemap(tmp_path: Path) -> None:
    """Polling mode: no ``WEBHOOK_URL``, so no address to publish.

    A sitemap of relative locations is invalid and discarded whole, so
    the route is not mounted and ``robots.txt`` promises nothing — the
    same silence the site itself keeps.
    """
    application = await _build(tmp_path)
    try:
        async with await _client(application) as client:
            robots = await client.get("/robots.txt")
            sitemap = await client.get("/sitemap.xml")
    finally:
        await application.close()

    assert robots.status_code == 200
    assert "Sitemap:" not in robots.text
    assert sitemap.status_code == 404


async def test_a_clean_shutdown_keeps_the_webhook_registered_by_default(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1568 — a fully-started lifespan must not hand the webhook back.

    ``manage_telegram_webhook=True`` used to imply the matching
    ``deleteWebhook`` on shutdown, and the #1394 gate only excused a
    startup that had FAILED. But the normal way this process dies is
    ``systemctl restart`` from ``scripts/deploy.sh``, where the outgoing
    process had ``startup_complete = True`` — so it deleted the
    registration on its way out, and the incoming process met an empty
    ``info.url``. That is precisely the state in which
    ``setup_webhook``'s anti-restart-loop fallback can never match, so a
    Telegram blip longer than the three-attempt retry budget cost the
    whole deployment.

    The unregister is opt-in now. Setup still runs — leaving the
    registration up is only safe because ``setWebhook`` is idempotent
    and re-asserted every start.
    """
    from telegram_invite_bot.webhook import lifespan as lifespan_module

    calls: list[str] = []

    async def fake_setup(app: Application) -> None:
        calls.append("setup")

    async def fake_teardown(app: Application) -> None:
        calls.append("teardown")

    monkeypatch.setattr(lifespan_module, "setup_webhook", fake_setup)
    monkeypatch.setattr(lifespan_module, "teardown_webhook", fake_teardown)

    fastapi_app = create_app(application, manage_telegram_webhook=True)
    types = await _drive_lifespan(fastapi_app)

    assert calls == ["setup"]
    assert types == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


async def test_the_single_worker_guard_covers_the_asgi_factory(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1990: the guard has to sit where a forking supervisor passes.

    ``_assert_single_worker`` used to be called from ``runner.webhook.
    run`` — the one entry point that structurally cannot fork, because
    it drives ``uvicorn.Server.serve()`` and the forking supervisor
    lives in ``uvicorn.run``/the CLI, which ``src/`` never touches. So
    every line it ever emitted was a false alarm, and the deployment
    shapes that really do fork (gunicorn with the uvicorn worker, or
    ``uvicorn --workers N``) never call ``run`` at all and were never
    checked.

    ``create_app`` is the chokepoint instead: any ASGI-based
    deployment, forking or not, has to go through it.
    """
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        create_app(application)
    finally:
        logger.remove(sink_id)

    assert any("WEB_CONCURRENCY" in line for line in lines), lines
