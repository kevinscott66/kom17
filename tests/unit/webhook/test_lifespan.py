"""Webhook setup/teardown lifecycle.

``setup_webhook`` and ``teardown_webhook`` are the runner's
production-only path — the FastAPI app factory takes a
``manage_telegram_webhook=False`` default so unit tests of the
routes don't hit the network. As a result, both functions sat at 0%
coverage even though they encode three real branches each:

* ``setup_webhook`` — empty URL (skip), with secret token, without
  secret token (full-URL composition is identical either way).
* ``teardown_webhook`` — empty URL (skip), happy path,
  best-effort-swallow on ``delete_webhook`` failure (the
  shutdown-must-not-raise contract).

A regression that flips the empty-URL guard (so ``setWebhook`` runs
with ``url=""``) or that lets a ``delete_webhook`` exception escape
shutdown would be caught by these tests. Mock-only — runs in
milliseconds, no network, no aiosqlite.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger
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
from telegram_invite_bot.webhook.lifespan import (
    _allowed_updates_match,
    setup_webhook,
    teardown_webhook,
)


def _settings(
    *,
    url: str = "",
    secret: str | None = None,
    path: str = "/webhook",
    allow_insecure: bool = True,
) -> Settings:
    """A DEV Settings for the lifespan tests.

    ``allow_insecure`` defaults to *True* so that the tests about
    retries, allowed-updates and teardown can keep saying nothing about
    secrets: without it every one of them would trip the no-secret
    startup refusal and fail for a reason that is not what they are
    testing. The refusal itself is exercised below, explicitly.
    """
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:abc"),
        webhook=WebhookConfig(
            WEBHOOK_URL=url,
            WEBHOOK_PATH=path,
            WEBHOOK_SECRET_TOKEN=SecretStr(secret) if secret is not None else None,
            ALLOW_INSECURE_WEBHOOK=allow_insecure,
        ),
        paths=PathsConfig(),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )


class _Sink:
    """Collects the formatted text of every record emitted while installed."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def __call__(self, message: Any) -> None:  # noqa: ANN401
        self.records.append(str(message))


def _app(settings: Settings, *, used_updates: list[str] | None = None) -> MagicMock:
    """Minimal Application stand-in: only the attributes the lifespan touches."""
    app = MagicMock()
    app.settings = settings
    app.bot = MagicMock()
    app.bot.set_webhook = AsyncMock()
    app.bot.delete_webhook = AsyncMock()
    # The real ``Dispatcher.resolve_used_update_types()`` walks the router
    # tree; here it is stubbed with whatever the test wants to observe.
    app.dispatcher.resolve_used_update_types = MagicMock(
        return_value=(["message", "callback_query"] if used_updates is None else used_updates)
    )
    return app


@pytest.mark.asyncio
async def test_setup_skips_when_url_is_empty() -> None:
    """Dev/test mode (``WEBHOOK_URL=""``) must not call ``setWebhook``.

    Calling with ``url=""`` would *delete* the registered webhook on
    Telegram's side — exactly the silent-deregister regression the
    early-return guards against.
    """
    app = _app(_settings(url=""))
    await setup_webhook(app)
    app.bot.set_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_passes_secret_when_configured() -> None:
    app = _app(_settings(url="https://bot.example.com", secret="topsecret"))
    await setup_webhook(app)
    app.bot.set_webhook.assert_awaited_once()
    kwargs = app.bot.set_webhook.await_args.kwargs
    assert kwargs["url"] == "https://bot.example.com/webhook"
    assert kwargs["secret_token"] == "topsecret"
    assert kwargs["drop_pending_updates"] is False


@pytest.mark.asyncio
async def test_setup_omits_secret_when_unset() -> None:
    """No secret configured ⇒ ``secret_token=None`` (Telegram accepts that)."""
    app = _app(_settings(url="https://bot.example.com", secret=None))
    await setup_webhook(app)
    kwargs = app.bot.set_webhook.await_args.kwargs
    assert kwargs["secret_token"] is None


@pytest.mark.asyncio
async def test_registering_a_public_url_with_no_secret_says_so_loudly() -> None:
    """#2023: an unauthenticated webhook must not be a silent state.

    ``verify_secret_token`` no-ops when no secret is configured, and
    that is deliberate — dev and staging run without one. What makes it
    dangerous is that the state has no symptom: Telegram is told no
    secret, so it sends no header, so every update passes, so the bot
    looks perfectly healthy while its only authentication control
    authenticates nobody. An operator reading the journal cannot tell
    that deployment apart from a secured one.

    A registered ``WEBHOOK_URL`` is what turns it from a local
    convenience into a public endpoint anyone can POST forged updates
    to, which is why the check belongs HERE, past the empty-URL guard,
    and not in the request path where it would fire on every update.

    This is the opted-in half: the operator has said
    ``ALLOW_INSECURE_WEBHOOK=1``, so startup proceeds — and the journal
    still has to say what is running, naming the flag that allowed it.
    """
    app = _app(_settings(url="https://bot.example.com", secret=None, allow_insecure=True))

    sink = _Sink()
    handler_id = logger.add(sink, level="ERROR", format="{message}")
    try:
        await setup_webhook(app)
    finally:
        logger.remove(handler_id)

    said = [line for line in sink.records if "ALLOW_INSECURE_WEBHOOK" in line]
    assert len(said) == 1, (
        "a public webhook was registered with no secret and the journal"
        f" does not say so: {sink.records}"
    )
    assert "forged" in said[0], "the line has to state the consequence, not the configuration"


