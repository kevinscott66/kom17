"""``/admin_arp`` — ARP neighbour table from /proc/net/arp.

The existing network surface looks at flow-level concerns:

* /admin_netconns — per-connection TCPv4 enumeration.
* /admin_sockstat — kernel-wide socket aggregates.
* /admin_tcpext — TCP-stack counters from /proc/net/netstat.
* /admin_netdev — per-interface byte/packet/error counters.
* /admin_route — IPv4 routing table.
* /admin_dns / /admin_resolver — name-resolution posture.

What none of them surface is the **link-layer neighbour cache**:
which IPv4 addresses on the local subnets the kernel currently has
hardware addresses for, and which are stuck in INCOMPLETE state.
That's what /proc/net/arp exposes, and it answers a class of
"why is connectivity flaky to one specific host on the same LAN"
questions that the flow-level cards can't even see — the failure
happens before a packet ever reaches the IP layer.

The operationally interesting predicate here is **incomplete ARP
entries**. The Flags column encodes the entry state: ``0x2``
(ATF_COM, complete and usable), ``0x6`` (complete + permanent),
``0x0`` (INCOMPLETE — kernel sent an ARP request and got no
response within the retry window). A handful of stale INCOMPLETE
entries are normal — they're how the kernel records "I tried to
reach X.X.X.X and couldn't" — but a growing pile usually means a
neighbour went down without notice, an ARP storm, a misconfigured
VLAN, or a noisy IP-scanner. We flag the count with ⚠ above a
modest threshold (_INCOMPLETE_WARN_THRESHOLD); below that, the
count renders without a marker.

Note: ARP is IPv4 only. The IPv6 equivalent (NDP — neighbour
discovery) lives in a different surface (/proc/net/icmp6 +
``ip -6 neigh``) and would be a separate card; this one is
explicitly the IPv4 lens.

Forward-compat: /proc/net/arp format has been stable since
forever. Header row, then space-separated columns
``IP HWtype Flags HWaddress Mask Device``. The mask column is
almost always ``*`` in modern kernels but is preserved verbatim;
the device column is the interface name. Lines whose column count
doesn't match are dropped (mid-update read or kernel format
change).

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


log = logger.bind(component="handlers.admin.arp")


_ARP_PATH = Path("/proc/net/arp")

# Flag bits — kernel encodes the entry state as a bitmask.
# ATF_COM = 0x02 (complete; the entry has a valid hardware
# address). ATF_PERM = 0x04 (permanent; configured statically and
# never expires). Absence of ATF_COM is INCOMPLETE — the kernel
# tried to resolve but didn't get an answer.
_ATF_COM = 0x02
_ATF_PERM = 0x04

# Threshold above which we flag the INCOMPLETE count with ⚠. A
# handful is normal background — scanning your own subnet, a host
# that briefly disappeared during a reboot. Beyond ~10 the pattern
# usually means something is actively wrong (downed neighbour, ARP
# storm, scanner). Picked low enough to catch real trouble and high
# enough to not fire on normal LAN churn.
_INCOMPLETE_WARN_THRESHOLD = 10

# Hard cap on rendered rows. /proc/net/arp on a large layer-2
# segment can hold hundreds of entries; Telegram message length
# matters. Full count remains on the snapshot for any drill-down
# caller; the cap only affects display.
_RENDER_ROW_CAP = 30


class _ArpEntry:
    """One row of /proc/net/arp.

    All fields preserved verbatim from the kernel — the operator
    reading this card wants the exact MAC and interface, not a
    summary. ``flags`` is the parsed integer mask so the
    ``is_complete`` / ``is_permanent`` predicates don't reparse
    the hex on every check.
    """

    __slots__ = ("device", "flags", "hw_addr", "hw_type", "ip", "mask")

    def __init__(
        self,
        *,
        ip: str,
        hw_type: str,
        flags: int,
        hw_addr: str,
        mask: str,
        device: str,
    ) -> None:
        self.ip = ip
        self.hw_type = hw_type
        self.flags = flags
        self.hw_addr = hw_addr
        self.mask = mask
        self.device = device

    @property
    def is_complete(self) -> bool:
        return bool(self.flags & _ATF_COM)

    @property
    def is_permanent(self) -> bool:
        return bool(self.flags & _ATF_PERM)


class _ArpSnapshot:
    """Captured /proc/net/arp.

    ``entries`` is every parsed row. ``available`` distinguishes
    "Linux with empty cache" (available + empty) from "macOS dev /
    non-procfs container" (not available).
    """

    __slots__ = ("available", "entries")

    def __init__(self, *, entries: tuple[_ArpEntry, ...], available: bool) -> None:
        self.entries = entries
        self.available = available

    @property
    def incomplete_count(self) -> int:
        """Pre-computed because both the render path and the log
        bind want the same number, and "not complete" trips on
        every non-ATF_COM entry including the rare ATF_PUB-only
        proxy entries (we treat those as incomplete-for-purposes-
        of-this-marker because they have no usable HW address)."""
        return sum(1 for e in self.entries if not e.is_complete)


def _parse_arp(text: str) -> tuple[_ArpEntry, ...]:
    """Parse /proc/net/arp. Returns the entry tuple.

    First line is a column header (``IP address HW type Flags …``);
    we identify it by its first token being ``IP`` rather than an
    actual address, and skip it. Each subsequent line is exactly
    6 whitespace-separated tokens. Lines with the wrong column
    count are dropped (mid-update read or format change). Flag
    parsing is hex (``0x2``); non-int hex poisons the row.
    """
    entries: list[_ArpEntry] = []
    for raw_line in text.splitlines():
        tokens = raw_line.split()
        if len(tokens) != 6:
            # Header line is 6 words too (``IP address HW type
            # Flags HW address Mask Device`` splits weirdly), but
            # it has multi-word column labels — we filter it via
            # the IP-token sniff below instead of by length.
            continue
        if tokens[0] == "IP":
            # Header row.
            continue
        # IP looks like a dotted quad — quick sniff to reject any
        # non-IPv4 first token without importing ipaddress.
        if tokens[0].count(".") != 3:
            continue
        try:
            flags = int(tokens[2], 16)
        except ValueError:
            # Non-hex flags column → not an arp data row.
            continue
        entries.append(
            _ArpEntry(
                ip=tokens[0],
                hw_type=tokens[1],
                flags=flags,
                hw_addr=tokens[3],
                mask=tokens[4],
                device=tokens[5],
            )
        )
    return tuple(entries)


def _capture(*, path: Path = _ARP_PATH) -> _ArpSnapshot:
    """Read /proc/net/arp + build a snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _ArpSnapshot(entries=(), available=False)
    return _ArpSnapshot(entries=_parse_arp(text), available=True)


