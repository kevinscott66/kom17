"""``/admin_netdev`` — per-interface counters from /proc/net/dev.

Completes the L2/L3/L4 network diagnostic spine:

* L2 NIC (this card) — per-interface bytes/packets/errs/drop.
* L3 routing — /admin_route.
* L4 sockets — /admin_netconns (state) + /admin_tcpext (counters).

Non-zero error counters on a non-loopback interface are the
unambiguous "your NIC or driver is misbehaving" smoking gun. They
don't show up anywhere else in the diagnostic surface — TCP-level
retransmits (/admin_tcpext) can be caused by upstream loss far
from this host, but RX/TX errs are by definition the kernel
counting frames its own driver couldn't process.

Card layout: one row per interface with bytes/packets in both
directions plus the four "something went wrong" counters (rx_errs,
rx_drop, tx_errs, tx_drop). Loopback is rendered (operator wants
to see it; lo errors would be a much deeper system problem) but
explicitly excluded from the ⚠ predicate — counters on lo are
either zero or noise.

⚠ predicate: rx_errs > 0 OR tx_errs > 0 on any non-loopback
interface. Drops are NOT ⚠'d because some drop categories are
normal — multicast filtering on busy LANs, packets to closed
sockets — and marking them would be cry-wolf. Errors are
unambiguous: the driver saw the frame on the wire and couldn't
process it. The disclaimer footer makes this boundary explicit.

Forward-compat: /proc/net/dev has had 16 stable counter columns
since 2.6 (8 receive + 8 transmit). Short lines or non-integer
fields degrade to None per-counter rather than crashing the row.
Unknown interface names are kept verbatim — kernel allows
arbitrary names within IFNAMSIZ.

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


log = logger.bind(component="handlers.admin.netdev")


_NETDEV_PATH = Path("/proc/net/dev")


# Container hosts can have a long tail of veth* interfaces from
# every container's network namespace bridge — 50+ rows on a
# busy podman/k8s box. Cap with explicit hidden-count, same logic
# as /admin_route and /admin_mounts.
_RENDER_ROW_CAP = 30


class _NetdevRow:
    """One /proc/net/dev interface line.

    The four "something went wrong" counters are the operationally
    interesting ones — bytes/packets are kept for context but the
    ⚠ predicate only looks at errs. Counter values are None when
    parsing failed for that specific field (degrade-don't-crash);
    the rest of the row stays usable."""

    __slots__ = (
        "iface",
        "rx_bytes",
        "rx_drop",
        "rx_errs",
        "rx_packets",
        "tx_bytes",
        "tx_drop",
        "tx_errs",
        "tx_packets",
    )

    def __init__(
        self,
        *,
        iface: str,
        rx_bytes: int | None,
        rx_packets: int | None,
        rx_errs: int | None,
        rx_drop: int | None,
        tx_bytes: int | None,
        tx_packets: int | None,
        tx_errs: int | None,
        tx_drop: int | None,
    ) -> None:
        self.iface = iface
        self.rx_bytes = rx_bytes
        self.rx_packets = rx_packets
        self.rx_errs = rx_errs
        self.rx_drop = rx_drop
        self.tx_bytes = tx_bytes
        self.tx_packets = tx_packets
        self.tx_errs = tx_errs
        self.tx_drop = tx_drop


class _NetdevSnapshot:
    """Captured /proc/net/dev.

    * ``rows`` — every parsed interface.
    * ``available`` — False when /proc/net/dev is unreadable
      (macOS dev, container without /proc).
    """

    __slots__ = ("available", "rows")

    def __init__(
        self,
        *,
        rows: tuple[_NetdevRow, ...],
        available: bool,
    ) -> None:
        self.rows = rows
        self.available = available


def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _parse_netdev(text: str) -> tuple[_NetdevRow, ...]:
    """Parse /proc/net/dev.

    First two lines are headers (``Inter-|...|...`` and
    ``face |bytes packets errs drop ...``) — we skip them. Each
    interface line is ``ifname: c1 c2 c3 c4 c5 c6 c7 c8 c9 c10
    c11 c12 c13 c14 c15 c16`` where the first 8 are receive and the
    last 8 are transmit. Of those we keep bytes/packets/errs/drop
    in both directions; fifo/frame/compressed/multicast and the
    transmit-side colls/carrier are not surfaced (operator rarely
    needs them and the card stays scannable)."""
    rows: list[_NetdevRow] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Headers don't contain ':' before the counter values; the
        # interface line always has ``ifname:`` as the first token.
        if ":" not in line:
            continue
        # Split on the first ':' to isolate the interface name from
        # the counter columns. Some kernel versions emit
        # ``  eth0: 12345...`` with leading spaces and no space
        # before the colon — leading whitespace already stripped.
        name_part, _, counter_part = line.partition(":")
        iface = name_part.strip()
        # The header lines from /proc/net/dev contain '|' which is
        # never in a real ifname — skip defensively in case a future
        # kernel version reformats the header.
        if "|" in iface or not iface:
            continue
        counters = counter_part.split()
        if len(counters) < 16:
            continue
        rx_bytes = _to_int(counters[0])
        rx_packets = _to_int(counters[1])
        rx_errs = _to_int(counters[2])
        rx_drop = _to_int(counters[3])
        tx_bytes = _to_int(counters[8])
        tx_packets = _to_int(counters[9])
        tx_errs = _to_int(counters[10])
        tx_drop = _to_int(counters[11])
        rows.append(
            _NetdevRow(
                iface=iface,
                rx_bytes=rx_bytes,
                rx_packets=rx_packets,
                rx_errs=rx_errs,
                rx_drop=rx_drop,
                tx_bytes=tx_bytes,
                tx_packets=tx_packets,
                tx_errs=tx_errs,
                tx_drop=tx_drop,
            )
        )
    return tuple(rows)


def _capture(*, path: Path = _NETDEV_PATH) -> _NetdevSnapshot:
    """Read /proc/net/dev + build a snapshot. Keyword-only path
    parameter for hermetic test injection."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _NetdevSnapshot(rows=(), available=False)
    return _NetdevSnapshot(rows=_parse_netdev(text), available=True)


def _interfaces_with_errors(snap: _NetdevSnapshot) -> tuple[str, ...]:
    """Names of non-loopback interfaces with rx_errs > 0 or tx_errs > 0.

    Loopback is intentionally excluded — errors there would be a
    much deeper system problem and the rare case isn't worth the
    false-positive risk on every healthy host that has lo errs=0.
    Returns empty tuple when snapshot is unavailable (cry-wolf
    prevention — absence of data is NOT a warning)."""
    if not snap.available:
        return ()
    triggered: list[str] = []
    for row in snap.rows:
        if row.iface == "lo":
            continue
        rx = row.rx_errs or 0
        tx = row.tx_errs or 0
        if rx > 0 or tx > 0:
            triggered.append(row.iface)
    return tuple(triggered)


def _fmt_bytes(value: int | None) -> str:
    """Compact byte rendering. Counters wrap at 2^32 on 32-bit
    kernels but everything we deploy on is 64-bit so we don't
    handle wrap — operator sees the raw value if it looks too small
    on a long-running host (wrap would manifest as MB after months
    of uptime; spotting it manually is fine for a diagnostic card)."""
    if value is None:
        return "?"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}GB"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}MB"
    if value >= 1_000:
        return f"{value / 1_000:.2f}KB"
    return f"{value}B"