@pytest.mark.asyncio
async def test_a_public_url_with_no_secret_refuses_to_start_by_default() -> None:
    """#2023 follow-up: an error in the log was not enough.

    The deployment it described still came up and still served traffic,
    so the only thing standing between an open webhook and a closed one
    was whether somebody read the journal of a bot that looked healthy.
    Nothing else in the system can catch it either: Telegram is told no
    secret, so it sends no header, so every forged update validates.

    Startup therefore fails closed. The insecure mode is still
    reachable — a tunnel to a laptop is a real use — but only by
    declaring it, which is the entire difference between running
    insecure and running insecure without knowing.
    """
    app = _app(_settings(url="https://bot.example.com", secret=None, allow_insecure=False))

    with pytest.raises(RuntimeError, match="no secret token") as excinfo:
        await setup_webhook(app)

    message = str(excinfo.value)
    assert "WEBHOOK_SECRET_TOKEN" in message, "say which setting fixes it"
    assert "APP_ENV" in message, "and which one makes it mandatory"
    assert "ALLOW_INSECURE_WEBHOOK" in message, "and how to proceed deliberately"
    app.bot.set_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_secured_registration_stays_quiet() -> None:
    """The warning must not cry wolf on a correctly configured bot."""
    app = _app(_settings(url="https://bot.example.com", secret="topsecret"))

    sink = _Sink()
    handler_id = logger.add(sink, level="ERROR", format="{message}")
    try:
        await setup_webhook(app)
    finally:
        logger.remove(handler_id)

    assert sink.records == []


@pytest.mark.asyncio
async def test_no_url_means_no_public_endpoint_and_no_alarm() -> None:
    """Local dev registers nothing, so there is nothing to warn about.

    The empty-URL skip already logs its own line; adding a second,
    scarier one to every developer's console is how a real alarm gets
    trained out of an operator.
    """
    app = _app(_settings(url="", secret=None))

    sink = _Sink()
    handler_id = logger.add(sink, level="ERROR", format="{message}")
    try:
        await setup_webhook(app)
    finally:
        logger.remove(handler_id)

    assert sink.records == []


@pytest.mark.asyncio
async def test_setup_strips_trailing_slash_from_base_url() -> None:
    """``url.rstrip("/") + path`` — guards against double-slash URLs.

    Telegram requires the exact webhook URL it was registered with on
    every incoming request; ``//webhook`` vs ``/webhook`` is a real
    mismatch that would silently break delivery.
    """
    app = _app(_settings(url="https://bot.example.com/", secret=None))
    await setup_webhook(app)
    kwargs = app.bot.set_webhook.await_args.kwargs
    assert kwargs["url"] == "https://bot.example.com/webhook"


@pytest.mark.asyncio
async def test_setup_subscribes_only_to_handled_update_types() -> None:
    """``allowed_updates`` mirrors the dispatcher, not Telegram's default.

    Telegram's default set is "everything except chat_member and the
    reaction/boost types". Unset, that delivers every
    ``edited_message``, ``channel_post``, ``business_message`` and
    ``poll_answer`` to a dispatcher with no handler for them (prod
    logged aiogram's "Detected unknown update type" warning), while
    still withholding ``chat_member`` from a handler that asked for it.
    Resolving from the dispatcher fixes both halves at once.
    """
    app = _app(
        _settings(url="https://bot.example.com"),
        used_updates=["message", "callback_query", "my_chat_member"],
    )
    await setup_webhook(app)
    kwargs = app.bot.set_webhook.await_args.kwargs
    assert kwargs["allowed_updates"] == ["message", "callback_query", "my_chat_member"]


@pytest.mark.asyncio
async def test_setup_leaves_allowed_updates_unset_when_resolution_is_empty() -> None:
    """An empty list is not "no preference" — Telegram reads it as
    "deliver nothing". If resolution ever comes back empty (a wiring bug
    that mounts no routers), fall back to ``None`` so the argument is
    omitted and the bot keeps receiving updates instead of going
    silently deaf.
    """
    app = _app(_settings(url="https://bot.example.com"), used_updates=[])
    await setup_webhook(app)
    kwargs = app.bot.set_webhook.await_args.kwargs
    assert kwargs["allowed_updates"] is None


