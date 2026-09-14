"""End-to-end ``/admin_certfp``.

Pins:

* Non-developer → silent drop.
* Card renders the structure (handshake, version, cipher, fingerprint,
  subject/issuer, expiry) when the probe succeeds — verified against
  an injected fake snapshot rather than a live handshake.
* Healthy snapshot → no ⚠ on the not_after row (cry-wolf prevention).
* Near-expiry snapshot → exactly one row ⚠ on not_after (single-row
  marker, not double).
* Failed handshake → exactly one row ⚠ on the handshake row, error
  class name surfaced (routing hint pattern).
* Fingerprint colon-grouping matches ``openssl x509 -fingerprint`` —
  diffing against out-of-band references is the diagnostic this card
  exists for, so wire-format parity is load-bearing.
* ``_fmt_dn`` flattens ssl's tuple-of-tuples to ``a=b, c=d``.
* ``_parse_asn1_time`` round-trips ssl's ``"%b %d %H:%M:%S %Y %Z"``.
* Group invocation → router-level private filter rejects.
* Probe-sourced values reach the card HTML-escaped — the DN fields
  are attacker-controlled exactly under the MITM this card exists
  to detect, and unescaped markup there makes Telegram reject the
  card outright (#1585).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin import certfp as certfp_module
from telegram_invite_bot.handlers.admin.certfp import (
    _CertSnapshot,
    _expiry_concerning,
    _fmt_dn,
    _fmt_fingerprint,
    _parse_asn1_time,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _healthy_snapshot(*, not_after: datetime | None = None) -> _CertSnapshot:
    return _CertSnapshot(
        fingerprint_sha256="a" * 64,
        subject="CN=api.telegram.org",
        issuer="CN=Test CA",
        not_before=datetime(2025, 1, 1, tzinfo=UTC),
        not_after=not_after or datetime.now(UTC) + timedelta(days=180),
        tls_version="TLSv1.3",
        cipher="TLS_AES_256_GCM_SHA384",
        latency_ms=42.0,
        error=None,
    )


def _error_snapshot(error: str = "SSLCertVerificationError") -> _CertSnapshot:
    return _CertSnapshot(
        fingerprint_sha256=None,
        subject=None,
        issuer=None,
        not_before=None,
        not_after=None,
        tls_version=None,
        cipher=None,
        latency_ms=12.0,
        error=error,
    )


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_certfp", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_structure_renders(
    make_wired: WiredFactory, capture_outgoing: Any, monkeypatch: Any
) -> None:
    """Happy-path render: every load-bearing field appears. The probe
    is monkeypatched so the test is hermetic — a CI runner without
    egress would otherwise see TimeoutError every run."""

    async def fake_probe() -> _CertSnapshot:
        return _healthy_snapshot()

    monkeypatch.setattr(certfp_module, "_probe", fake_probe)
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_certfp", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "TLS cert probe" in text
    assert "api.telegram.org:443" in text
    assert "handshake:" in text
    assert "TLSv1.3" in text
    assert "TLS_AES_256_GCM_SHA384" in text
    assert "sha256:" in text
    assert "CN=api.telegram.org" in text
    assert "CN=Test CA" in text


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
            "/admin_certfp",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_healthy_snapshot_no_warn() -> None:
    """Healthy cert (180 days out) → zero ⚠ on data rows. The legend
    in the footer always names ⚠, so we partition before counting —
    same row-warn pattern as every other admin card."""
    rendered = _render(_healthy_snapshot())
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 0


def test_near_expiry_renders_single_warn() -> None:
    """Cert expiring in 5 days → exactly one ⚠ on the not_after row.
    Not the handshake row (handshake succeeded), and not double-marked."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    snap = _healthy_snapshot(not_after=now + timedelta(days=5))
    rendered = _render(snap, now=now)
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    # The marker must be on the not_after line, not handshake.
    not_after_line = [line for line in head.splitlines() if "not_after" in line][0]
    assert "⚠" in not_after_line


def test_failed_handshake_renders_single_warn_with_error_class() -> None:
    """Handshake failure → single ⚠ on the handshake row + error class
    name surfaced as a routing hint to /admin_dns + /admin_ssl. The
    error-class-name pattern is mirrored across every probe card."""
    rendered = _render(_error_snapshot("SSLCertVerificationError"))
    head, _, _ = rendered.partition("<i>⚠")
    assert head.count("⚠") == 1
    assert "SSLCertVerificationError" in head
    # No cert-detail rows when the handshake failed.
    assert "sha256:" not in head
    assert "not_after" not in head


