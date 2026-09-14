"""``/admin_certfp`` — TLS peer-certificate fingerprint of api.telegram.org.

Complements /admin_dns (resolver-side reachability), /admin_ssl
(OpenSSL runtime + trust store + TLS-protocol flags), and
/admin_telegram_api (live API round-trip) by surfacing the
**handshake-side** posture: which leaf cert is api.telegram.org
presenting to THIS process right now, and what are its SHA-256
fingerprint, subject, issuer and expiry?

Why an operator wants this:

* MITM detection. A captive portal / misconfigured corporate proxy
  / hostile upstream that intercepts TLS will present a cert
  signed by a CA the host trusts (otherwise the bot wouldn't be
  talking at all) — but the leaf fingerprint will differ from the
  one Telegram actually serves. The card surfaces the fingerprint
  so an operator can diff against a known-good value or
  cross-reference with a trusted out-of-band source (mobile data,
  another region).
* Cert-rotation awareness. Telegram rotates the api.telegram.org
  cert on its own schedule; aiohttp's connection-reuse pool can
  briefly hold a connection on the old cert after rotation,
  surfacing as a confusing "the call worked five minutes ago but
  not now" pattern. The card forces a fresh handshake and reports
  the current leaf.
* Expiry visibility. notBefore / notAfter are the canonical fields
  an operator wants to glance at; a soon-to-expire cert from the
  upstream is a leading indicator of a service-level event.

Cost: one full TLS handshake (TCP + ClientHello + ServerHello +
cert chain + key exchange + Finished) against the real
api.telegram.org, then immediate close. No data exchanged after
handshake. Hard 5 s timeout caps the worst case.

Posture: silent-drop for non-devs, private-only at the router
level. The cert chain we surface is public information (anyone
running ``openssl s_client`` against the same host sees the same
data), so there's no privacy cost — but the diagnostic is
operator-only because the diagnosis pattern around it is.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import ssl
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.certfp")


# Hard host:port. The card's purpose is specifically the Telegram
# API endpoint; a future variant might accept an argument but the
# diagnostic value is highest when the operator doesn't have to
# remember what to ask for.
_PROBE_HOST = "api.telegram.org"
_PROBE_PORT = 443


# Per-call timeout. A handshake against a healthy CDN POP completes
# in 100-500 ms; 5 s is decisive for a wedged endpoint while leaving
# enough headroom for transient TCP retransmits.
_PROBE_TIMEOUT_S = 5.0


# Days-to-expiry below which we mark ⚠. 14 days is generous — most
# upstreams rotate well before expiry, so seeing < 14 days remaining
# on api.telegram.org's leaf means either the rotation got skipped
# or the host's clock is off (cross-check /admin_clock).
_EXPIRY_CONCERNING_DAYS = 14


class _CertSnapshot:
    """Captured TLS-handshake result.

    On success we capture the fields an operator can act on:
    fingerprint (diff-able), subject + issuer (chain identity),
    notBefore + notAfter (expiry windows), TLS version + cipher
    (handshake-time posture). On failure ``error`` carries the
    exception class name, same routing-hint pattern as /admin_dns.
    """

    __slots__ = (
        "cipher",
        "error",
        "fingerprint_sha256",
        "issuer",
        "latency_ms",
        "not_after",
        "not_before",
        "subject",
        "tls_version",
    )

    def __init__(
        self,
        *,
        fingerprint_sha256: str | None,
        subject: str | None,
        issuer: str | None,
        not_before: datetime | None,
        not_after: datetime | None,
        tls_version: str | None,
        cipher: str | None,
        latency_ms: float,
        error: str | None,
    ) -> None:
        self.fingerprint_sha256 = fingerprint_sha256
        self.subject = subject
        self.issuer = issuer
        self.not_before = not_before
        self.not_after = not_after
        self.tls_version = tls_version
        self.cipher = cipher
        self.latency_ms = latency_ms
        self.error = error


def _fmt_dn(rdns: tuple[tuple[tuple[str, str], ...], ...] | None) -> str | None:
    """Format a Distinguished Name (subject or issuer) as ``a=b, c=d``.

    Python's ssl module returns DNs as a tuple-of-tuples; we flatten
    to a single comma-separated string so the render is one line per
    field. Defensive ``None`` handling because ssl's typing is loose
    and we'd rather render "unknown" than raise during the
    diagnostic.

    Deliberately does NOT escape: this is a formatter, and the value
    is remote-sourced, so escaping belongs to whoever puts it into
    markup. :func:`_render` is that place and does it (#1585).
    """
    if not rdns:
        return None
    parts: list[str] = []
    for rdn in rdns:
        for key, value in rdn:
            parts.append(f"{key}={value}")
    return ", ".join(parts) if parts else None


def _parse_asn1_time(asn1: str | None) -> datetime | None:
    """Convert ssl's ``"%b %d %H:%M:%S %Y %Z"`` notBefore/notAfter."""
    if not asn1:
        return None
    try:
        # ssl returns e.g. "Jan 15 12:00:00 2025 GMT".
        return datetime.strptime(asn1, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    except ValueError:
        return None


async def _probe(
    host: str = _PROBE_HOST,
    port: int = _PROBE_PORT,
    timeout_s: float = _PROBE_TIMEOUT_S,
) -> _CertSnapshot:
    """Open a TLS connection, capture the peer cert + handshake info,
    immediately close. No data exchanged after the handshake.

    Uses :func:`asyncio.open_connection` with an ``ssl`` context
    rather than the lower-level socket plumbing — keeps the diagnostic
    on the same async path the live Bot session uses, so any
    misconfiguration of the default trust store surfaces consistently
    here and there.
    """
    start = time.monotonic()
    ctx = ssl.create_default_context()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=port, ssl=ctx, server_hostname=host),
            timeout=timeout_s,
        )
    except TimeoutError:
        return _CertSnapshot(
            fingerprint_sha256=None,
            subject=None,
            issuer=None,
            not_before=None,
            not_after=None,
            tls_version=None,
            cipher=None,
            latency_ms=(time.monotonic() - start) * 1000.0,
            error="TimeoutError",
        )
    except (OSError, ssl.SSLError) as exc:
        return _CertSnapshot(
            fingerprint_sha256=None,
            subject=None,
            issuer=None,
            not_before=None,
            not_after=None,
            tls_version=None,
            cipher=None,
            latency_ms=(time.monotonic() - start) * 1000.0,
            error=type(exc).__name__,
        )

    try:
        # Reach into the underlying SSLObject to pull both the parsed
        # cert dict and the DER bytes (for the SHA-256 fingerprint).
        # getpeercert(binary_form=True) returns the DER bytes; the
        # parsed dict requires a separate call.
        sslobj = writer.get_extra_info("ssl_object")
        peercert_dict = sslobj.getpeercert() if sslobj is not None else None
        peercert_der = sslobj.getpeercert(binary_form=True) if sslobj is not None else None
        tls_version = sslobj.version() if sslobj is not None else None
        cipher_info = sslobj.cipher() if sslobj is not None else None
        cipher_name = cipher_info[0] if cipher_info else None
    finally:
        writer.close()
        # We're tearing down anyway; close-side errors are not the
        # diagnostic the operator is here for.
        with contextlib.suppress(OSError, ssl.SSLError):
            await writer.wait_closed()
        # Reader is implicitly closed by writer; reference kept to
        # keep the variable live for clarity.
        del reader

    fingerprint = hashlib.sha256(peercert_der).hexdigest() if peercert_der else None
    subject = _fmt_dn(peercert_dict.get("subject") if peercert_dict else None)
    issuer = _fmt_dn(peercert_dict.get("issuer") if peercert_dict else None)
    not_before = _parse_asn1_time(peercert_dict.get("notBefore") if peercert_dict else None)
    not_after = _parse_asn1_time(peercert_dict.get("notAfter") if peercert_dict else None)

    return _CertSnapshot(
        fingerprint_sha256=fingerprint,
        subject=subject,
        issuer=issuer,
        not_before=not_before,
        not_after=not_after,
        tls_version=tls_version,
        cipher=cipher_name,
        latency_ms=(time.monotonic() - start) * 1000.0,
        error=None,
    )


