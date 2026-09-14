"""Webhook secret-token verification."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
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
from telegram_invite_bot.webhook.security import verify_secret_token


def _settings(secret: str | None) -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:abc"),
        webhook=WebhookConfig(
            WEBHOOK_SECRET_TOKEN=SecretStr(secret) if secret is not None else None
        ),
        paths=PathsConfig(),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )


def _request(headers: dict[str, str]) -> Any:
    req = MagicMock()
    req.headers = headers
    return req


def test_no_secret_configured_is_noop() -> None:
    verify_secret_token(_request({}), _settings(secret=None))


def test_matching_secret_passes() -> None:
    verify_secret_token(
        _request({"X-Telegram-Bot-Api-Secret-Token": "s3cret"}),
        _settings(secret="s3cret"),
    )


def test_missing_header_when_secret_required_403() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_secret_token(_request({}), _settings(secret="s3cret"))
    assert exc.value.status_code == 403


def test_wrong_secret_403() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_secret_token(
            _request({"X-Telegram-Bot-Api-Secret-Token": "wrong"}),
            _settings(secret="s3cret"),
        )
    assert exc.value.status_code == 403


# ── SEC audit: empty WEBHOOK_SECRET_TOKEN must never authenticate ──────


def test_empty_secret_token_normalized_to_none() -> None:
    """An empty (or whitespace) configured secret normalises to None, so
    it routes through the explicit no-secret path instead of comparing
    against "" (which a no-header request would match)."""
    assert _settings(secret="").webhook.secret_token is None
    assert _settings(secret="   ").webhook.secret_token is None


def test_prod_empty_secret_token_fails_fast() -> None:
    """In prod an empty secret must crash startup (the normalisation turns
    it into None, and ``_require_secret_token_in_prod`` then refuses to
    boot) rather than silently disabling webhook auth."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(
            app_env=AppEnv.PROD,
            bot=BotConfig(BOT_TOKEN="123:abc"),
            webhook=WebhookConfig(
                WEBHOOK_SECRET_TOKEN=SecretStr(""),
                WEBHOOK_URL="https://example.com/webhook",
            ),
            paths=PathsConfig(),
            logging=LoggingConfig(),
            observability=ObservabilityConfig(),
            features=FeatureFlags(),
        )


def test_empty_secretstr_reaching_verify_never_authenticates() -> None:
    """Defence in depth: even if an empty SecretStr bypasses the settings
    normalisation and reaches ``verify_secret_token``, a request with NO
    header (provided == "") must NOT authenticate — it gets 403, not the
    ``compare_digest("", "")`` True that the bug allowed."""
    fake = MagicMock()
    fake.webhook.secret_token = SecretStr("")
    with pytest.raises(HTTPException) as no_header:
        verify_secret_token(_request({}), fake)
    assert no_header.value.status_code == 403
    with pytest.raises(HTTPException) as empty_header:
        verify_secret_token(_request({"X-Telegram-Bot-Api-Secret-Token": ""}), fake)
    assert empty_header.value.status_code == 403


# ── A non-ASCII header must be a 403, not an unhandled 500 ────────────


def test_non_ascii_header_is_rejected_not_crashed() -> None:
    """Bytes 0x80-0xFF in the header must not escape as a TypeError.

    Starlette decodes header values with latin-1, so ``\\xff`` reaches
    ``verify_secret_token`` as a non-ASCII ``str``. ``compare_digest``
    raises ``TypeError`` on those, and since the app registers no
    ``Exception`` handler that used to surface as a 500 with a full
    uvicorn traceback — an unauthenticated caller's cheapest way to
    write unbounded log volume, and one that never incremented a single
    rejection metric.
    """
    with pytest.raises(HTTPException) as exc:
        verify_secret_token(
            _request({"X-Telegram-Bot-Api-Secret-Token": "\xff"}),
            _settings(secret="s3cret"),
        )
    assert exc.value.status_code == 403


def test_rejection_detail_does_not_reveal_which_guard_tripped() -> None:
    """Both 403 branches answer identically.

    "misconfigured" told an anonymous caller that the deployment's
    secret is empty — i.e. that this layer authenticates nobody right
    now. The operator reads that distinction from the logs instead.
    """
    empty = MagicMock()
    empty.webhook.secret_token = SecretStr("")
    with pytest.raises(HTTPException) as misconfigured:
        verify_secret_token(_request({}), empty)
    with pytest.raises(HTTPException) as wrong:
        verify_secret_token(
            _request({"X-Telegram-Bot-Api-Secret-Token": "wrong"}),
            _settings(secret="s3cret"),
        )
    assert misconfigured.value.detail == wrong.value.detail


# ── A CONFIGURED secret outside Telegram's charset must not crash either ──


def test_non_ascii_configured_secret_denies_all_instead_of_crashing() -> None:
    """The mirror image of the header case, and a worse failure.

    ``compare_digest`` raises ``TypeError`` when EITHER side is
    non-ASCII, so a configured secret carrying, say, a Cyrillic
    character turns every single update — including Telegram's own —
    into an unhandled 500 with a full uvicorn traceback. The bot would
    be off the air while its config looked complete.
    ``Settings._webhook_secret_token_charset`` now rejects such a value at
    settings load; this pins the last line of defence for anything that
    reaches ``verify_secret_token`` without passing through Settings.
    """
    fake = MagicMock()
    fake.webhook.secret_token = SecretStr("ключ")
    for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "anything"}):
        with pytest.raises(HTTPException) as exc:
            verify_secret_token(_request(headers), fake)
        assert exc.value.status_code == 403
        assert exc.value.detail == "forbidden"