def _render(snap: _ArpSnapshot) -> str:
    lines = ["📇 <b>ARP neighbour table (/proc/net/arp)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/net/arp unavailable on this host — Linux-only "
            "card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if not snap.entries:
        lines.append(
            "  <i>ARP cache is empty — no recent IPv4 traffic on the "
            "local segment, or the host has no IPv4 interfaces.</i>"
        )
        return "\n".join(lines)

    total = len(snap.entries)
    complete = sum(1 for e in snap.entries if e.is_complete)
    permanent = sum(1 for e in snap.entries if e.is_permanent)
    incomplete = snap.incomplete_count

    incomplete_marker = " ⚠" if incomplete > _INCOMPLETE_WARN_THRESHOLD else ""
    lines.append(
        f"  <b>Total entries:</b> <code>{total}</code> "
        f"(<code>{complete}</code> complete, "
        f"<code>{permanent}</code> permanent, "
        f"<code>{incomplete}</code> incomplete{incomplete_marker})"
    )
    lines.append("")

    truncated = len(snap.entries) > _RENDER_ROW_CAP
    rendered = snap.entries[:_RENDER_ROW_CAP]
    lines.append("  <b>Entries:</b>")
    for entry in rendered:
        state = "OK" if entry.is_complete else "INCOMPLETE"
        perm = " PERM" if entry.is_permanent else ""
        lines.append(
            f"  • <code>{entry.ip}</code> → "
            f"<code>{entry.hw_addr}</code> "
            f"on <code>{entry.device}</code> "
            f"[<i>{state}{perm}</i>]"
        )
    if truncated:
        lines.append(
            f"  <i>… {total - _RENDER_ROW_CAP} more entries not shown "
            f"(cap <code>{_RENDER_ROW_CAP}</code>).</i>"
        )

    lines.append("")
    if incomplete > _INCOMPLETE_WARN_THRESHOLD:
        lines.append(
            f"<i>⚠ <code>{incomplete}</code> INCOMPLETE entries — the "
            "kernel tried to resolve those IPs and got no ARP reply. A "
            "few are normal background; a sustained pile usually means "
            "a downed neighbour, ARP storm, misconfigured VLAN, or an "
            "IP scanner. See /admin_netdev for the interface that "
            "owns the segment, /admin_route for routing context.</i>"
        )
    else:
        lines.append(
            "<i>IPv4 only — IPv6 neighbour discovery (NDP) lives in a "
            "different kernel surface and isn't shown here. No marker "
            "for permanent entries (those are operator-configured by "
            "design).</i>"
        )
    return "\n".join(lines)


async def handle_admin_arp(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_arp; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        total=len(snap.entries),
        incomplete=snap.incomplete_count,
        permanent=sum(1 for e in snap.entries if e.is_permanent),
    ).info("/admin_arp rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.arp")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_arp(message, settings)

    router.message.register(_entry, Command("admin_arp", ignore_case=True))
    return router