@pytest.mark.asyncio
async def test_teardown_skips_when_url_is_empty() -> None:
    """Symmetric to setup — dev/test mode must not call ``deleteWebhook``."""
    app = _app(_settings(url=""))
    await teardown_webhook(app)
    app.bot.delete_webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_teardown_calls_delete_webhook_on_shutdown() -> None:
    app = _app(_settings(url="https://bot.example.com"))
    await teardown_webhook(app)
    app.bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)


@pytest.mark.asyncio
async def test_setup_retries_on_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """M-I-1: ``setWebhook`` is retried up to 3 times with backoff before giving up.

    Two transient failures followed by a success — startup must complete
    without raising and the final ``set_webhook`` invocation is the one
    that returned successfully.
    """
    import telegram_invite_bot.webhook.lifespan as mod

    # Skip the real sleep so the test is sub-second.
    async def _no_sleep(_seconds: float) -> None:
        return None

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    app = _app(_settings(url="https://bot.example.com"))
    app.bot.set_webhook = AsyncMock(
        side_effect=[RuntimeError("blip 1"), RuntimeError("blip 2"), None]
    )
    await mod.setup_webhook(app)
    assert app.bot.set_webhook.await_count == 3


@pytest.mark.asyncio
async def test_setup_continues_when_existing_webhook_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-I-1: after retries exhausted, if ``getWebhookInfo`` reports the
    expected URL is already registered, startup continues (the prior
    deploy's webhook is still serving traffic).
    """
    import telegram_invite_bot.webhook.lifespan as mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    app = _app(_settings(url="https://bot.example.com"))
    app.bot.set_webhook = AsyncMock(side_effect=RuntimeError("Telegram down"))
    info = MagicMock()
    info.url = "https://bot.example.com/webhook"
    info.allowed_updates = ["message", "callback_query"]
    app.bot.get_webhook_info = AsyncMock(return_value=info)

    # Must NOT raise — existing registration matches.
    await mod.setup_webhook(app)
    assert app.bot.set_webhook.await_count == 3
    app.bot.get_webhook_info.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_raises_when_existing_webhook_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-I-1: if the existing webhook URL does not match what we'd set,
    re-raise the last error — silently using a foreign webhook is the
    misconfiguration we want startup to surface.
    """
    import telegram_invite_bot.webhook.lifespan as mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    app = _app(_settings(url="https://bot.example.com"))
    app.bot.set_webhook = AsyncMock(side_effect=RuntimeError("Telegram down"))
    info = MagicMock()
    info.url = "https://different.example.com/webhook"
    app.bot.get_webhook_info = AsyncMock(return_value=info)

    with pytest.raises(RuntimeError, match="Telegram down"):
        await mod.setup_webhook(app)


@pytest.mark.asyncio
async def test_setup_raises_when_get_webhook_info_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-I-1: when both ``setWebhook`` and ``getWebhookInfo`` fail, we
    have no proof the webhook is configured at all — re-raise.
    """
    import telegram_invite_bot.webhook.lifespan as mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    app = _app(_settings(url="https://bot.example.com"))
    app.bot.set_webhook = AsyncMock(side_effect=RuntimeError("Telegram down"))
    app.bot.get_webhook_info = AsyncMock(side_effect=RuntimeError("also down"))

    with pytest.raises(RuntimeError, match="Telegram down"):
        await mod.setup_webhook(app)


@pytest.mark.asyncio
async def test_teardown_swallows_delete_webhook_failure() -> None:
    """Shutdown must complete even if Telegram is unreachable.

    A raised exception during ``deleteWebhook`` would leak through
    FastAPI's lifespan teardown and turn a clean shutdown into a
    crash — losing whatever orderly close the runner had planned
    next (engine dispose, container exit). The handler logs and
    moves on; this test locks that contract.
    """
    app = _app(_settings(url="https://bot.example.com"))
    app.bot.delete_webhook.side_effect = RuntimeError("Telegram unreachable")
    # Must NOT raise.
    await teardown_webhook(app)
    app.bot.delete_webhook.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_final_failure_does_not_promise_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#819: the last attempt must not log a sleep that never happens.

    ``_SETUP_RETRY_DELAYS_SECONDS`` used to double as the attempt count,
    so its third element was never slept on — but the log line was
    emitted before the guard that skipped it, and told the operator
    "sleeping 4.0s before retry" a moment before "exhausted". Reading
    the journal after a failed deploy, you had to work out for yourself
    which of the two lines was lying. Attempts and delays are separate
    names now; this pins both the honest wording and the schedule.
    """
    import asyncio as _asyncio

    import telegram_invite_bot.webhook.lifespan as mod

    slept: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(_asyncio, "sleep", _record_sleep)

    app = _app(_settings(url="https://bot.example.com"))
    app.bot.set_webhook = AsyncMock(side_effect=RuntimeError("Telegram down"))
    # Existing registration matches, so startup continues rather than
    # raising — this test is about the log, not the give-up branch.
    info = MagicMock()
    info.url = "https://bot.example.com/webhook"
    info.allowed_updates = ["message", "callback_query"]
    app.bot.get_webhook_info = AsyncMock(return_value=info)

    sink = _Sink()
    handler_id = logger.add(sink, level="WARNING", format="{message}")
    try:
        await mod.setup_webhook(app)
    finally:
        logger.remove(handler_id)

    assert app.bot.set_webhook.await_count == 3
    # Three attempts, two waits between them — never a wait after the last.
    assert slept == [1.0, 2.0]

    attempts = [line for line in sink.records if line.startswith("setWebhook attempt")]
    assert len(attempts) == 3
    assert "sleeping 1.0s before retry" in attempts[0]
    assert "sleeping 2.0s before retry" in attempts[1]
    assert "no retries left" in attempts[2]
    assert "sleeping" not in attempts[2]


# ── The give-up fallback must verify the update set, not just the URL ──


def test_allowed_updates_match_rules() -> None:
    """The three cases the fallback turns on, pinned in isolation.

    Order is Telegram's to choose, so equal sets in a different order
    match. ``None`` on our side means "we omitted the argument", so
    anything live is what we would have asked for. ``None`` on
    Telegram's side is the DEFAULT set, which is emphatically not our
    resolved list — it withholds ``chat_member`` and delivers types
    nothing handles — so it is a mismatch, not a wildcard.
    """
    assert _allowed_updates_match(None, None) is True
    assert _allowed_updates_match(["message"], ["message"]) is True
    assert _allowed_updates_match(["a", "b"], ["b", "a"]) is True
    assert _allowed_updates_match(["message"], None) is False
    assert _allowed_updates_match(["message"], ["message", "callback_query"]) is False


@pytest.mark.asyncio
async def test_setup_raises_when_existing_allowed_updates_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL match alone is not proof the live webhook is the one we want.

    The registration this deploy would replace can carry a stale
    ``allowed_updates`` — that is exactly what happens when a release
    adds a handler for an update type nobody subscribed to before. If we
    continue on it, the new handler never fires and nothing says so: the
    URL matches, startup logs "continuing", and the missing update type
    is invisible until a user reports the feature simply doing nothing.
    """
    import telegram_invite_bot.webhook.lifespan as mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    app = _app(
        _settings(url="https://bot.example.com"),
        used_updates=["message", "callback_query", "my_chat_member"],
    )
    app.bot.set_webhook = AsyncMock(side_effect=RuntimeError("Telegram down"))
    info = MagicMock()
    info.url = "https://bot.example.com/webhook"
    # The previous deploy never subscribed to ``my_chat_member``.
    info.allowed_updates = ["message", "callback_query"]
    app.bot.get_webhook_info = AsyncMock(return_value=info)

    with pytest.raises(RuntimeError, match="Telegram down"):
        await mod.setup_webhook(app)


@pytest.mark.asyncio
async def test_continue_warning_names_the_unverifiable_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one thing the fallback cannot check has to be said out loud.

    ``aiogram.types.WebhookInfo`` has no ``secret_token`` field — Telegram
    never returns it — so continuing on the previous deploy's
    registration silently keeps the PREVIOUS secret. If this deploy
    rotated ``WEBHOOK_SECRET_TOKEN``, every update is then answered 403
    by ``verify_secret_token`` and the bot is off the air while looking
    healthy. The warning must point the operator at the metric that
    shows it.
    """
    import telegram_invite_bot.webhook.lifespan as mod

    async def _no_sleep(_seconds: float) -> None:
        return None

    import asyncio as _asyncio

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    app = _app(_settings(url="https://bot.example.com", secret="ZZZ-secret-marker-8888"))
    app.bot.set_webhook = AsyncMock(side_effect=RuntimeError("Telegram down"))
    info = MagicMock()
    info.url = "https://bot.example.com/webhook"
    info.allowed_updates = ["callback_query", "message"]
    app.bot.get_webhook_info = AsyncMock(return_value=info)

    sink = _Sink()
    handler_id = logger.add(sink, level="WARNING", format="{message}")
    try:
        await mod.setup_webhook(app)
    finally:
        logger.remove(handler_id)

    kept = [line for line in sink.records if "continuing startup" in line]
    assert len(kept) == 1
    assert "WEBHOOK_SECRET_TOKEN" in kept[0]
    assert 'outcome="forbidden"' in kept[0]
    # The secret itself must never reach a log line.
    assert "ZZZ-secret-marker-8888" not in kept[0]
