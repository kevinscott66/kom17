"""End-to-end ``/admin_dns``.

Pins:

* Non-developer → silent drop.
* Successful resolve → ok + latency + address list (mocked
  via monkeypatch on the live ``_probe`` helper).
* Failed resolve → error class surfaced, ⚠ on the resolve row.
* Slow resolve (above the threshold) → latency ⚠.
* Healthy resolve under the threshold → bare. Cry-wolf prevention.
* Failed-probe latency does NOT also get a latency-⚠ (the resolve-⚠
  is the warning; double-marking would be noise).
* Probe against localhost succeeds (real-host smoke).
* Probe against an unresolvable host returns an error-class hint.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.dns import (
    _LATENCY_CONCERNING_MS,
    _DNSSnapshot,
    _probe,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _snap(**overrides: Any) -> _DNSSnapshot:
    defaults: dict[str, Any] = {
        "host": "api.telegram.org",
        "addresses": ["149.154.167.220", "149.154.167.99"],
        "latency_ms": 12.3,
        "error": None,
    }
    defaults.update(overrides)
    return _DNSSnapshot(**defaults)


def _row_warn_count(rendered: str) -> int:
    head, _, _legend = rendered.partition("<i>⚠")
    return head.count("⚠")


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_dns", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(
    make_wired: WiredFactory,
    capture_outgoing: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card invokes the real probe by default; monkeypatch the
    probe to a deterministic snapshot so the test is hermetic and
    doesn't depend on the CI runner's network."""

    async def _fake_probe() -> _DNSSnapshot:
        return _snap()

    monkeypatch.setattr("telegram_invite_bot.handlers.admin.dns._probe", _fake_probe)
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_dns", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "DNS probe" in text
    assert "api.telegram.org" in text
    assert "149.154.167.220" in text


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
            "/admin_dns",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_failed_resolve_surfaces_error() -> None:
    """Probe failure → resolve row carries ⚠ + error-class hint.
    Routing-hint posture mirrors /admin_tempdir's writable_error."""
    rendered = _render(_snap(addresses=[], error="gaierror", latency_ms=12.0))
    assert "failed" in rendered
    assert "gaierror" in rendered
    assert _row_warn_count(rendered) >= 1


def test_render_failed_resolve_no_double_latency_warn() -> None:
    """On a failed probe the slow-latency ⚠ MUST NOT fire — the
    resolve-failure ⚠ is the warning; double-marking would dilute
    the triage signal."""
    rendered = _render(
        _snap(
            addresses=[],
            error="TimeoutError",
            latency_ms=_LATENCY_CONCERNING_MS + 500,
        )
    )
    # Exactly one row-level ⚠ — the resolve-failed row.
    assert _row_warn_count(rendered) == 1


def test_render_slow_latency_warns() -> None:
    """Above-threshold latency on a successful probe → ⚠ on the
    latency row. Catches the "primary resolver down, failing over"
    pattern."""
    rendered = _render(_snap(latency_ms=_LATENCY_CONCERNING_MS + 100))
    assert _row_warn_count(rendered) >= 1


def test_render_healthy_state_no_warnings() -> None:
    """Successful resolve under the threshold → bare. Cry-wolf
    prevention; mirrors every other admin card."""
    rendered = _render(_snap())
    assert _row_warn_count(rendered) == 0


@pytest.mark.asyncio
async def test_probe_against_localhost() -> None:
    """``localhost`` resolves on essentially every host (POSIX guarantees
    it via /etc/hosts). Validates the probe end-to-end without any
    external network — keeps the test hermetic on CI."""
    snap = await _probe(host="localhost", timeout_s=2.0)
    # We don't care WHICH address — IPv4 127.0.0.1 vs IPv6 ::1 varies
    # by host config. Just that the probe succeeded.
    assert snap.error is None
    assert snap.addresses
    assert snap.latency_ms >= 0


@pytest.mark.asyncio
async def test_probe_against_invalid_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that cannot resolve must come back as an error
    snapshot with the exception class name surfaced. This is the
    diagnosis path the card exists for.

    The failure is injected rather than taken from the real resolver:
    a ``.invalid`` name is only guaranteed to fail on a resolver that
    plays by the rules, and captive DNS, corporate resolvers and the
    fake-IP mode of most VPN clients all answer it with an address of
    their own — which used to fail this test on the developer's
    machine while saying nothing about ``_probe``.
    """

    async def boom(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", boom)

    snap = await _probe(host="this-host-must-not-exist.invalid", timeout_s=2.0)
    assert snap.error == "gaierror"
    assert snap.addresses == []
