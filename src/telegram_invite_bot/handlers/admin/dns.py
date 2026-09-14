"""``/admin_dns`` — DNS resolution probe for the Telegram API endpoint.

Complements /admin_ssl (OpenSSL runtime + TLS posture) and
/admin_hashlib (crypto-algorithm posture) by surfacing the **network
prerequisite** that has to work before TLS or HTTP can: can this host
actually resolve ``api.telegram.org`` right now, and how long does it
take?

Why an operator wants this:

* "Why is the bot not delivering?" — the legacy webhook delivery
  chain is opaque about WHY a callback fails. A 5-second DNS
  timeout to ``api.telegram.org`` looks identical to a TLS error
  in the Telegram-side delivery logs (both surface as "no
  response"). Surfacing the resolve latency here lets the operator
  rule DNS in or out before chasing the harder failure modes.
* "Did we just lose DNS?" — systemd-resolved restarts, /etc/resolv.conf
  rewrites by a DHCP client, container-runtime DNS-policy changes
  (Docker's default 127.0.0.11 stub) — any of these can break
  resolution silently. A latency-shifted resolve (300ms instead
  of the usual 5ms) is the canary; an outright failure is the
  diagnosis.
* "Are we hitting the right Telegram POP?" — Telegram's API is
  fronted by an anycast pool. The actual A records returned tell
  you which DC the host is steering toward, which on a multi-
  region deploy is the difference between sub-100ms RTT and a
  trans-continental hop. Cross-references with /admin_hostinfo's
  fqdn/uname for the geographic context.

The probe runs :func:`asyncio.get_event_loop().getaddrinfo` rather
than blocking ``socket.getaddrinfo`` — a sync getaddrinfo would
freeze the event loop for the full DNS timeout (5 s default on
glibc), which on a webhook host means dropped updates during the
diagnostic. Using the async variant keeps the diagnostic non-
disruptive.

Silent-drop for non-devs, private-only at the router level. Same
posture as every other ``/admin_*``.
"""

from __future__ import annotations

import asyncio
import socket
import time
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.dns")


# The host we probe. Hard-coded rather than read from settings:
# this card's purpose is specifically Telegram-API reachability,
# not a general-purpose DNS-debugging tool. A future variant might
# accept an argument; not worth the surface for now.
_PROBE_HOST = "api.telegram.org"


# Above this we emit ⚠ — 500 ms to resolve a cached anycast host
# is a real anomaly (typical is 1-30 ms). Below the threshold we
# render the latency as a number without a marker; cry-wolf
# prevention.
_LATENCY_CONCERNING_MS = 500.0


# Per-attempt timeout. Hard cap on how long the diagnostic itself
# can block — the operator typed /admin_dns to learn something,
# and a 5-second wait for the answer is itself a failure mode.
# 3 seconds is generous for a healthy resolver, decisive for a
# broken one.
_PROBE_TIMEOUT_S = 3.0


class _DNSSnapshot:
    """Captured DNS probe result.

    ``addresses`` is the list of unique IP-string forms returned by
    getaddrinfo (one entry may show up multiple times under
    different socket types — we dedupe at capture time so the
    render can just iterate). ``error`` is the exception class name
    on failure (same routing-hint posture as /admin_tempdir's
    writability probe), ``None`` on success.
    """

    __slots__ = ("addresses", "error", "host", "latency_ms")

    def __init__(
        self,
        *,
        host: str,
        addresses: list[str],
        latency_ms: float,
        error: str | None,
    ) -> None:
        self.host = host
        self.addresses = addresses
        self.latency_ms = latency_ms
        self.error = error


async def _probe(host: str = _PROBE_HOST, timeout_s: float = _PROBE_TIMEOUT_S) -> _DNSSnapshot:
    """Run a single getaddrinfo with hard timeout.

    We measure wall-clock latency around the await. ``asyncio.wait_for``
    bounds the worst case so a fully-broken resolver doesn't pin the
    diagnostic open. On timeout the snapshot reports ``TimeoutError``
    via the error-class-name field; the operator's diagnosis path is
    the same as for ``socket.gaierror`` (resolver-side), which we
    also surface by class name.
    """
    loop = asyncio.get_event_loop()
    start = time.monotonic()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM),
            timeout=timeout_s,
        )
    except TimeoutError:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return _DNSSnapshot(host=host, addresses=[], latency_ms=elapsed_ms, error="TimeoutError")
    except OSError as exc:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return _DNSSnapshot(
            host=host,
            addresses=[],
            latency_ms=elapsed_ms,
            error=type(exc).__name__,
        )
    elapsed_ms = (time.monotonic() - start) * 1000.0

    # Dedupe addresses — getaddrinfo returns one entry per
    # (family, type, proto) combination; we want unique IP strings.
    # Preserve first-seen order so the render is stable across runs
    # (set() ordering would be random in older CPython).
    seen: set[str] = set()
    addrs: list[str] = []
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else ""
        if addr and addr not in seen:
            seen.add(addr)
            addrs.append(addr)

    return _DNSSnapshot(host=host, addresses=addrs, latency_ms=elapsed_ms, error=None)


def _latency_concerning(snap: _DNSSnapshot) -> bool:
    """``True`` if latency exceeds the threshold AND the probe succeeded.

    On a failed probe the latency reflects timeout / error path, not
    "slow resolution" — flagging it as ⚠-slow would be misleading.
    The error itself is the ⚠ on the failed path.
    """
    if snap.error is not None:
        return False
    return snap.latency_ms > _LATENCY_CONCERNING_MS


def _render(snap: _DNSSnapshot) -> str:
    lines = ["🌐 <b>DNS probe</b>", ""]
    lines.append(f"  • <b>host:</b> <code>{snap.host}</code>")

    if snap.error is not None:
        lines.append(f"  • <b>resolve:</b> <code>failed</code> <i>({snap.error})</i> ⚠")
        lines.append(f"  • <b>latency:</b> <code>{snap.latency_ms:.1f} ms</code>")
    else:
        latency_marker = " ⚠" if _latency_concerning(snap) else ""
        lines.append(
            f"  • <b>resolve:</b> <code>ok</code> "
            f"({len(snap.addresses)} address"
            f"{'es' if len(snap.addresses) != 1 else ''})"
        )
        lines.append(f"  • <b>latency:</b> <code>{snap.latency_ms:.1f} ms</code>{latency_marker}")
        if snap.addresses:
            lines.append("")
            lines.append("  <b>addresses:</b>")
            for addr in snap.addresses:
                lines.append(f"    • <code>{addr}</code>")

    lines.append("")
    lines.append(
        f"<i>⚠ markers: resolve failed (TimeoutError / gaierror / "
        f"NXDOMAIN — check /etc/resolv.conf, systemd-resolved, "
        f"container DNS-policy), or latency above "
        f"{_LATENCY_CONCERNING_MS:.0f} ms (anycast cache hit should "
        f"be &lt; 30 ms — a 500+ ms resolve usually means the "
        f"primary resolver is unreachable and we're failing over to "
        f"the secondary).</i>"
    )
    return "\n".join(lines)


async def handle_admin_dns(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_dns; silently dropped"
        )
        return
    snap = await _probe()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        latency_ms=snap.latency_ms,
        error=snap.error,
        addresses=len(snap.addresses),
    ).info("/admin_dns rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.dns")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_dns(message, settings)

    router.message.register(_entry, Command("admin_dns", ignore_case=True))
    return router