def _expiry_concerning(snap: _CertSnapshot, *, now: datetime | None = None) -> bool:
    """``True`` if notAfter is within ``_EXPIRY_CONCERNING_DAYS`` AND known.

    Missing notAfter (parse failed) is informational, not a ⚠ — we
    can't claim "expiring soon" without the data. Mirrors the
    cry-wolf posture from /admin_memory / /admin_tempdir on missing
    fields.
    """
    if snap.not_after is None or snap.error is not None:
        return False
    reference = now or datetime.now(UTC)
    delta = snap.not_after - reference
    return delta.days < _EXPIRY_CONCERNING_DAYS


def _fmt_fingerprint(hex_digest: str) -> str:
    """Render the SHA-256 hex digest as colon-grouped pairs.

    `aa:bb:cc…` is the form ``openssl x509 -fingerprint`` produces,
    and the form security advisories quote. Diffing against an
    out-of-band reference is the diagnostic this card exists for, so
    matching the canonical wire-format matters.
    """
    return ":".join(hex_digest[i : i + 2] for i in range(0, len(hex_digest), 2))


def _render(snap: _CertSnapshot, *, now: datetime | None = None) -> str:
    """Build the card. Every probe-sourced value goes through ``html``.

    The card is sent with the bot's default HTML parse mode, so a
    ``<`` or ``&`` inside any interpolated value is markup, not
    text. Four of the values below come off the wire —
    ``subject`` and ``issuer`` are flattened straight out of
    ``getpeercert()`` by :func:`_fmt_dn` (which deliberately does
    not escape: formatting and rendering are separate jobs), and
    ``error`` is an exception's own text.

    The host is the constant ``api.telegram.org``, so a peer that
    can put markup into a DN is by definition the MITM this card
    exists to detect — and that is exactly when unescaped output
    is worst: Telegram rejects the malformed HTML and the operator
    gets no card at all, i.e. the diagnostic dies at the one moment
    it was written for. ``tls_version`` and ``cipher`` come from
    OpenSSL's own string tables and are escaped for uniformity, not
    because they are hostile (#1585).
    """
    lines = ["🔏 <b>TLS cert probe</b>", ""]
    lines.append(f"  • <b>host:</b> <code>{_PROBE_HOST}:{_PROBE_PORT}</code>")

    if snap.error is not None:
        lines.append(
            f"  • <b>handshake:</b> <code>failed</code> "
            f"<i>({html.escape(snap.error)}, {snap.latency_ms:.1f} ms)</i> ⚠"
        )
        lines.append("")
        lines.append(
            "<i>⚠ TLS handshake failed. Check /admin_dns first "
            "(resolution before handshake), then /admin_ssl for "
            "the OpenSSL build + trust store. SSLCertVerificationError "
            "is the classic stale-CA-bundle / MITM signal.</i>"
        )
        return "\n".join(lines)

    lines.append(f"  • <b>handshake:</b> <code>ok</code> <i>({snap.latency_ms:.1f} ms)</i>")
    if snap.tls_version:
        lines.append(f"  • <b>tls version:</b> <code>{html.escape(snap.tls_version)}</code>")
    if snap.cipher:
        lines.append(f"  • <b>cipher:</b> <code>{html.escape(snap.cipher)}</code>")

    if snap.fingerprint_sha256:
        lines.append(f"  • <b>sha256:</b> <code>{_fmt_fingerprint(snap.fingerprint_sha256)}</code>")
    if snap.subject:
        lines.append(f"  • <b>subject:</b> <code>{html.escape(snap.subject)}</code>")
    if snap.issuer:
        lines.append(f"  • <b>issuer:</b> <code>{html.escape(snap.issuer)}</code>")
    if snap.not_before:
        lines.append(f"  • <b>not_before:</b> <code>{snap.not_before.isoformat()}</code>")
    if snap.not_after:
        expiry_marker = " ⚠" if _expiry_concerning(snap, now=now) else ""
        lines.append(
            f"  • <b>not_after:</b> <code>{snap.not_after.isoformat()}</code>{expiry_marker}"
        )

    lines.append("")
    lines.append(
        f"<i>⚠ markers: handshake failed (cross-check /admin_dns + "
        f"/admin_ssl), or cert expires within "
        f"{_EXPIRY_CONCERNING_DAYS} days (Telegram rotates well "
        f"before; remaining &lt; 14 days means rotation got skipped "
        f"or this host's clock is off — see /admin_clock).</i>"
    )
    return "\n".join(lines)


async def handle_admin_certfp(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_certfp; silently dropped"
        )
        return
    snap = await _probe()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        error=snap.error,
        fingerprint=snap.fingerprint_sha256,
        latency_ms=snap.latency_ms,
    ).info("/admin_certfp rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.certfp")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_certfp(message, settings)

    router.message.register(_entry, Command("admin_certfp", ignore_case=True))
    return router