def test_expiry_concerning_threshold() -> None:
    """Boundary pin around ``_EXPIRY_CONCERNING_DAYS``. 13 days out is
    concerning; 30 days out is not. Missing not_after is never
    concerning (cry-wolf prevention on absent data)."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert _expiry_concerning(_healthy_snapshot(not_after=now + timedelta(days=13)), now=now)
    assert not _expiry_concerning(_healthy_snapshot(not_after=now + timedelta(days=30)), now=now)
    # Error path: never concerning, the operator already has bigger
    # things to worry about.
    assert not _expiry_concerning(_error_snapshot(), now=now)


def test_fmt_fingerprint_matches_openssl_form() -> None:
    """``openssl x509 -fingerprint`` uses colon-separated hex pairs;
    operators diff against that form. Wire-format parity is load-
    bearing — a future tweak to grouping would silently break the
    cross-reference workflow this card exists for."""
    assert _fmt_fingerprint("aabbccdd") == "aa:bb:cc:dd"
    # Real-length SHA-256 produces 32 pairs.
    digest = "0" * 64
    grouped = _fmt_fingerprint(digest)
    assert grouped.count(":") == 31
    assert all(len(p) == 2 for p in grouped.split(":"))


def test_fmt_dn_flattens_ssl_tuple_of_tuples() -> None:
    """ssl returns DNs as ``((('CN', 'foo'),), (('O', 'bar'),))``.
    We flatten to ``CN=foo, O=bar`` so the render is one line."""
    rdns = ((("CN", "api.telegram.org"),), (("O", "Telegram"),))
    assert _fmt_dn(rdns) == "CN=api.telegram.org, O=Telegram"
    # Defensive: None / empty render as None, not crash.
    assert _fmt_dn(None) is None
    assert _fmt_dn(()) is None


def test_parse_asn1_time_round_trip() -> None:
    """ssl emits notBefore/notAfter as ``"Jan 15 12:00:00 2025 GMT"``.
    We parse to tz-aware UTC datetime; unparseable strings yield None
    rather than raising (the render branches on None)."""
    parsed = _parse_asn1_time("Jan 15 12:00:00 2025 GMT")
    assert parsed == datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
    assert _parse_asn1_time(None) is None
    assert _parse_asn1_time("not a date") is None


def test_render_escapes_the_remote_sourced_dn_fields() -> None:
    """subject/issuer are flattened straight out of ``getpeercert()``.

    A peer that can put ``<`` or ``&`` into a DN is by definition the
    MITM this card exists to detect. Unescaped, Telegram rejects the
    malformed HTML and the operator gets NO card — the diagnostic dies
    at the one moment it was written for. So the entities must arrive
    escaped, and the raw markup must not survive anywhere (#1585).
    """
    snap = _CertSnapshot(
        fingerprint_sha256="b" * 64,
        subject="CN=<b>evil</b> & co",
        issuer="CN=</code><i>proxy</i>",
        not_before=datetime(2025, 1, 1, tzinfo=UTC),
        not_after=datetime.now(UTC) + timedelta(days=180),
        tls_version="TLSv1.3<x>",
        cipher="AES&CBC",
        latency_ms=42.0,
        error=None,
    )
    card = _render(snap)

    assert "CN=&lt;b&gt;evil&lt;/b&gt; &amp; co" in card
    assert "CN=&lt;/code&gt;&lt;i&gt;proxy&lt;/i&gt;" in card
    assert "TLSv1.3&lt;x&gt;" in card
    assert "AES&amp;CBC" in card
    # Nothing hostile survived: the card's own tags are the only ones left.
    for hostile in ("<b>evil", "</code><i>", "<x>", "AES&CBC"):
        assert hostile not in card


def test_render_escapes_the_failure_text() -> None:
    """The failure row interpolates the exception's own text.

    Same class as the DN fields and on the busier path — a handshake
    against a hostile endpoint is precisely how an operator gets here.
    """
    card = _render(_error_snapshot(error="SSLError(<b>bad</b> & ugly)"))

    assert "SSLError(&lt;b&gt;bad&lt;/b&gt; &amp; ugly)" in card
    assert "<b>bad</b>" not in card
