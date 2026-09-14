"""End-to-end ``/admin_telegram_api``.

Pins:

* Non-developer → silent drop.
* Successful probe (both calls) → username, id, webhook url,
  pending count surfaced; no ⚠ in body (cry-wolf prevention).
* getMe failure → ⚠ on the getMe row with error-class hint.
* getWebhookInfo failure → ⚠ on the webhook row.
* Pending backlog above threshold → ⚠ on the pending row.
* Last-error message surfaced + truncated to 200 chars (one bad-
  day Telegram error must NOT blow the 4096-char card budget).
* Long-polling mode (empty webhook url) renders as hint, NOT
  warning — that's a deploy-mode signal, not a problem.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.methods import GetMe, GetWebhookInfo
from aiogram.types import Chat, WebhookInfo
from aiogram.types import Message as TelegramMessage
from aiogram.types import User as TelegramUser
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.telegram_api import (
    _PENDING_CONCERNING,
    _ApiSnapshot,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _snap(**overrides: Any) -> _ApiSnapshot:
    defaults: dict[str, Any] = {
        "get_me_user_id": 12345,
        "get_me_username": "test_bot",
        "get_me_latency_ms": 45.0,
        "get_me_error": None,
        "webhook_url": "https://example.com/webhook",
        "webhook_pending": 0,
        "webhook_max_connections": 40,
        "webhook_last_error_date": None,
        "webhook_last_error_message": None,
        "webhook_latency_ms": 67.0,
        "webhook_error": None,
    }
    defaults.update(overrides)
    return _ApiSnapshot(**defaults)


def _row_warn_count(rendered: str) -> int:
    head, _, _legend = rendered.partition("<i>⚠")
    return head.count("⚠")


def _patch_telegram(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[dict[str, Any]],
    *,
    get_me_raises: bool = False,
    webhook_raises: bool = False,
    webhook_url: str = "https://example.com/wh",
    pending: int = 0,
    last_error: str | None = None,
) -> None:
    """Patch ``bot.session.make_request`` to fake both probes plus
    the outgoing SendMessage so the card renders end-to-end without
    a network."""

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if isinstance(method, GetMe):
            if get_me_raises:
                raise RuntimeError("simulated getMe failure")
            return TelegramUser(id=99, is_bot=True, first_name="probe", username="probe_bot")
        if isinstance(method, GetWebhookInfo):
            if webhook_raises:
                raise RuntimeError("simulated getWebhookInfo failure")
            return WebhookInfo(
                url=webhook_url,
                has_custom_certificate=False,
                pending_update_count=pending,
                max_connections=40,
                last_error_message=last_error,
            )
        if type(method).__name__ == "SendMessage":
            sink.append({"text": method.text})
            return TelegramMessage(
                message_id=2,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        raise AssertionError(f"unexpected Telegram call: {type(method).__name__}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


@pytest.mark.asyncio
async def test_silent_for_non_developer(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent: list[dict[str, Any]] = []
    _patch_telegram(bot, monkeypatch, sent)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_telegram_api", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders_happy_path(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent: list[dict[str, Any]] = []
    _patch_telegram(bot, monkeypatch, sent)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_telegram_api", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Telegram API probe" in text
    assert "@probe_bot" in text
    assert "https://example.com/wh" in text
    # Healthy state must render without per-row ⚠ markers — cry-wolf.
    assert _row_warn_count(text) == 0


@pytest.mark.asyncio
async def test_card_handles_get_me_failure(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token revoked / network error must NOT raise; card surfaces
    the error class as a routing hint and still renders."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent: list[dict[str, Any]] = []
    _patch_telegram(bot, monkeypatch, sent, get_me_raises=True)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_telegram_api", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "failed" in text
    assert "RuntimeError" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent: list[dict[str, Any]] = []
    _patch_telegram(bot, monkeypatch, sent)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_telegram_api",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_pending_backlog_warns() -> None:
    """Above threshold the pending row carries ⚠ — catches the
    stuck-consumer pattern documented in the module docstring."""
    rendered = _render(_snap(webhook_pending=_PENDING_CONCERNING + 5))
    assert _row_warn_count(rendered) >= 1


def test_render_last_error_surfaces_and_truncates() -> None:
    """A multi-kB last_error from Telegram must NOT blow the
    4096-char card budget. Trimmed at 200 chars + ⚠."""
    long_err = "A" * 1000
    rendered = _render(_snap(webhook_last_error_message=long_err))
    assert "A" * 200 in rendered
    assert "A" * 300 not in rendered
    assert _row_warn_count(rendered) >= 1


def test_render_long_polling_mode_no_warning() -> None:
    """Empty webhook url means the bot is on long-polling — that's
    a deploy-mode signal, NOT a problem. Surface as hint without ⚠."""
    rendered = _render(_snap(webhook_url=None))
    assert "long-polling" in rendered
    assert _row_warn_count(rendered) == 0


def test_render_healthy_no_warnings() -> None:
    rendered = _render(_snap())
    assert _row_warn_count(rendered) == 0


def test_render_both_calls_failed() -> None:
    """Both probes failed simultaneously — every section carries its
    own ⚠ + error class. Operator sees two distinct diagnoses
    (token issue vs webhook issue) rather than one merged blob."""
    rendered = _render(
        _snap(
            get_me_error="TimeoutError",
            get_me_user_id=None,
            get_me_username=None,
            webhook_error="TelegramAPIError",
            webhook_url=None,
            webhook_pending=None,
            webhook_max_connections=None,
        )
    )
    assert "TimeoutError" in rendered
    assert "TelegramAPIError" in rendered
    assert _row_warn_count(rendered) >= 2


def test_render_escapes_telegram_supplied_strings() -> None:
    """Every string in this card comes from Telegram, not from us.

    Under the bot-wide ``parse_mode=HTML`` a single unescaped ``<`` in
    ``last_error_message`` (Telegram quotes the failing response back
    at us) makes Telegram reject the whole card, so the developer
    debugging a webhook outage gets silence instead of the error they
    came for — exactly the failure class fixed for the other
    ``/admin_*`` cards.
    """
    from tests.telegram_html import telegram_html_errors

    rendered = _render(
        _snap(
            get_me_username="bot<script>",
            webhook_url="https://ex.com/<hook>?a=1&b=2",
            webhook_last_error_message='Wrong response: <html lang="ru">',
        )
    )
    assert not telegram_html_errors(rendered), rendered
    # Escaped, not stripped: the operator still sees what Telegram said.
    assert "&lt;html" in rendered, rendered


def test_render_long_polling_hint_is_not_monospace() -> None:
    """The "no username" branch is a hint, not a value.

    It used to render as ``<code><i>none</i></code>`` — italic
    monospace, reading like a bot literally named "none". The webhook
    branch right below it already kept its hint outside ``<code>``.
    """
    rendered = _render(_snap(get_me_username=None))
    assert "<b>username:</b> <i>none</i>" in rendered, rendered
    assert "<code><i>none</i></code>" not in rendered, rendered
