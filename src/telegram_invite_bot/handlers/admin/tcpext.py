"""``/admin_tcpext`` — TCP extended counters from /proc/net/netstat.

Pair-of-pairs with the rest of the L3/L4 surface. /admin_sysctl
shows configuration (``somaxconn``, ``tcp_max_syn_backlog``,
``tcp_fin_timeout``, …). /admin_netconns shows per-connection state
right now (TIME_WAIT, ESTAB census). /admin_route shows L3 reachability.

What's still missing is the **counter evidence**: did the kernel
actually overflow the listen queue? Did it retransmit SYNs because
of an upstream problem? Did it abort connections because of memory
pressure? Those are cumulative-since-boot integers in
/proc/net/netstat under the ``TcpExt:`` line — invisible to every
other card.

The single highest-signal field is ``ListenOverflows``. Any value
greater than zero means the kernel filled the accept queue and
dropped at least one inbound SYN — either because the app didn't
``accept()`` fast enough or because ``net.core.somaxconn`` is too
small. It's the smoking gun for the textbook accept-queue tuning
failure. We ⚠ on it.

Other counters render as decoration. Some are noisy by design on
WAN-facing hosts (``TCPLostRetransmit``, ``TCPSynRetrans``) — a
non-zero value there is normal under any packet loss and would be
classic cry-wolf if we marked it. The disclaimer footer makes that
boundary explicit.

Naming: the file in /proc is ``netstat`` but the command name
``/admin_netstat`` would collide mentally with the ``netstat(8)``
CLI tool (which surfaces a totally different cross-section).
``/admin_tcpext`` matches the actual line prefix in the file
(``TcpExt:``) and is unambiguous in the help index.

Format reminder: /proc/net/netstat alternates header-line / value-
line pairs, two pairs per file (TcpExt and IpExt). We parse both
but only render the curated TcpExt subset; IpExt is captured so a
future card can extend without re-parsing. Forward-compat: unknown
header keys are kept verbatim, so a kernel that adds a new counter
doesn't crash the parser — it just doesn't appear in the curated
render until we add it.

Same posture as every other admin card — silent-drop, private-only,
pure stdlib, hermetic via keyword-only path injection.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.tcpext")


_NETSTAT_PATH = Path("/proc/net/netstat")


# Curated TcpExt fields rendered in the card. Order is operational
# priority — ListenOverflows first because it's the ⚠ field, then
# retransmit family, then abort/memory pressure family. Anything not
# in this tuple is still parsed and kept in the snapshot for future
# extension, just not surfaced in the body.
_CURATED_TCPEXT: tuple[tuple[str, str], ...] = (
    ("ListenOverflows", "accept-queue overflow — somaxconn or app accept() too slow ⚠"),
    ("ListenDrops", "total drops while in LISTEN — superset of overflows"),
    ("SyncookiesSent", "SYN cookies sent — accept queue under SYN-flood-like pressure"),
    ("SyncookiesRecv", "SYN cookies validated — completing the cookie handshake"),
    ("TCPSynRetrans", "SYN retransmits — outbound reach issues / slow handshakes"),
    ("TCPTimeouts", "RTO timeouts — generic transport instability indicator"),
    ("TCPLostRetransmit", "retransmits later lost — multi-hop path degradation"),
    ("TCPAbortOnMemory", "connections aborted under memory pressure ⚠"),
    ("TCPAbortOnTimeout", "connections aborted after RTO exhaustion"),
    ("TCPFastOpenListenOverflow", "TFO accept-queue overflow — independent of ListenOverflows"),
)


# ⚠ trigger fields — strict subset of curated. ANY non-zero value
# in this set fires the single ⚠ summary. Kept narrow on purpose:
# we want one extremely high-confidence trigger, not a forest of
# them that erodes operator trust.
_WARN_FIELDS: frozenset[str] = frozenset(
    {
        "ListenOverflows",
        "TCPAbortOnMemory",
    }
)


class _TcpExtSnapshot:
    """Captured /proc/net/netstat.

    * ``tcpext`` — dict of TcpExt counter name → int.
    * ``ipext`` — dict of IpExt counter name → int (captured but
      not currently rendered; kept so a future IP-layer card can
      reuse the parse).
    * ``available`` — False on macOS dev / non-procfs container.
    """

    __slots__ = ("available", "ipext", "tcpext")

    def __init__(
        self,
        *,
        tcpext: dict[str, int],
        ipext: dict[str, int],
        available: bool,
    ) -> None:
        self.tcpext = tcpext
        self.ipext = ipext
        self.available = available


def _parse_netstat(text: str) -> tuple[dict[str, int], dict[str, int]]:
    """Parse /proc/net/netstat header/value-line pairs.

    File layout is two pairs:

        TcpExt: <keys…>
        TcpExt: <values…>
        IpExt:  <keys…>
        IpExt:  <values…>

    Header and value lines share the same prefix. We pair them by
    consecutive same-prefix lines. Non-integer values render as a
    missing key (degrade-don't-crash) — happens on out-of-spec
    kernels but the rest of the line stays usable.
    """
    tcpext: dict[str, int] = {}
    ipext: dict[str, int] = {}
    pending_header: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        prefix = parts[0]
        rest = parts[1:]
        # First line for a prefix is the header (names); second is
        # the values. We use the pending_header dict keyed by prefix
        # to find the pairing.
        if prefix not in pending_header:
            # First sighting — assume header. Values can't appear
            # before header in well-formed files; if they do, the
            # header-line check below (non-int detection) will catch
            # it and we'll skip the malformed pair.
            pending_header[prefix] = rest
            continue
        keys = pending_header.pop(prefix)
        # Pair up keys with values. Length-mismatch: zip-shortest
        # so a kernel that adds a counter mid-line doesn't crash —
        # we lose the trailing values until headers catch up.
        target = tcpext if prefix == "TcpExt:" else ipext if prefix == "IpExt:" else None
        if target is None:
            continue
        for key, value in zip(keys, rest, strict=False):
            try:
                target[key] = int(value)
            except ValueError:
                # Non-int value — skip just this field, keep the rest.
                continue
    return tcpext, ipext


def _capture(*, path: Path = _NETSTAT_PATH) -> _TcpExtSnapshot:
    """Read /proc/net/netstat + build a snapshot.

    Keyword-only path parameter so tests can inject a tmp file —
    same hermetic-fixture pattern as every other diagnostic card.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _TcpExtSnapshot(tcpext={}, ipext={}, available=False)
    tcpext, ipext = _parse_netstat(text)
    return _TcpExtSnapshot(tcpext=tcpext, ipext=ipext, available=True)


def _triggered_warns(snap: _TcpExtSnapshot) -> tuple[str, ...]:
    """Names of warn-fields with non-zero values.

    Empty tuple means no warn. Empty also when snap.available is
    False — absence of data is NOT a warning (cry-wolf prevention,
    same posture as every other card)."""
    if not snap.available:
        return ()
    return tuple(name for name in _WARN_FIELDS if snap.tcpext.get(name, 0) > 0)


def _render(snap: _TcpExtSnapshot) -> str:
    lines = ["📡 <b>TCP extended counters (/proc/net/netstat TcpExt)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/net/netstat unavailable on this host — Linux-"
            "only card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if not snap.tcpext:
        lines.append(
            "  <i>TcpExt section absent — kernel does not expose the "
            "extended TCP counters here. Unusual; check kernel build.</i>"
        )
        return "\n".join(lines)

    warns = _triggered_warns(snap)
    if warns:
        # Hoist the ⚠ above the table — operator's eye lands at the
        # top. Same ergonomic as /admin_route no-default-route.
        joined = ", ".join(f"<code>{w}</code>" for w in warns)
        lines.append(
            f"  ⚠ <b>non-zero warn counter(s):</b> {joined} — kernel "
            f"either filled the accept queue or aborted connections "
            f"under memory pressure since boot. Investigate the app's "
            f"accept() loop and net.core.somaxconn (see /admin_sysctl)."
        )
        lines.append("")

    lines.append("  <b>Curated counters:</b>")
    for key, desc in _CURATED_TCPEXT:
        value = snap.tcpext.get(key)
        if value is None:
            # Field absent on this kernel — show explicitly rather
            # than silently dropping, so an operator on an older
            # kernel sees what's missing.
            lines.append(f"  • <code>{key}</code> = <i>n/a</i> — {desc}")
        else:
            lines.append(f"  • <code>{key}</code> = <code>{value}</code> — {desc}")

    lines.append("")
    lines.append(
        f"<i>TcpExt total fields parsed: {len(snap.tcpext)} "
        f"(IpExt: {len(snap.ipext)} also captured but not surfaced "
        f"here). Counters are cumulative since boot — a small "
        f"non-zero value on a long-running host may be ancient "
        f"history.</i>"
    )
    lines.append("")
    lines.append(
        "<i>⚠ markers: ListenOverflows or TCPAbortOnMemory non-zero "
        "— the unambiguous &quot;accept queue overflowed&quot; / "
        "&quot;OOM-aborted connections&quot; signals. Retransmit / "
        "timeout counters are NOT marked (any WAN-facing host sees "
        "non-zero values under transient packet loss; marking them "
        "would be cry-wolf). See /admin_sysctl for somaxconn config, "
        "/admin_netconns for live socket state.</i>"
    )
    return "\n".join(lines)


async def handle_admin_tcpext(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_tcpext; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    warns = _triggered_warns(snap)
    log.bind(
        user_id=user.id,
        available=snap.available,
        tcpext_field_count=len(snap.tcpext),
        ipext_field_count=len(snap.ipext),
        listen_overflows=snap.tcpext.get("ListenOverflows"),
        tcp_abort_on_memory=snap.tcpext.get("TCPAbortOnMemory"),
        warn_count=len(warns),
    ).info("/admin_tcpext rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.tcpext")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_tcpext(message, settings)

    router.message.register(_entry, Command("admin_tcpext", ignore_case=True))
    return router
