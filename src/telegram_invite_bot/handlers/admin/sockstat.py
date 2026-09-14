"""``/admin_sockstat`` — aggregate socket counts from /proc/net/sockstat.

Complements /admin_netconns (per-connection enumeration of TCP4)
by surfacing aggregates the enumeration doesn't expose cleanly:

* ``sockets: used`` — kernel-wide total open sockets (all families).
* ``TCP: orphan`` — TCP sockets with no userspace owner. Sustained
  growth here is the canonical "your app is leaking sockets
  without close()" signal — and unlike inuse/tw, orphan sockets
  don't show up in /proc/net/tcp{,6} because they're past the
  fd-mapped phase. /admin_netconns literally cannot see them.
* ``TCP: tw`` — TIME_WAIT count; /admin_netconns counts by state
  but only for v4, sockstat shows the unified total.
* ``TCP: alloc`` — total TCP socket allocations; alloc - inuse - tw
  is roughly the orphan/dying-socket pool size.
* ``TCP: mem`` — TCP buffer memory in 4KiB pages.
* ``UDP``, ``UDPLITE``, ``RAW``, ``FRAG`` — short sections; UDP
  matters for DNS, others are decoration.

The IPv6 equivalent lives at /proc/net/sockstat6 with different
keys (no orphan/alloc/mem in the IPv4 sense). We read it too and
render as a separate "TCP6:" block so the v4/v6 picture is
unified without conflating fields that don't mean the same thing.

Zero-⚠ card by design. There's no universally-safe threshold for
"too many orphans" — the kernel default ``net.ipv4.tcp_max_orphans``
is 65536-ish on typical hosts, so 100 orphans is fine and 50000
is concerning, but the value is operator policy not card policy.
Same posture as /admin_swaps and /admin_limits: surface the data,
let the operator decide. The disclaimer footer makes that explicit
so a future refactor doesn't accidentally add a marker.

Forward-compat: /proc/net/sockstat is a small fixed format
(``family: k1 v1 k2 v2 …``) but kernel can add keys per section
without warning. We parse all key→value pairs into a per-family
dict and render a curated subset; unknown keys are stored and
counted in the footer so an operator can spot when the format
expanded.

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


log = logger.bind(component="handlers.admin.sockstat")


_SOCKSTAT_PATH = Path("/proc/net/sockstat")
_SOCKSTAT6_PATH = Path("/proc/net/sockstat6")


# Curated key order per family. The IPv4 file has TCP keys
# ``inuse orphan tw alloc mem`` in that order; we render in
# operator-priority order (orphan first because it's the leak
# signal). Anything in the parsed dict but not listed here is
# silently kept for the footer "extra keys" count, not surfaced.
_TCP4_CURATED: tuple[str, ...] = ("inuse", "orphan", "tw", "alloc", "mem")
_TCP6_CURATED: tuple[str, ...] = ("inuse",)  # v6 only carries inuse
_UDP_CURATED: tuple[str, ...] = ("inuse", "mem")


class _SockstatSnapshot:
    """Captured /proc/net/sockstat (+ sockstat6 when present).

    Both files parse into ``per_family: dict[str, dict[str, int]]``.
    Family names match the line prefixes verbatim (``TCP``, ``UDP``,
    ``UDPLITE``, ``RAW``, ``FRAG``, ``TCP6``, ``UDP6``, …) and
    ``sockets`` (the kernel-wide-total line) is a family of its own
    with the single key ``used``.

    ``available_v4`` and ``available_v6`` track each file
    independently — many container hosts disable IPv6 in the
    network namespace, so v6 absence is normal not a failure.
    """

    __slots__ = ("available_v4", "available_v6", "v4", "v6")

    def __init__(
        self,
        *,
        v4: dict[str, dict[str, int]],
        v6: dict[str, dict[str, int]],
        available_v4: bool,
        available_v6: bool,
    ) -> None:
        self.v4 = v4
        self.v6 = v6
        self.available_v4 = available_v4
        self.available_v6 = available_v6

    @property
    def available(self) -> bool:
        """At least one of the two files was readable."""
        return self.available_v4 or self.available_v6


def _parse_sockstat(text: str) -> dict[str, dict[str, int]]:
    """Parse /proc/net/sockstat or sockstat6.

    Each line: ``<family>: k1 v1 k2 v2 …``. We split on whitespace,
    take parts[0] (with trailing colon stripped) as family, then
    walk the rest as alternating key/value pairs. Non-integer values
    skip the pair, not the whole line — same degrade-don't-crash
    posture as every other /proc parser in this surface.
    """
    out: dict[str, dict[str, int]] = {}
    for raw_line in text.splitlines():
        parts = raw_line.split()
        if len(parts) < 2:
            continue
        family = parts[0].rstrip(":")
        if not family:
            continue
        kv = parts[1:]
        family_dict: dict[str, int] = {}
        # Walk pairs. Odd-length tail (kernel emits ``family: used 5``
        # → 2 pieces, pair-perfect; but if a future kernel emits a
        # bare flag without value, zip-shortest skips it).
        for key, value in zip(kv[0::2], kv[1::2], strict=False):
            try:
                family_dict[key] = int(value)
            except ValueError:
                continue
        if family_dict:
            out[family] = family_dict
    return out


def _capture(
    *,
    v4_path: Path = _SOCKSTAT_PATH,
    v6_path: Path = _SOCKSTAT6_PATH,
) -> _SockstatSnapshot:
    """Read both /proc/net/sockstat files. Each is tracked
    independently so v6-disabled hosts render cleanly."""
    v4: dict[str, dict[str, int]] = {}
    available_v4 = False
    try:
        v4 = _parse_sockstat(v4_path.read_text(encoding="utf-8", errors="replace"))
        available_v4 = True
    except OSError:
        pass
    v6: dict[str, dict[str, int]] = {}
    available_v6 = False
    try:
        v6 = _parse_sockstat(v6_path.read_text(encoding="utf-8", errors="replace"))
        available_v6 = True
    except OSError:
        pass
    return _SockstatSnapshot(v4=v4, v6=v6, available_v4=available_v4, available_v6=available_v6)


def _render_family(
    label: str, family: dict[str, int] | None, curated: tuple[str, ...]
) -> list[str]:
    """Render one family's curated keys. Missing family → 'absent'
    line; missing key → 'n/a' value. Both observable so an operator
    on an older kernel can see what's not there."""
    if family is None:
        return [f"  <b>{label}:</b> <i>section absent on this kernel</i>"]
    parts: list[str] = []
    for key in curated:
        value = family.get(key)
        rendered = "n/a" if value is None else f"{value:,}"
        parts.append(f"{key}=<code>{rendered}</code>")
    return [f"  <b>{label}:</b> " + " ".join(parts)]


