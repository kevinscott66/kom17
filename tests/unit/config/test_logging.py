"""Loguru redactor scrubs known secrets and Telegram bot tokens.

Also covers the surrounding pieces — ``_build_filter`` (the closure
loguru installs as a record filter), ``_extract_secret`` (pulls
``SecretStr`` values out of a pydantic settings sub-model so we know
what strings to redact), and ``configure_logging`` (the end-to-end
bootstrap). These all sit on the cold-start path: a regression that
silently disables redaction means tokens leak to stderr or JSON sinks
on the very first log line, which is the exact failure this module
exists to prevent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from loguru import logger
from pydantic import SecretStr

from telegram_invite_bot.config.logging import (
    _RUNTIME_SECRETS,
    _build_filter,
    _extract_secret,
    _redact,
    configure_logging,
    register_runtime_secret,
)
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


def test_redactor_scrubs_telegram_token() -> None:
    raw = "got webhook for token 1234567:AAH0qHmF6abcdefghijklmnopqrstuvwxyz1234"
    out = _redact(raw, secret_values=())
    assert "1234567:" not in out
    assert "REDACTED_TOKEN" in out


def test_redactor_scrubs_explicit_secrets() -> None:
    raw = "deepseek key sk-deadbeef leaked into log line"
    out = _redact(raw, secret_values=("sk-deadbeef",))
    assert "sk-deadbeef" not in out
    assert "REDACTED" in out


def test_redactor_ignores_short_values() -> None:
    """Values shorter than 6 chars are not substituted (avoids false positives)."""
    raw = "user id is 42 in chat"
    out = _redact(raw, secret_values=("42",))
    assert out == raw


# ── _build_filter ────────────────────────────────────────────────────────


def test_build_filter_mutates_record_message_in_place() -> None:
    """The returned closure replaces ``record['message']`` with the
    redacted form and returns ``True`` so loguru keeps the record.

    Returning ``False`` would silently drop log lines — a regression
    that would make the bot go dark without anyone noticing.
    """
    flt = _build_filter(("supersecret123",))
    record: dict[str, Any] = {"message": "leaked supersecret123 here"}
    assert flt(record) is True
    assert "supersecret123" not in record["message"]
    assert "REDACTED" in record["message"]


# ── _extract_secret ──────────────────────────────────────────────────────


def test_extract_secret_returns_empty_for_none_group() -> None:
    """Settings can have optional sub-models (e.g. ``observability``
    is always present in practice but the guard exists for safety).
    ``None`` must short-circuit so ``configure_logging`` doesn't blow
    up on partial Settings during bootstrap.
    """
    assert _extract_secret(None) == ()


def test_extract_secret_pulls_secretstr_values_from_settings_submodel() -> None:
    """``BotConfig.token`` is a ``SecretStr`` — its plain value must
    end up in the tuple so the redactor knows the literal to scrub.
    """
    bot = BotConfig(BOT_TOKEN="123:abcdefghijklmnopqrstuvwxyz0123456789")
    out = _extract_secret(bot)
    assert "123:abcdefghijklmnopqrstuvwxyz0123456789" in out


def test_extract_secret_skips_non_secret_fields() -> None:
    """Plain str/int fields don't have ``get_secret_value`` — they
    must be skipped, not coerced. A regression that ``str()``-ifies
    every field would put e.g. ADMIN_CHAT_ID into the redactor and
    cause "0" to be globally substring-replaced in every log line.
    """
    logging_cfg = LoggingConfig()  # no SecretStr fields
    assert _extract_secret(logging_cfg) == ()


def test_extract_secret_swallows_get_secret_value_exceptions() -> None:
    """Logging bootstrap must never fail. If a custom secret-like
    object raises from ``get_secret_value`` (corrupted state, partial
    init), we drop that field and keep going.
    """

    class _BadSecret:
        def get_secret_value(self) -> str:
            raise RuntimeError("simulated corruption")

    class _Group:
        model_fields = {"broken": None}
        broken = _BadSecret()

    # Must not raise.
    assert _extract_secret(_Group()) == ()


# ── configure_logging ────────────────────────────────────────────────────


def _settings(*, json_format: bool = False) -> Settings:
    return Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN="123:abcdefghijklmnopqrstuvwxyz0123456789"),
        webhook=WebhookConfig(WEBHOOK_SECRET_TOKEN=SecretStr("hookhookhook")),
        paths=PathsConfig(),
        logging=LoggingConfig(LOG_JSON=json_format),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )


@pytest.fixture(autouse=True)
def _restore_loguru() -> Any:
    """Each test resets the loguru sink registry so previous tests
    can't leak filters / sinks across the suite. ``configure_logging``
    calls ``logger.remove()`` already; this guard handles the case
    where a test raises before that.
    """
    yield
    logger.remove()


def test_configure_logging_runs_end_to_end_with_secrets() -> None:
    """Smoke: real Settings → ``configure_logging`` installs a sink
    that redacts the configured BOT_TOKEN. We capture by replacing
    sys.stderr-bound sink with an in-memory list via loguru's own
    add() — proves the filter pipeline is wired.
    """
    settings = _settings()
    captured: list[str] = []

    configure_logging(settings)
    # Add an extra sink that observes records *after* the filter ran.
    logger.add(captured.append, level="DEBUG", format="{message}")
    logger.info("token leaked: 123:abcdefghijklmnopqrstuvwxyz0123456789")

    assert any("REDACTED" in line for line in captured)
    assert not any("abcdefghijklmnopqrstuvwxyz0123456789" in line for line in captured)


def test_configure_logging_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    """Called twice (e.g. polling-then-webhook in tests) must not
    stack sinks; ``logger.remove()`` at the top of the function is
    the contract that keeps this from doubling every log line.

    #1980: this test used to name that contract in prose and then call
    the function twice with no assertion at all — deleting the
    ``logger.remove()`` it describes left it green. Counting the line
    is the whole point: a doubled sink does not raise, it just quietly
    bills twice for every log line the process ever writes.
    """
    settings = _settings()
    configure_logging(settings)
    configure_logging(settings)
    logger.info("idempotence probe")

    assert capsys.readouterr().err.count("idempotence probe") == 1


def test_configure_logging_accepts_json_format_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``LOG_JSON=true`` has to reach loguru's ``serialize``.

    #1980: the previous body called the function and asserted nothing,
    on the stated grounds that "the JSON serialization happens inside
    loguru and isn't ours to assert on". The serialization is loguru's;
    passing the flag is ours, and that is what is checked here — by
    reading the line the sink actually wrote, because a flag that never
    reaches ``logger.add`` is indistinguishable from one that does
    until someone parses the output. Which is precisely what the log
    shipper on prod does.
    """
    configure_logging(_settings(json_format=True))
    logger.info("json probe")
    line = next(ln for ln in capsys.readouterr().err.splitlines() if "json probe" in ln)

    assert json.loads(line)["record"]["message"] == "json probe"


