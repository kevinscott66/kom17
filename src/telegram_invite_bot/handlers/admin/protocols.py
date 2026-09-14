"""``/admin_protocols`` — /proc/net/protocols registered protocol families.

Adjacent network cards each surface one slice:

* /admin_netconns — *per-socket* live connection table (ss -tan).
* /admin_sockstat — *aggregate* socket counts (TCP inuse=N, …).
* /admin_netdev — interface-level RX/TX byte counters.
* /admin_tcpext — TCP MIB counters (retransmits, OOO, …).

What no card surfaces is the **per-protocol-family kernel
metadata**: which families (TCP, UDP, RAW, NETLINK, UNIX, …)
are registered, how many sockets each holds, and — critically
— whether the family is **under memory pressure**.

/proc/net/protocols format (one header + one line per family)::

    protocol  size sockets  memory press maxhdr  slab module ...
    TCP       2208     32  131072   no     320   yes  kernel ...
    UDP       1376      6  524288   no       0   yes  kernel ...
    TCPv6     2280     12      -1   NI       0   yes  kernel ...

Column meanings we care about (rest of the y/n capability matrix
is parsed-but-dropped — those flags are kernel-version-stable
posture, not operational signal):

* **protocol** — family name (TCP / UDP / TCPv6 / …).
* **size** — sock struct size in bytes (informational).
* **sockets** — count currently allocated. Cross-checkable
  against /admin_sockstat's inuse= for the same family; if they
  disagree it's a kernel accounting bug worth flagging.
* **memory** — pages of socket memory accounted to this proto.
  ``-1`` means "no memory accounting" (PACKET/UNIX/NETLINK
  always emit -1 — they don't go through ``proto_memory_*``).
* **press** — pressure state: ``no`` / ``yes`` / ``NI`` (no
  indication, paired with memory=-1). ``yes`` means the kernel
  has crossed ``net.ipv4.tcp_mem[1]`` (or equivalent) for this
  family and is in the squeeze regime — new allocations may
  fail. **This is the single ⚠ predicate.**

⚠ predicate: any family with ``press=yes``. Surfaces the
moment a protocol family enters memory pressure — actionable
because the operator can correlate with /admin_meminfo
(MemAvailable, Slab) and /admin_psi (memory pressure) to
distinguish "system-wide OOM imminent" from "tcp_mem squeeze
specifically". Pinned with must-not-fire test on the canonical-
healthy sample where every family is press=no or press=NI.

Per-family y/n capability flags (close/connect/disconnect/…)
deliberately dropped — they're a posture matrix that doesn't
change between kernel boots, not an operational signal. Re-
rendering them would 5× the card length for no operator benefit.

Same wiring as every other admin card — silent-drop, private-only,
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


log = logger.bind(component="handlers.admin.protocols")


_PROTOCOLS_PATH = Path("/proc/net/protocols")


# Maximum families rendered. /proc/net/protocols on a modern
# Linux box typically lists 15–25 families (TCP/UDP/RAW × {v4,v6}
# + UNIX + NETLINK + PACKET + KCM + SMC + RDS + AF_VSOCK + …).
# Cap exists purely as a safety net against an exotic kernel
# registering hundreds.
_MAX_FAMILIES = 40


class _ProtocolRow:
    """One parsed /proc/net/protocols line.

    ``memory == -1`` is the legitimate "no memory accounting"
    marker (PACKET / UNIX / NETLINK), NOT a parse-failure
    sentinel. ``press`` is stored as the raw kernel token so
    render can distinguish ``no`` from ``NI`` (no-indication,
    paired with memory=-1) — collapsing both to ``False`` would
    lose the fact that the family is opted out of accounting
    entirely.
    """

    __slots__ = ("memory", "name", "press", "size", "sockets")

    def __init__(
        self,
        *,
        name: str,
        size: int,
        sockets: int,
        memory: int,
        press: str,
    ) -> None:
        self.name = name
        self.size = size
        self.sockets = sockets
        self.memory = memory
        self.press = press

    @property
    def under_pressure(self) -> bool:
        return self.press == "yes"


class _ProtocolsSnapshot:
    """Captured /proc/net/protocols rows."""

    __slots__ = ("available", "rows")

    def __init__(self, *, rows: list[_ProtocolRow], available: bool) -> None:
        self.rows = rows
        self.available = available

    @property
    def pressured(self) -> list[_ProtocolRow]:
        return [r for r in self.rows if r.under_pressure]


def _parse_protocols(text: str) -> list[_ProtocolRow]:
    """Parse /proc/net/protocols. First non-blank line is the
    header (``protocol size sockets memory press …``) — skipped.
    Lines with fewer than 5 fields drop defensively. Non-integer
    ``size``/``sockets``/``memory`` drop.
    """
    rows: list[_ProtocolRow] = []
    saw_header = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if not saw_header:
            # The header line begins with the literal string
            # "protocol" — anything else and the file is in a
            # shape we don't recognise; bail rather than guess.
            if parts and parts[0] == "protocol":
                saw_header = True
            continue
        if len(parts) < 5:
            continue
        try:
            size = int(parts[1])
            sockets = int(parts[2])
            memory = int(parts[3])
        except ValueError:
            continue
        press = parts[4]
        rows.append(
            _ProtocolRow(name=parts[0], size=size, sockets=sockets, memory=memory, press=press)
        )
    return rows


def _capture(*, path: Path = _PROTOCOLS_PATH) -> _ProtocolsSnapshot:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _ProtocolsSnapshot(rows=[], available=False)
    rows = _parse_protocols(text)
    return _ProtocolsSnapshot(rows=rows, available=True)


def _fmt_memory(pages: int) -> str:
    """Render the memory column. -1 → 'n/a (no accounting)'; else
    pages with thousands separator. We don't multiply by page
    size because the kernel column is intentionally in pages
    (operator can compare against ``net.ipv4.tcp_mem`` which is
    also in pages)."""
    if pages < 0:
        return "n/a"
    return f"{pages:,}"


def _render(snap: _ProtocolsSnapshot) -> str:
    lines = ["🌐 <b>Registered protocol families (/proc/net/protocols)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/net/protocols unavailable — Linux-only "
            "surface (macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        lines.append("  <i>No families parsed — kernel emitted an unrecognised format.</i>")
        return "\n".join(lines)

    pressured = snap.pressured
    warn = bool(pressured)

    lines.append(f"  <b>Families:</b> <code>{len(snap.rows)}</code>")
    lines.append("")

    shown = snap.rows[:_MAX_FAMILIES]
    for r in shown:
        marker = " ⚠" if r.under_pressure else ""
        lines.append(
            f"  <code>{r.name:<10}</code> "
            f"sockets=<code>{r.sockets:,}</code> "
            f"mem=<code>{_fmt_memory(r.memory)}</code> "
            f"press=<code>{r.press}</code>{marker}"
        )
    if len(snap.rows) > _MAX_FAMILIES:
        lines.append(f"  <i>… {len(snap.rows) - _MAX_FAMILIES} more (truncated).</i>")

    lines.append("")
    if warn:
        names = ", ".join(f"<code>{r.name}</code>" for r in pressured)
        lines.append(
            f"<i>⚠ {names} under socket-memory pressure (press=yes). "
            "Kernel has crossed the proto-mem squeeze threshold "
            "(net.ipv4.tcp_mem[1] for TCP/UDP, equivalents for "
            "other families) — new allocations from this family "
            "may fail. Cross-check with /admin_meminfo "
            "(MemAvailable, Slab), /admin_psi (memory pressure), "
            "and /admin_sockstat (per-family inuse counts). On a "
            "healthy host every family is press=no or press=NI.</i>"
        )
    else:
        lines.append(
            "<i>No warnings — every family is press=no (memory "
            "accounted, under threshold) or press=NI (no "
            "accounting, e.g. UNIX/PACKET/NETLINK). The y/n "
            "capability matrix per family is intentionally "
            "omitted — it's a posture constant, not an "
            "operational signal.</i>"
        )
    return "\n".join(lines)


async def handle_admin_protocols(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_protocols; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        families=len(snap.rows),
        pressured=len(snap.pressured),
    ).info("/admin_protocols rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.protocols")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_protocols(message, settings)

    router.message.register(_entry, Command("admin_protocols", ignore_case=True))
    return router
