"""Shared pytest fixtures.

Stage 2: provides a ``tmp_settings`` factory that builds an isolated
:class:`Settings` rooted at a ``tmp_path`` so engine tests never touch
the real ``database/`` directory (which on a dev box may hold a stale
copy of production data, which tests must never read or write).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

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


@pytest.fixture(autouse=True)
def _reset_language_cache() -> None:
    """Clear the module-level LanguageMiddleware cache before each test.

    The cache is process-global (so ``/lang`` can invalidate one user
    across middleware instances); without this reset, a fixed test
    ``user_id`` reused across cases with different expected languages
    would serve a stale cached language and fail in-suite while passing
    in isolation.
    """
    from telegram_invite_bot.middlewares.language import clear_language_cache

    clear_language_cache()


@pytest.fixture(autouse=True)
def _reset_unverified_alert_budget() -> None:
    """Clear the two process-global payment-alert budgets per test.

    Same reason as the language cache above: the marks are
    process-global by design (one worker serves the webhook routes),
    so the test that deliberately spends the whole hourly budget
    would silence every alert assertion that runs after it and make
    the suite's result depend on collection order.

    Lives here rather than next to the payment tests so a reversal
    case added in another module inherits the reset instead of
    quietly borrowing a half-spent budget.

    Both budgets, not one: #1643 gave the paid-but-uncredited card its
    own ration, deliberately kept separate from #228's so that a flood
    of one kind cannot silence the other. The fixture name is left
    alone — renaming it would churn nothing but this docstring.
    """
    from telegram_invite_bot.webhook.payments import (
        reset_uncredited_alert_budget,
        reset_unverified_alert_budget,
    )

    reset_unverified_alert_budget()
    reset_uncredited_alert_budget()


@pytest.fixture(autouse=True)
def _reset_reverify_throttle() -> None:
    """Give every test the YooKassa rate limiter's full budget.

    Third process-global on this route, and the one with the longest
    memory: its buckets refill against ``time.monotonic``, so a test
    that spends them is spending them for whatever runs in the next few
    seconds too. Production hosts one application per process and wants
    exactly that behaviour; a suite hosting hundreds would otherwise
    hand an unrelated payment test a 429 depending on collection order.
    """
    from telegram_invite_bot.webhook.payments import reset_reverify_throttle

    reset_reverify_throttle()


@pytest.fixture(autouse=True)
def _reset_runtime_secrets() -> None:
    """Clear the #1367 runtime-secret set before each test.

    ``register_runtime_secret`` is called on the Crypto Pay token read
    path, so any test that resolves a token leaves that value in a
    process-global set for the rest of the session — later tests would
    then see it redacted out of log messages they never registered it
    in, and the assertion that the set is empty after refusing short
    values would depend on collection order.
    """
    from telegram_invite_bot.config.logging import _RUNTIME_SECRETS

    _RUNTIME_SECRETS.clear()


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    """Return a factory for tmp-rooted ``Settings`` instances."""

    def _factory(app_env: AppEnv = AppEnv.DEV) -> Settings:
        # Stage 78: ``Settings._require_secret_token_in_prod`` rejects a
        # missing token when ``app_env=PROD``. Build the WebhookConfig
        # with the dummy token only in that mode so prod-mode tests
        # (db.safety listener etc.) keep building Settings cleanly while
        # dev-mode tests don't have to pretend a secret exists.
        webhook = (
            WebhookConfig(
                WEBHOOK_SECRET_TOKEN="test-secret-32-chars-padding-xxxx",
                # Stage 83: Settings._require_webhook_url_in_prod also
                # rejects an empty WEBHOOK_URL when app_env=PROD.
                WEBHOOK_URL="https://test.invalid",
            )
            if app_env is AppEnv.PROD
            else WebhookConfig()
        )
        return Settings(
            app_env=app_env,
            bot=BotConfig(BOT_TOKEN="123:test-token-for-tests-only"),
            webhook=webhook,
            paths=PathsConfig(
                DATABASE_DIR=tmp_path / "db",
                MESSAGE_STATS_DIR=tmp_path / "db",
                LOGS_DIR=tmp_path / "logs",
            ),
            logging=LoggingConfig(),
            observability=ObservabilityConfig(),
            features=FeatureFlags(),
        )

    return _factory


# ---------------------------------------------------------------------------
# The ambient event loop
# ---------------------------------------------------------------------------

#: The loop this session installs as the main thread's ambient one. A
#: dict rather than a module-level name so the hooks below do not need
#: a ``global`` statement.
_AMBIENT: dict[str, asyncio.AbstractEventLoop | None] = {"loop": None}


def pytest_sessionstart() -> None:
    """Own the main thread's ambient event loop for the whole session.

    On Python 3.11 ``asyncio.get_event_loop()`` still CREATES a loop
    when the calling thread has none, and pytest-asyncio calls it once
    per scoped runner to remember the loop it is about to displace
    (``pytest_asyncio.plugin._get_event_loop_no_warn``). Whatever that
    call mints is only ever handed back afterwards, never closed, so
    the garbage collector finalises it as three unraisable
    ``ResourceWarning``s: the loop plus the two halves of its
    self-pipe. Under ``filterwarnings = error`` those become a failure
    attributed to whichever test the collector happened to interrupt —
    a different one on every run, and never the one at fault. The suite
    passes on 3.14, where the same call raises instead of minting, so
    the whole thing is invisible until CI runs the supported floor.

    Installing a loop up front means that call finds one instead of
    creating it. :func:`pytest_sessionfinish` closes it.
    """
    loop = asyncio.new_event_loop()
    _AMBIENT["loop"] = loop
    asyncio.set_event_loop(loop)


def pytest_runtest_teardown() -> None:
    """Reinstate the ambient loop after every test.

    ``asyncio.run`` clears the thread's current loop on the way out, so
    a synchronous test that uses it leaves the next scoped runner
    looking at an empty slot again. Reinstating costs one attribute
    write per test and makes the guarantee hold for the whole session
    rather than only until the first such test.
    """
    loop = _AMBIENT["loop"]
    if loop is not None and not loop.is_closed():
        asyncio.set_event_loop(loop)


def pytest_sessionfinish() -> None:
    """Close the loop :func:`pytest_sessionstart` installed."""
    loop = _AMBIENT["loop"]
    _AMBIENT["loop"] = None
    if loop is not None and not loop.is_closed():
        asyncio.set_event_loop(None)
        loop.close()
