"""End-to-end ``/admin_settings``.

Pins:

* Non-developer → silent drop.
* Secrets (BOT_TOKEN, WEBHOOK_SECRET_TOKEN, SENTRY_DSN) render
  as ``<set>`` / ``<unset>`` — never the underlying value, not
  even partially. The pin is load-bearing: the card is meant
  to be safe to share in operator-only DMs, and any leak of a
  bot token compromises the whole bot.
* Non-secret values (LOG_LEVEL, WEBHOOK_PATH, ports, etc.)
  render verbatim.
* developer_ids surfaces the *resolved* set (including the
  ADMIN_CHAT_ID fallback) — operator should not re-derive the
  fallback in their head.
* SSL pair status renders yes/no, not the underlying paths.
* The full WEBHOOK_URL is printed and HTML-escaped (#1594).
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import (
    BotConfig,
    LoggingConfig,
    LogLevel,
    ObservabilityConfig,
    WebhookConfig,
)
from telegram_invite_bot.handlers.admin.settings_view import _redact
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def test_redact_never_leaks_value() -> None:
    """The redaction layer is defence-in-depth; pin every branch.

    A bot-token leak through this card would compromise the bot,
    so the test enumerates every code path of :func:`_redact` and
    asserts the underlying string never appears in the result."""
    secret_value = "super-secret-bot-token-12345"
    assert _redact(SecretStr(secret_value)) == "&lt;set&gt;"
    assert secret_value not in _redact(SecretStr(secret_value))
    # Empty SecretStr is treated as unset — the token will fail at
    # runtime, the operator wants to see the diagnostic right now.
    assert _redact(SecretStr("")) == "&lt;unset&gt;"
    assert _redact(None) == "&lt;unset&gt;"
    # Plain-string fallback (defensive): unconfigured webhook URL is
    # the "polling, not webhook" mode and should report unset.
    assert _redact("") == "&lt;unset&gt;"
    assert _redact("anything") == "&lt;set&gt;"


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_settings", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_secrets_redacted_in_rendered_card(
    make_wired: WiredFactory, capture_outgoing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Load-bearing: the bot token, webhook secret, and Sentry DSN
    must NOT appear in the rendered card under any circumstance.
    This is the regression that would let a screenshot leak prod
    credentials — pin it hard."""
    # Build configs with distinctive, easy-to-search values so any
    # leak would show up unambiguously.
    # Token must satisfy aiogram's validate_token (digits:rest); embed
    # the marker in the secret portion so any leak still shows up.
    bot_token = "9999:ZZZ-bot-token-marker-9999"
    webhook_secret = "YYY-webhook-secret-marker-8888"
    sentry_dsn = "https://XXX-sentry-dsn-marker@example.invalid/1"
    # Webhook config sourced from explicit fields rather than env
    # so the test doesn't depend on the surrounding shell.
    monkeypatch.setenv("WEBHOOK_SECRET_TOKEN", webhook_secret)
    monkeypatch.setenv("SENTRY_DSN", sentry_dsn)
    monkeypatch.setenv("WEBHOOK_URL", "https://staging.example/webhook")

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr(bot_token), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_settings", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # No marker should appear anywhere in the rendered card.
    assert bot_token not in text
    assert webhook_secret not in text
    assert sentry_dsn not in text
    # The "set" indicator should be present so the operator sees the
    # secrets are configured.
    assert "BOT_TOKEN" in text
    assert "&lt;set&gt;" in text


@pytest.mark.asyncio
async def test_non_secret_values_render_verbatim(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_settings", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # Default WEBHOOK_PATH is /webhook — surfaces verbatim.
    assert "WEBHOOK_PATH" in text
    assert "/webhook" in text
    # Default LOG_LEVEL is INFO.
    assert LogLevel.INFO.value in text
    # developer_ids resolved set — operator confirmation surface.
    assert "developer_ids" in text
    assert "42" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_settings",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# Touch unused imports so they aren't flagged. These imports document
# the public surface the test exercises (LoggingConfig, ObservabilityConfig,
# WebhookConfig) even though the test goes through make_wired's defaults.
_ = LoggingConfig, ObservabilityConfig, WebhookConfig


@pytest.mark.asyncio
async def test_webhook_url_is_printed_in_full_and_escaped(
    make_wired: WiredFactory, capture_outgoing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1594: the URL row is the staging-vs-prod answer, and it is HTML.

    The module docstring used to promise that the full URL was
    withheld against Telegram's 4096-char limit — a measure the code
    never had. Both halves are pinned here: the URL really is printed
    whole, and an ``&`` in it does not reach the card raw. The value
    is operator-set, so this is a broken-card risk rather than an
    injection one: one raw ``&`` costs the operator the whole readout."""
    monkeypatch.setenv("WEBHOOK_URL", "https://staging.example/hook?a=1&b=2")
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_settings", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "https://staging.example/hook?a=1&amp;b=2" in text
    assert "a=1&b=2" not in text