def _render(snap: _SockstatSnapshot) -> str:
    lines = ["🧮 <b>Socket-state aggregates (/proc/net/sockstat)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/net/sockstat unavailable on this host — "
            "Linux-only card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    # Kernel-wide total. ``sockets`` family carries ``used`` only.
    if snap.available_v4:
        sockets = snap.v4.get("sockets")
        if sockets is not None:
            lines.append(f"  <b>Total sockets used:</b> <code>{sockets.get('used', 0):,}</code>")
        lines.append("")
        lines.extend(_render_family("TCP (v4)", snap.v4.get("TCP"), _TCP4_CURATED))
        lines.extend(_render_family("UDP (v4)", snap.v4.get("UDP"), _UDP_CURATED))
        # Short, fixed sections — render as one line each with raw dicts.
        for short_family in ("UDPLITE", "RAW", "FRAG"):
            section = snap.v4.get(short_family)
            if section is not None:
                rendered_pairs = " ".join(f"{k}=<code>{v:,}</code>" for k, v in section.items())
                lines.append(f"  <b>{short_family} (v4):</b> {rendered_pairs}")
    else:
        lines.append("  <i>/proc/net/sockstat (v4) unavailable.</i>")

    lines.append("")
    if snap.available_v6:
        lines.extend(_render_family("TCP6", snap.v6.get("TCP6"), _TCP6_CURATED))
        lines.extend(_render_family("UDP6", snap.v6.get("UDP6"), _UDP_CURATED))
        for short_family in ("UDPLITEv6", "RAWv6", "FRAG6"):
            section = snap.v6.get(short_family)
            if section is not None:
                rendered_pairs = " ".join(f"{k}=<code>{v:,}</code>" for k, v in section.items())
                lines.append(f"  <b>{short_family}:</b> {rendered_pairs}")
    else:
        lines.append(
            "  <i>/proc/net/sockstat6 unavailable — IPv6 likely "
            "disabled in this network namespace (common in "
            "containers).</i>"
        )

    lines.append("")
    lines.append(
        "<i>No warning markers on this card by design — there's no "
        "universally-safe threshold for &quot;too many orphans&quot; "
        "or &quot;too many TIME_WAITs&quot;. The kernel default "
        "<code>net.ipv4.tcp_max_orphans</code> is the relevant "
        "ceiling; compare against /admin_sysctl. TCP.orphan growth "
        "over time is the canonical app-leaking-sockets signal — "
        "but trend detection is outside a point-in-time card's "
        "remit. See /admin_netconns for per-connection enumeration.</i>"
    )
    return "\n".join(lines)


async def handle_admin_sockstat(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_sockstat; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available_v4=snap.available_v4,
        available_v6=snap.available_v6,
        tcp_orphan=snap.v4.get("TCP", {}).get("orphan"),
        tcp_inuse=snap.v4.get("TCP", {}).get("inuse"),
        tcp_tw=snap.v4.get("TCP", {}).get("tw"),
        sockets_used=snap.v4.get("sockets", {}).get("used"),
    ).info("/admin_sockstat rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.sockstat")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_sockstat(message, settings)

    router.message.register(_entry, Command("admin_sockstat", ignore_case=True))
    return router