def test_configure_logging_leaves_plain_text_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other side of the flag: default settings must not serialize.

    Without this, the JSON assertion above could be satisfied by a sink
    hardwired to ``serialize=True``, which would be the same defect
    wearing the opposite sign.
    """
    configure_logging(_settings())
    logger.info("plain probe")
    line = next(ln for ln in capsys.readouterr().err.splitlines() if "plain probe" in ln)

    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


def test_configure_logging_redacts_secrets_from_every_subgroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every BaseModel sub-config's ``SecretStr`` field must be in the
    redaction set — not just bot/webhook/observability.

    The pre-Stage-82 implementation hardcoded three group names and
    silently missed ``AiConfig.api_key`` (DEEPSEEK_API_KEY). A logged
    DeepSeek error containing the bearer header would have leaked the
    key in plaintext. This test pins the dynamic walk: an API-key
    string that lives on ``settings.ai`` must be replaced in the
    filtered output.
    """
    # Inject DEEPSEEK_API_KEY through the env so AiConfig picks it up.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-secret-1234567890")
    monkeypatch.setenv("BOT_TOKEN", "123:abcdefghijklmnopqrstuvwxyz0123456789")
    settings = Settings(_env_file=None)
    assert settings.ai.api_key is not None  # sanity — the env wired

    captured: list[str] = []
    configure_logging(settings)
    logger.add(captured.append, level="DEBUG", format="{message}")
    logger.info("calling deepseek with key=sk-deepseek-secret-1234567890")

    # The key value must be redacted (not the literal field name).
    assert any("REDACTED" in line for line in captured)
    assert not any("sk-deepseek-secret-1234567890" in line for line in captured)


# ── M-I-6: redaction also walks record["extra"] and exception args ───────


def test_build_filter_redacts_secrets_inside_bind_extra() -> None:
    """``logger.bind(token=...).info(...)`` puts the bound value into
    ``record['extra']``. Pre-M-I-6 the filter only touched
    ``record['message']``, so the bound token survived into the JSON
    sink in plaintext. The recursive walk must scrub it.
    """
    flt = _build_filter(("supersecret123",))
    record: dict[str, Any] = {
        "message": "ok",
        "extra": {"token": "supersecret123", "nested": {"k": ["supersecret123"]}},
    }
    assert flt(record) is True
    assert record["extra"]["token"] != "supersecret123"
    assert "REDACTED" in record["extra"]["token"]
    # Nested list inside nested dict — confirms recursion.
    assert record["extra"]["nested"]["k"][0] != "supersecret123"
    assert "REDACTED" in record["extra"]["nested"]["k"][0]


def test_build_filter_redacts_telegram_token_inside_extra() -> None:
    """Even when the operator forgot to add a literal to ``_extract_secret``,
    the generic ``_TOKEN_PATTERN`` must still catch Telegram-shaped
    tokens that land in ``extra``.
    """
    flt = _build_filter(())
    record: dict[str, Any] = {
        "message": "ok",
        "extra": {"bot_token": "1234567:AAH0qHmF6abcdefghijklmnopqrstuvwxyz1234"},
    }
    flt(record)
    assert "1234567:" not in record["extra"]["bot_token"]
    assert "REDACTED_TOKEN" in record["extra"]["bot_token"]


