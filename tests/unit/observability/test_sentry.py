"""Sentry init contract.

We don't talk to real Sentry — we monkeypatch ``sentry_sdk.init`` and
assert the kwargs we pass. The arguments are the load-bearing surface:
get them wrong and either a) prod crashes don't fan out (release/env
tag missing), or b) dev exceptions page the on-call rotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import SecretStr, ValidationError

from telegram_invite_bot import __version__
from telegram_invite_bot.config.settings import AppEnv, ObservabilityConfig
from telegram_invite_bot.observability import init_sentry

if TYPE_CHECKING:
    from telegram_invite_bot.config.settings import Settings


@dataclass(frozen=True, slots=True)
class _StubObservability:
    sentry_dsn: SecretStr | None
    # #1995: defaulted so the DSN-focused tests below stay unchanged —
    # the point of the field is that it can be raised, not that every
    # test has to say it isn't.
    sentry_traces_sample_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class _StubSettings:
    """Duck-typed Settings surface.

    ``init_sentry`` only reaches into ``settings.observability.sentry_dsn``
    and ``settings.app_env`` — building a real pydantic Settings here
    drags in the prod-mode validators (WEBHOOK_SECRET_TOKEN/URL must
    be set) that have nothing to do with what's under test. The stub
    keeps the test focused and survives future Settings refactors.
    """

    observability: _StubObservability
    app_env: AppEnv


def _settings_with_dsn(dsn: str | None, app_env: AppEnv = AppEnv.DEV) -> _StubSettings:
    return _StubSettings(
        observability=_StubObservability(
            sentry_dsn=SecretStr(dsn) if dsn is not None else None,
        ),
        app_env=app_env,
    )


def _init(settings: _StubSettings) -> bool:
    """Call the initialiser with the duck-typed stub.

    ``_StubSettings`` is deliberately not a ``Settings`` — see its
    docstring for why — so every call site drew the same mypy
    ``arg-type`` complaint. Saying it once here, where the reason is
    written down, beats four identical unexplained warnings.
    """
    return init_sentry(cast("Settings", settings))


def test_no_init_when_dsn_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default (empty DSN) MUST be a no-op. A spurious init with
    DSN=None would crash the SDK and bring down startup — worse than
    no Sentry at all.
    """
    called: list[Any] = []

    def fake_init(**kwargs: Any) -> None:
        called.append(kwargs)

    monkeypatch.setattr("sentry_sdk.init", fake_init)
    assert _init(_settings_with_dsn(None)) is False
    assert called == []


def test_no_init_when_dsn_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only DSN counts as unset. Operators routinely leave
    placeholder ``SENTRY_DSN=`` in .env templates; stripping in the
    initialiser avoids a CamelCase-empty SDK init call that would
    register the SDK in a half-state.
    """
    called: list[Any] = []
    monkeypatch.setattr("sentry_sdk.init", lambda **kw: called.append(kw))
    assert _init(_settings_with_dsn("   ")) is False
    assert called == []


def test_init_passes_release_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_init(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("sentry_sdk.init", fake_init)
    ok = _init(_settings_with_dsn("https://abc@o0.ingest.sentry.io/1", AppEnv.PROD))
    assert ok is True
    # Release pins regressions to deploy; environment scopes alert
    # routing. Both are load-bearing for the on-call experience.
    assert captured["release"] == f"telegram-invite-bot@{__version__}"
    assert captured["environment"] == "prod"
    assert captured["dsn"] == "https://abc@o0.ingest.sentry.io/1"
    # Default trace rate is 0 — we send errors, not spans. A non-zero
    # default would silently burn the Sentry quota.
    assert captured["traces_sample_rate"] == 0.0
    # PII default must stay off — Telegram updates carry user IDs.
    assert captured["send_default_pii"] is False
    # And frame locals must stay in the process. Sentry's own scrubber
    # works off variable *names*, and ours are not on its list:
    # ``expected_value`` holds the Telegram secret token in
    # ``webhook/security.py``, ``expected`` holds GUIDES_EDIT_SECRET in
    # the guide editor. Any exception raised beneath either frame would
    # otherwise carry the cleartext to a third party.
    assert captured["include_local_variables"] is False


def test_init_includes_loguru_and_asyncio_integrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without LoguruIntegration, ``log.exception(...)`` calls go to
    stdout but NOT to Sentry — that's exactly the path our global
    error handler uses, so its events would never surface as alerts.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr("sentry_sdk.init", lambda **kw: captured.update(kw))
    _init(_settings_with_dsn("https://x@o0.ingest.sentry.io/2"))
    names = {type(i).__name__ for i in captured["integrations"]}
    assert "LoguruIntegration" in names
    assert "AsyncioIntegration" in names


def test_incident_trace_rate_reaches_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1995: the rate an operator sets is the rate Sentry gets.

    The comment beside this argument has always told the reader to raise
    ``SENTRY_TRACES_SAMPLE_RATE`` during an incident. Until now the call
    passed a literal ``0.0``, so following that instruction produced
    exactly as many spans as ignoring it — and the person finding that
    out was mid-incident. This test is the difference between the two.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr("sentry_sdk.init", lambda **kw: captured.update(kw))

    settings = _StubSettings(
        observability=_StubObservability(
            sentry_dsn=SecretStr("https://abc@o0.ingest.sentry.io/1"),
            sentry_traces_sample_rate=0.25,
        ),
        app_env=AppEnv.PROD,
    )
    assert _init(settings) is True
    assert captured["traces_sample_rate"] == 0.25


def test_the_traces_rate_is_settable_and_bounded() -> None:
    """The knob is a real alias, and refuses values Sentry cannot use.

    Out-of-range is rejected at load rather than passed through: the SDK
    treats a rate above 1.0 or below 0.0 as meaningless, which would put
    an operator back where #1995 found them — a variable that is set and
    does nothing.
    """
    assert ObservabilityConfig().sentry_traces_sample_rate == 0.0
    assert ObservabilityConfig(SENTRY_TRACES_SAMPLE_RATE=0.5).sentry_traces_sample_rate == 0.5
    for refused in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            ObservabilityConfig(SENTRY_TRACES_SAMPLE_RATE=refused)
