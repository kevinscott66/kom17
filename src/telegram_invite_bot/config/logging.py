"""Loguru-based logging setup with secret redaction.

Stdlib ``logging`` is intercepted and forwarded to loguru so aiogram /
SQLAlchemy / uvicorn / Flask (legacy) all funnel through one sink.

Redaction is driven by secret VALUES, discovered by reflection at
startup: :func:`_iter_settings_groups` walks every ``BaseModel``
sub-config on ``Settings`` and :func:`_extract_secret` pulls the value
out of anything exposing ``get_secret_value``. So the way to make a new
secret redacted is to declare it as a ``SecretStr`` field on a Settings
sub-config — there is no list of names to extend. This docstring used to
say the opposite and point at a ``_SECRET_ENV_NAMES`` tuple that nothing
read; two of its entries (``STRIPE_SECRET_KEY``, ``OPENWEATHER_API_KEY``)
had no matching field anywhere, which is how far it had drifted.

Secrets that never pass through ``Settings`` are the gap that reflection
cannot close: ``economy.runtime_secrets`` rows are read per call, long
after the sink was built. :func:`register_runtime_secret` is how those
get in — see :func:`telegram_invite_bot.services.payments.secret_resolver.resolve_crypto_token`.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

from loguru import logger
from pydantic import BaseModel

from telegram_invite_bot.config.settings import LoggingConfig, Settings

#: Secrets learned after ``configure_logging`` ran. ``flat_secrets`` is a
#: snapshot of Settings taken once at startup, so a value that only exists
#: in ``economy.runtime_secrets`` — the live Crypto Pay token on prod is
#: exactly that — could never reach it. The filter reads this set at WRITE
#: time, so registering a value scrubs it from every record emitted after.
_RUNTIME_SECRETS: set[str] = set()


def register_runtime_secret(value: str | None) -> None:
    """Teach the redactor a secret that did not come from ``Settings``.

    Call this wherever a secret is read from or written to the DB. The
    length floor mirrors :func:`_redact`'s: a short value would match
    everywhere and turn ordinary log lines into confetti.
    """
    if value and len(value) >= 6:
        _RUNTIME_SECRETS.add(value)


# ``<id>:<35+ url-safe chars>``. Four, not six, digits: the ``{6,}`` floor
# was aimed at Telegram bot tokens (8-10 digit ids) and silently excluded
# Crypto Pay, whose app id is five digits — the one payment token actually
# live on prod. Four keeps the pattern specific enough (a colon plus 30+
# url-safe chars is not a shape ordinary log text takes) while covering it.
_TOKEN_PATTERN = re.compile(r"\b\d{4,}:[A-Za-z0-9_-]{30,}\b")


def _redact(message: str, secret_values: tuple[str, ...]) -> str:
    redacted = _TOKEN_PATTERN.sub("***REDACTED_TOKEN***", message)
    for value in secret_values:
        if value and len(value) >= 6:
            redacted = redacted.replace(value, "***REDACTED***")
    return redacted


def _redact_recursive(value: Any, secret_values: tuple[str, ...]) -> Any:
    """Walk a value tree, replacing secret strings at every leaf.

    M-I-6: ``record["extra"]`` (loguru's ``logger.bind(...)`` payload)
    used to bypass the redactor entirely — a ``logger.bind(token=...).
    info(...)`` would serialise the raw token into JSON output. This
    helper walks dicts / lists / tuples recursively and applies
    :func:`_redact` to every string leaf. Non-string leaves pass
    through unchanged (numbers, booleans, ``None``).
    """
    if isinstance(value, str):
        return _redact(value, secret_values)
    if isinstance(value, dict):
        return {k: _redact_recursive(v, secret_values) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_recursive(v, secret_values) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_recursive(v, secret_values) for v in value)
    return value


def _build_filter(secret_values: tuple[str, ...]) -> Any:
    def _filter(record: dict[str, Any]) -> bool:
        # Recombined per record, deliberately: ``_RUNTIME_SECRETS`` grows
        # after the sink is installed, and a tuple captured at build time
        # would freeze the redactor at whatever Settings knew on startup.
        secrets = secret_values + tuple(_RUNTIME_SECRETS)
        record["message"] = _redact(record["message"], secrets)
        # M-I-6: also walk ``extra`` (loguru's bound key-value payload)
        # and the attached exception's ``args``. Without this a
        # ``logger.bind(token=...).info(...)`` or an exception whose
        # message echoed a bearer token would land in the JSON sink
        # in plaintext.
        extra = record.get("extra")
        if isinstance(extra, dict):
            record["extra"] = _redact_recursive(extra, secrets)
        exc_info = record.get("exception")
        if exc_info is not None:
            _redact_exception_args(exc_info, secrets)
        return True

    return _filter


def _redact_exception_args(exc_info: Any, secret_values: tuple[str, ...]) -> None:
    """Scrub secret leaves out of an exception's ``args`` tuple.

    Loguru attaches an ``exception`` record carrying the live
    exception; mutating ``exc.args`` in place propagates to the
    formatter, the JSON serialiser, and any downstream Sentry-style
    handler that pickles the exception.
    """
    exc = getattr(exc_info, "value", None) or getattr(exc_info, "exception", None)
    if exc is None and isinstance(exc_info, BaseException):
        exc = exc_info
    if exc is None:
        return
    args = getattr(exc, "args", None)
    if not args:
        return
    try:
        exc.args = tuple(_redact_recursive(a, secret_values) for a in args)
    except (AttributeError, TypeError):
        # Some BaseException subclasses freeze args — silently skip.
        return


class _InterceptHandler(logging.Handler):
    """Forward stdlib logging to loguru, preserving level and exc_info."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - thin shim
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(settings: Settings) -> None:
    """Configure loguru once. Safe to call multiple times (idempotent)."""

    log_cfg: LoggingConfig = settings.logging
    # Walk every ``BaseModel`` sub-group on Settings rather than a
    # hardcoded list of three names. The old approach missed
    # ``AiConfig.api_key`` (DEEPSEEK_API_KEY) — a real secret that
    # could leak through a logged DeepSeek response error. Any future
    # SecretStr field on a new sub-config is picked up automatically.
    flat_secrets = tuple(
        v for group in _iter_settings_groups(settings) for v in _extract_secret(group) if v
    )

    logger.remove()
    logger.add(
        sys.stderr,
        level=log_cfg.level.value,
        backtrace=False,
        diagnose=False,
        enqueue=False,
        filter=_build_filter(flat_secrets),
        serialize=log_cfg.json_format,
    )

    # Route stdlib loggers (aiogram, sqlalchemy, uvicorn, telebot, etc.) through loguru.
    logging.basicConfig(handlers=[_InterceptHandler()], level=log_cfg.level.value, force=True)
    for name in ("aiogram", "sqlalchemy.engine", "uvicorn", "uvicorn.error", "telebot"):
        logging.getLogger(name).handlers = [_InterceptHandler()]
        logging.getLogger(name).propagate = False

    logger.bind(component="bootstrap").info(
        "logging configured: level={level} json={json} env={env}",
        level=log_cfg.level.value,
        json=log_cfg.json_format,
        env=settings.app_env.value,
    )


def _iter_settings_groups(settings: Settings) -> tuple[BaseModel, ...]:
    """Yield every BaseModel sub-config attached to ``settings``.

    Walks the class-level fields so newly-added sub-configs (e.g. a
    future ``PaymentsConfig``) start contributing secrets automatically
    — we never want a new SecretStr field to bypass redaction because
    someone forgot to extend a hardcoded list.
    """
    groups: list[BaseModel] = []
    for field_name in type(settings).model_fields:
        attr = getattr(settings, field_name, None)
        if isinstance(attr, BaseModel):
            groups.append(attr)
    return tuple(groups)


def _extract_secret(group: Any) -> tuple[str, ...]:
    """Pull SecretStr values out of a settings sub-model for redaction."""

    if group is None:
        return ()
    values: list[str] = []
    # ``model_fields`` is class-level in pydantic v2.11+ — instance access
    # is deprecated and would emit ``PydanticDeprecatedSince211`` on every
    # bootstrap, polluting the very first log line we configure.
    model_fields = getattr(type(group), "model_fields", {})
    for field_name in model_fields:
        attr = getattr(group, field_name, None)
        get_secret = getattr(attr, "get_secret_value", None)
        if callable(get_secret):
            try:
                values.append(get_secret())
            except Exception:  # noqa: BLE001, S112 — defensive, never fail logging setup
                continue
    return tuple(values)