def test_build_filter_redacts_secrets_inside_exception_args() -> None:
    """An exception whose ``args`` contains a token would be serialised
    by the loguru exception formatter with the raw value. M-I-6 mutates
    ``exc.args`` in place so the formatter sees only the redacted form.
    """

    class _FakeExcInfo:
        def __init__(self, exc: BaseException) -> None:
            self.value = exc

    exc = RuntimeError("upstream returned token supersecret123")
    flt = _build_filter(("supersecret123",))
    record: dict[str, Any] = {
        "message": "boom",
        "extra": {},
        "exception": _FakeExcInfo(exc),
    }
    flt(record)
    # Args were mutated in place — the redactor scrubbed the secret.
    assert all("supersecret123" not in str(a) for a in exc.args)
    assert any("REDACTED" in str(a) for a in exc.args)


def test_build_filter_handles_exception_with_frozen_args() -> None:
    """Some BaseException subclasses freeze ``args``. The redactor must
    not propagate AttributeError/TypeError out of the filter — logging
    setup is on the cold-start path and a raise here would prevent the
    very log line that reports the freeze.
    """

    class _FrozenExc(Exception):
        @property
        def args(self) -> tuple[str, ...]:  # type: ignore[override]
            return ("supersecret123",)

    class _FakeExcInfo:
        def __init__(self, exc: BaseException) -> None:
            self.value = exc

    flt = _build_filter(("supersecret123",))
    record: dict[str, Any] = {
        "message": "boom",
        "extra": {},
        "exception": _FakeExcInfo(_FrozenExc("supersecret123")),
    }
    # Must not raise.
    assert flt(record) is True


# -- register_runtime_secret (#1367) --------------------------------------


def test_registered_runtime_secret_reaches_an_already_built_filter() -> None:
    """A secret registered AFTER the sink was installed is still scrubbed.

    This is the whole point of #1367. ``flat_secrets`` is reflected out
    of ``Settings`` once, during ``configure_logging``; the live Crypto
    Pay token on prod exists only as an ``economy.runtime_secrets`` row
    read per call, so it can never be in that snapshot. If the filter
    closed over a frozen tuple, registering later would be a no-op and
    the token would stay in plaintext — hence the recombination inside
    ``_filter`` rather than in ``_build_filter``.
    """
    flt = _build_filter(())
    record: dict[str, Any] = {"message": "calling with runtime-only-secret", "extra": {}}

    register_runtime_secret("runtime-only-secret")

    assert flt(record) is True
    assert "runtime-only-secret" not in record["message"]
    assert "***REDACTED***" in record["message"]


def test_registered_runtime_secret_is_scrubbed_from_bind_extra() -> None:
    """``logger.bind(token=...)`` must be covered too, not just the message.

    The realistic leak is diagnostic code on the crypto path binding the
    token as structured context, which lands in ``extra`` and goes
    straight into the JSON sink.
    """
    register_runtime_secret("runtime-only-secret")
    flt = _build_filter(())
    record: dict[str, Any] = {
        "message": "crypto call failed",
        "extra": {"token": "runtime-only-secret"},
    }

    assert flt(record) is True
    assert record["extra"]["token"] == "***REDACTED***"


def test_register_runtime_secret_ignores_short_and_empty_values() -> None:
    """Short values are refused at registration, mirroring ``_redact``.

    A two-character "secret" would match inside ordinary words and turn
    every log line into redaction confetti — worse than not redacting,
    because it destroys the diagnostics the line existed for.
    """
    register_runtime_secret(None)
    register_runtime_secret("")
    register_runtime_secret("abc")

    assert not _RUNTIME_SECRETS


def test_token_pattern_catches_a_five_digit_app_id() -> None:
    """A Crypto Pay token has a FIVE-digit app id, not a Telegram-sized one.

    The pattern required ``\\d{6,}`` before the colon, aimed at Telegram
    bot tokens (8-10 digit ids). Crypto Pay's app id is five digits, so
    the one payment token actually live on prod matched nothing — and it
    is also the one absent from ``flat_secrets``, since it lives in the
    DB rather than in ``.env``. Both halves of its coverage were missing
    at once.
    """
    raw = "Crypto-Pay-API-Token: 12345:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    out = _redact(raw, secret_values=())

    assert "12345:AA" not in out
    assert "***REDACTED_TOKEN***" in out


def test_token_pattern_still_catches_a_telegram_token() -> None:
    """Loosening the digit floor must not lose the original coverage."""
    raw = "token=8123456789:AAH1234567890abcdefghijklmnopqrstuvw"

    out = _redact(raw, secret_values=())

    assert "8123456789:AAH" not in out
    assert "***REDACTED_TOKEN***" in out