def _fmt_int(value: int | None) -> str:
    return "?" if value is None else str(value)


def _render(snap: _NetdevSnapshot) -> str:
    lines = ["🔌 <b>Per-interface counters (/proc/net/dev)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/net/dev unavailable on this host — Linux-"
            "only card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        lines.append(
            "  <i>no interfaces parsed — extremely unusual; likely "
            "an empty network namespace or a non-procfs mount.</i>"
        )
        return "\n".join(lines)

    triggered = _interfaces_with_errors(snap)
    if triggered:
        joined = ", ".join(f"<code>{i}</code>" for i in triggered)
        lines.append(
            f"  ⚠ <b>non-zero errs on:</b> {joined} — driver saw "
            f"frames on the wire it couldn't process. NIC/cable/"
            f"duplex-mismatch or kernel driver bug. Per-interface "
            f"detail below."
        )
        lines.append("")

    shown = snap.rows[:_RENDER_ROW_CAP]
    hidden = len(snap.rows) - len(shown)
    for row in shown:
        lines.append(
            f"  • <code>{row.iface}</code> "
            f"RX <code>{_fmt_bytes(row.rx_bytes)}</code>/"
            f"<code>{_fmt_int(row.rx_packets)}p</code> "
            f"(errs <code>{_fmt_int(row.rx_errs)}</code>, "
            f"drop <code>{_fmt_int(row.rx_drop)}</code>) | "
            f"TX <code>{_fmt_bytes(row.tx_bytes)}</code>/"
            f"<code>{_fmt_int(row.tx_packets)}p</code> "
            f"(errs <code>{_fmt_int(row.tx_errs)}</code>, "
            f"drop <code>{_fmt_int(row.tx_drop)}</code>)"
        )

    if hidden > 0:
        lines.append("")
        lines.append(
            f"  <i>{hidden} additional interface(s) hidden — "
            f"Telegram text cap. Total interfaces: {len(snap.rows)}.</i>"
        )

    lines.append("")
    lines.append(
        "<i>⚠ markers: non-zero rx_errs or tx_errs on a non-loopback "
        "interface — the unambiguous &quot;driver couldn't process "
        "frames on the wire&quot; signal. Drops are NOT marked: "
        "multicast filtering and packets-to-closed-sockets produce "
        "drops on every healthy host. Loopback excluded by design. "
        "See /admin_route for L3, /admin_tcpext for L4 counters.</i>"
    )
    return "\n".join(lines)


async def handle_admin_netdev(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_netdev; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    triggered = _interfaces_with_errors(snap)
    log.bind(
        user_id=user.id,
        available=snap.available,
        iface_count=len(snap.rows),
        ifaces_with_errors=list(triggered),
    ).info("/admin_netdev rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.netdev")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_netdev(message, settings)

    router.message.register(_entry, Command("admin_netdev", ignore_case=True))
    return router
