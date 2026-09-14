"""Entry points: ``__main__`` and the two ``runner.*`` modules.

These three files are the literal first lines of code that execute when
the container starts (``python -m telegram_invite_bot --mode=…``). They
hold no real logic — they're just glue around ``build_app`` + a runner —
but a regression here breaks every deploy at once. CI was at 0% on all
three because nothing ever imported them outside of production.

We patch ``build_app`` so no real container is built, and we patch the
``Dispatcher.start_polling`` / ``uvicorn.Server.serve`` halves so the
runners exit immediately. The point is to lock the contract:

* the runner always builds the app first,
* always closes it in ``finally`` (a crash in ``serve()`` mustn't leak
  the SQLite engines), and
* webhook mode calls ``create_app`` with ``manage_telegram_webhook=True``
  — flipping that flag would make uvicorn skip the Telegram
  ``setWebhook`` round-trip on boot.

T-011 (2026-05-26) removed the legacy telebot ``TelebotFallback`` that
webhook mode used to wire alongside the FastAPI app; the runner now
just builds and serves the new pipeline directly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_invite_bot import __main__ as main_module
from telegram_invite_bot.runner import polling as polling_module
from telegram_invite_bot.runner import webhook as webhook_module

# String paths for ``monkeypatch.setattr`` — avoids the attr-defined
# mypy complaint that ``webhook_module.uvicorn`` is re-exported.
_UVICORN_CONFIG = "telegram_invite_bot.runner.webhook.uvicorn.Config"
_UVICORN_SERVER = "telegram_invite_bot.runner.webhook.uvicorn.Server"


def _fake_application() -> MagicMock:
    app = MagicMock()
    app.settings = MagicMock()
    app.settings.app_env = MagicMock(value="dev")
    app.settings.webhook = MagicMock(
        host="0.0.0.0",
        port=8443,
        ssl_cert=None,
        ssl_key=None,
        forwarded_allow_ips="127.0.0.1",
    )
    app.bot = MagicMock()
    app.dispatcher = MagicMock()
    app.dispatcher.start_polling = AsyncMock()
    app.close = AsyncMock()
    # Stage 35: the runners now invoke ``Application.start_background``
    # before handing off to start_polling / uvicorn so the FSM timeout
    # sweeper task is alive for the whole serving window. Mock it as
    # an awaitable no-op — the close path still owns cancellation.
    app.start_background = AsyncMock()
    return app


# ── runner.polling ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_polling_run_builds_app_starts_polling_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: ``start_polling`` returns cleanly and ``close()``
    still runs. If a future refactor moves ``close`` out of the
    ``finally`` block this test fails first.
    """
    app = _fake_application()

    async def fake_build() -> MagicMock:
        return app

    monkeypatch.setattr(polling_module, "build_app", fake_build)

    await polling_module.run()

    app.dispatcher.start_polling.assert_awaited_once_with(app.bot, handle_signals=True)
    app.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_polling_run_closes_app_when_start_polling_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If polling crashes (network hiccup, Ctrl-C, etc.) we still owe
    the world a ``close()`` — otherwise the five SQLite engines leak
    until the next process restart.
    """
    app = _fake_application()
    boom = RuntimeError("polling crashed")
    app.dispatcher.start_polling.side_effect = boom

    async def fake_build() -> MagicMock:
        return app

    monkeypatch.setattr(polling_module, "build_app", fake_build)

    with pytest.raises(RuntimeError) as excinfo:
        await polling_module.run()

    assert excinfo.value is boom
    app.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_polling_run_closes_app_when_start_background_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1574 — a half-spawned background set still owes a ``close()``.

    ``start_background`` is not atomic: it registers the FSM sweeper
    first and only then does a lazy import to build the economy-cleanup
    job. The call used to sit ABOVE the ``try`` that owns
    ``application.close()``, so a failure in the second half propagated
    with the first half still running — leaking the sweeper task, the
    dispatcher storage, the bot session and all five engines until the
    process was killed. The webhook lifespan has always put the same
    call inside its teardown ``try``; this is the polling mirror.
    """
    app = _fake_application()
    boom = RuntimeError("economy_cleanup import blew up")
    app.start_background.side_effect = boom

    async def fake_build() -> MagicMock:
        return app

    monkeypatch.setattr(polling_module, "build_app", fake_build)

    with pytest.raises(RuntimeError) as excinfo:
        await polling_module.run()

    assert excinfo.value is boom
    app.dispatcher.start_polling.assert_not_awaited()
    app.close.assert_awaited_once()


