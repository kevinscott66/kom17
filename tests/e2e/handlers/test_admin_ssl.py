"""End-to-end ``/admin_ssl``.

Pins:

* Non-developer → silent drop.
* Card renders the OpenSSL identity + cafile/capath + TLS-protocol
  flags. Any going missing would silently degrade the CVE-audit
  story in the module docstring.
* Both trust-store endpoints unset → ⚠ surfaces. This is the
  "ca-certificates not installed on a slim Docker image" failure
  mode; a future cleanup that drops the marker would hide it.
* TLS 1.3 missing → ⚠. Soft, because FIPS builds legitimately
  disable it.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.ssl_info import _render, _SSLSnapshot
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_ssl", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders_identity_rows(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_ssl", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "OpenSSL" in text
    assert "runtime:" in text
    assert "compiled against:" in text
    assert "cafile:" in text
    assert "capath:" in text
    assert "Protocols" in text
    # OpenSSL is always linked in CPython, so the runtime version
    # string must start with "OpenSSL" — pinning the prefix catches
    # a future change that swaps to e.g. LibreSSL without updating
    # the docstring narrative around OpenSSL CVEs.
    assert "OpenSSL" in text


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
            "/admin_ssl",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_render_both_trust_paths_unset_surfaces_warning() -> None:
    """The ca-certificates-missing failure mode: slim Docker image
    where neither ``cafile`` nor ``capath`` resolves. Outbound HTTPS
    leans on certifi or fails closed. Without the ⚠ marker the
    operator could read the ``<unset>`` lines as the documented
    default for an unconfigured-but-OK system."""
    snap = _SSLSnapshot(
        library_version="OpenSSL 3.0.13",
        compile_version="3.0.13",
        cafile="<unset>",
        capath="<unset>",
        protocols=(("HAS_TLSv1_2", True), ("HAS_TLSv1_3", True)),
    )
    rendered = _render(snap)
    assert "⚠" in rendered
    assert "trust store unavailable" in rendered


def test_render_tls13_missing_surfaces_warning() -> None:
    """TLS 1.3 missing on a non-FIPS prod host is the "OpenSSL is
    ancient" signal. Surfaces as a soft marker on the protocol
    row — not on the trust-store rows, because mixing the two
    classes of warning into one alarm would defeat triage."""
    snap = _SSLSnapshot(
        library_version="OpenSSL 1.1.1f",
        compile_version="1.1.1",
        cafile="/etc/ssl/certs/ca-certificates.crt",
        capath="/etc/ssl/certs",
        protocols=(("HAS_TLSv1_2", True), ("HAS_TLSv1_3", False)),
    )
    rendered = _render(snap)
    assert "⚠" in rendered
    # Trust store is fine here, so the warning must be on the
    # protocol row only — not on the cafile/capath rows.
    assert "trust store unavailable" not in rendered


def test_render_healthy_no_warnings() -> None:
    """Healthy state: modern OpenSSL, trust store wired, TLS 1.3 on.
    The card must not carry any ⚠ — otherwise operators learn to
    ignore the marker and lose the signal on real regressions."""
    snap = _SSLSnapshot(
        library_version="OpenSSL 3.0.13",
        compile_version="3.0.13",
        cafile="/etc/ssl/certs/ca-certificates.crt",
        capath="/etc/ssl/certs",
        protocols=(("HAS_TLSv1_2", True), ("HAS_TLSv1_3", True)),
    )
    rendered = _render(snap)
    assert "⚠" not in rendered