# ── runner.webhook ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_run_builds_app_starts_uvicorn_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Webhook runner must:
    1. build the app,
    2. hand it to ``create_app`` with ``manage_telegram_webhook=True``
       — the flag tells the FastAPI lifespan to call Telegram's
       setWebhook,
    2b. hand it ``unregister_webhook_on_shutdown=False`` — #1568: the
       registration must survive a restart so the next process's
       ``setWebhook`` fallback has something to fall back on,
    3. hand it ``close_application=True`` as well — #279: uvicorn
       restores ``SIG_DFL`` and re-raises the captured SIGTERM as
       ``serve()`` unwinds, so the ``finally`` below is unreachable on
       a real ``systemctl stop`` and the ASGI lifespan has to own the
       teardown,
    4. start uvicorn,
    5. still close the app afterwards, for the paths where ``serve()``
       fails without a signal (port in use, bad TLS material).
    """
    app = _fake_application()

    async def fake_build() -> MagicMock:
        return app

    monkeypatch.setattr(webhook_module, "build_app", fake_build)

    captured_create: dict[str, Any] = {}

    def fake_create_app(
        application: Any,
        *,
        manage_telegram_webhook: bool,
        close_application: bool = False,
        # Defaulted to the WRONG value on purpose: the runner has to
        # pass this explicitly (#1568), and a default of ``False`` here
        # would let a runner that stopped passing it slip through.
        unregister_webhook_on_shutdown: bool = True,
    ) -> object:
        captured_create["application"] = application
        captured_create["manage_telegram_webhook"] = manage_telegram_webhook
        captured_create["close_application"] = close_application
        captured_create["unregister_webhook_on_shutdown"] = unregister_webhook_on_shutdown
        return object()

    monkeypatch.setattr(webhook_module, "create_app", fake_create_app)

    server_instance = MagicMock()
    server_instance.serve = AsyncMock()
    config_seen: dict[str, Any] = {}

    def fake_config(asgi: Any, **kwargs: Any) -> MagicMock:
        config_seen["asgi"] = asgi
        config_seen.update(kwargs)
        return MagicMock(name="UvicornConfig")

    monkeypatch.setattr(_UVICORN_CONFIG, fake_config)
    monkeypatch.setattr(_UVICORN_SERVER, MagicMock(return_value=server_instance))

    await webhook_module.run()

    assert captured_create["application"] is app
    assert captured_create["manage_telegram_webhook"] is True
    assert captured_create["close_application"] is True
    # #1568: prod must NOT hand the registration back on shutdown. The
    # normal death of this process is ``systemctl restart``, and an
    # empty ``info.url`` in the gap disarms ``setup_webhook``'s
    # anti-restart-loop fallback for exactly the deploy it protects.
    assert captured_create["unregister_webhook_on_shutdown"] is False

    # uvicorn config picks up host/port from settings and disables its
    # own access log + log config so loguru's interceptor stays the
    # single source of truth.
    assert config_seen["host"] == "0.0.0.0"
    assert config_seen["port"] == 8443
    assert config_seen["access_log"] is False
    assert config_seen["log_config"] is None
    assert config_seen["ssl_certfile"] is None
    assert config_seen["ssl_keyfile"] is None
    # M-I-8: trust X-Forwarded-* only from the configured proxy IPs.
    # Without proxy_headers=True uvicorn ignores X-Forwarded-For and
    # every request looks like it came from 127.0.0.1; a wider allow
    # list would let any client spoof their IP via the header.
    assert config_seen["proxy_headers"] is True
    assert config_seen["forwarded_allow_ips"] == "127.0.0.1"

    server_instance.serve.assert_awaited_once()
    app.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_run_threads_custom_forwarded_allow_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-I-8: ``FORWARDED_ALLOW_IPS`` from settings flows into
    ``uvicorn.Config(forwarded_allow_ips=...)`` verbatim. Operators
    running with a non-loopback nginx (e.g. a sidecar on 10.0.0.0/8)
    need to widen the allow-list without code changes.
    """
    app = _fake_application()
    app.settings.webhook.forwarded_allow_ips = "10.0.0.5,10.0.0.6"

    async def fake_build() -> MagicMock:
        return app

    monkeypatch.setattr(webhook_module, "build_app", fake_build)
    monkeypatch.setattr(webhook_module, "create_app", lambda *a, **kw: object())

    config_seen: dict[str, Any] = {}

    def fake_config(_asgi: Any, **kwargs: Any) -> MagicMock:
        config_seen.update(kwargs)
        return MagicMock()

    server_instance = MagicMock()
    server_instance.serve = AsyncMock()
    monkeypatch.setattr(_UVICORN_CONFIG, fake_config)
    monkeypatch.setattr(_UVICORN_SERVER, MagicMock(return_value=server_instance))

    await webhook_module.run()

    assert config_seen["proxy_headers"] is True
    assert config_seen["forwarded_allow_ips"] == "10.0.0.5,10.0.0.6"


@pytest.mark.asyncio
async def test_webhook_run_bounds_the_graceful_shutdown_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1935: the graceful-shutdown wait must be finite.

    uvicorn defaults ``timeout_graceful_shutdown`` to ``None`` and hands
    it straight to ``asyncio.wait_for`` (``server.py:289``), so the
    default is "wait for in-flight requests forever". One update parked
    in ``feed_update`` behind a slow upstream then blocks
    ``Server.shutdown()`` on SIGTERM, the ``self.lifespan.shutdown()``
    call on the very next line never runs, and ``application.close()``
    — the #1815 broadcast drain, the storage/session closes, and
    ``engines.dispose()`` across every SQLite pool — is skipped until
    systemd SIGKILLs the process at its 90 s ``DefaultTimeoutStopSec``.
    The runner's ``finally`` is no help there: SIGKILL runs nothing.

    Two things are pinned. That we pass a bounded value at all, and
    that it leaves systemd's budget room for the teardown it exists to
    protect — a value at or above 90 s would be the unbounded bug
    wearing a number.
    """
    app = _fake_application()

    async def fake_build() -> MagicMock:
        return app

    monkeypatch.setattr(webhook_module, "build_app", fake_build)
    monkeypatch.setattr(webhook_module, "create_app", lambda *a, **kw: object())

    config_seen: dict[str, Any] = {}

    def fake_config(_asgi: Any, **kwargs: Any) -> MagicMock:
        config_seen.update(kwargs)
        return MagicMock()

    server_instance = MagicMock()
    server_instance.serve = AsyncMock()
    monkeypatch.setattr(_UVICORN_CONFIG, fake_config)
    monkeypatch.setattr(_UVICORN_SERVER, MagicMock(return_value=server_instance))

    await webhook_module.run()

    timeout = config_seen["timeout_graceful_shutdown"]
    assert timeout is not None
    assert 0 < timeout < 90


def test_uvicorn_leaves_the_graceful_shutdown_wait_unbounded_by_default() -> None:
    """#1935: pins the premise of the guard above.

    The fix is only load-bearing while uvicorn's own default really is
    ``None``. If a future upgrade ships a finite default, this test
    fails and tells us to re-read the reasoning rather than leaving a
    magic number nobody can justify.
    """
    import uvicorn

    async def _asgi(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
        raise AssertionError("never served")

    assert uvicorn.Config(_asgi).timeout_graceful_shutdown is None


@pytest.mark.asyncio
async def test_webhook_run_passes_ssl_paths_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """When ``webhook.ssl_cert`` and ``ssl_key`` are both set, uvicorn
    binds HTTPS directly (no nginx). The runner stringifies the Paths
    because uvicorn's API takes ``str | None`` only — passing a
    ``PosixPath`` would raise ``TypeError`` at boot.
    """
    app = _fake_application()
    cert = tmp_path / "fullchain.pem"
    key = tmp_path / "privkey.pem"
    app.settings.webhook.ssl_cert = cert
    app.settings.webhook.ssl_key = key

    async def fake_build() -> MagicMock:
        return app

    monkeypatch.setattr(webhook_module, "build_app", fake_build)
    monkeypatch.setattr(webhook_module, "create_app", lambda *a, **kw: object())

    config_seen: dict[str, Any] = {}

    def fake_config(asgi: Any, **kwargs: Any) -> MagicMock:
        config_seen.update(kwargs)
        return MagicMock()

    server_instance = MagicMock()
    server_instance.serve = AsyncMock()
    monkeypatch.setattr(_UVICORN_CONFIG, fake_config)
    monkeypatch.setattr(_UVICORN_SERVER, MagicMock(return_value=server_instance))

    await webhook_module.run()

    assert config_seen["ssl_certfile"] == str(cert)
    assert config_seen["ssl_keyfile"] == str(key)


@pytest.mark.asyncio
async def test_webhook_run_closes_app_when_uvicorn_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of the polling crash test — uvicorn's ``serve`` may exit
    with an exception (SIGTERM during a slow request, port conflict on
    boot). ``close()`` still has to fire.
    """
    app = _fake_application()

    async def fake_build() -> MagicMock:
        return app

    monkeypatch.setattr(webhook_module, "build_app", fake_build)
    monkeypatch.setattr(webhook_module, "create_app", lambda *a, **kw: object())

    boom = RuntimeError("uvicorn crashed")
    server_instance = MagicMock()
    server_instance.serve = AsyncMock(side_effect=boom)
    monkeypatch.setattr(_UVICORN_CONFIG, MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(_UVICORN_SERVER, MagicMock(return_value=server_instance))

    with pytest.raises(RuntimeError) as excinfo:
        await webhook_module.run()

    assert excinfo.value is boom
    app.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_run_closes_app_when_create_app_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#821: ``build_app`` already opened the engines and the aiogram
    session, so a failure in the *setup* block — not just in
    ``serve()`` — has to run ``close()`` too. ``create_app`` is the
    first thing that can raise after the app exists.
    """
    app = _fake_application()

    async def fake_build() -> MagicMock:
        return app

    boom = RuntimeError("router wiring blew up")

    def exploding_create_app(*a: Any, **kw: Any) -> Any:
        raise boom

    monkeypatch.setattr(webhook_module, "build_app", fake_build)
    monkeypatch.setattr(webhook_module, "create_app", exploding_create_app)

    with pytest.raises(RuntimeError) as excinfo:
        await webhook_module.run()

    assert excinfo.value is boom
    app.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_run_closes_app_when_uvicorn_config_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#821, second half: ``uvicorn.Config`` validates its arguments
    eagerly, so unreadable TLS material or a malformed
    ``forwarded_allow_ips`` raises before ``Server`` is ever built.
    That path used to leak the engines as well.
    """
    app = _fake_application()

    async def fake_build() -> MagicMock:
        return app

    boom = ValueError("bad TLS material")

    monkeypatch.setattr(webhook_module, "build_app", fake_build)
    monkeypatch.setattr(webhook_module, "create_app", lambda *a, **kw: object())
    monkeypatch.setattr(_UVICORN_CONFIG, MagicMock(side_effect=boom))

    with pytest.raises(ValueError) as excinfo:
        await webhook_module.run()

    assert excinfo.value is boom
    app.close.assert_awaited_once()


# ── __main__ ──────────────────────────────────────────────────────────────


def test_main_default_mode_dispatches_to_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``--mode`` flag → polling runner. The default is "polling"
    on purpose: a developer who forgets the flag must NOT accidentally
    start a webhook server against the production token. Pinning it
    here means a future "default to webhook" refactor is loud.
    """
    called: list[str] = []

    async def fake_polling() -> None:
        called.append("polling")

    async def fake_webhook() -> None:  # pragma: no cover — not reached
        called.append("webhook")

    monkeypatch.setattr(polling_module, "run", fake_polling)
    monkeypatch.setattr(webhook_module, "run", fake_webhook)

    rc = main_module.main([])

    assert rc == 0
    assert called == ["polling"]


def test_main_webhook_mode_dispatches_to_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    async def fake_polling() -> None:  # pragma: no cover — not reached
        called.append("polling")

    async def fake_webhook() -> None:
        called.append("webhook")

    monkeypatch.setattr(polling_module, "run", fake_polling)
    monkeypatch.setattr(webhook_module, "run", fake_webhook)

    rc = main_module.main(["--mode", "webhook"])

    assert rc == 0
    assert called == ["webhook"]


def test_main_rejects_unknown_mode() -> None:
    """argparse must reject typos at the CLI boundary — never fall
    through to the ``ValueError`` inside ``_async_main`` (which is the
    last-ditch defensive raise).
    """
    with pytest.raises(SystemExit) as excinfo:
        main_module.main(["--mode", "carrier-pigeon"])
    # argparse exits with code 2 on usage errors.
    assert excinfo.value.code == 2
