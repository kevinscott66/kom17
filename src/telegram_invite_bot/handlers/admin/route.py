"""``/admin_route`` — IPv4 routing table from /proc/net/route.

The existing network cards each answer a different layer:

* ``/admin_dns`` / ``/admin_resolver`` — name resolution (L7).
* ``/admin_certfp`` — TLS termination posture (L6).
* ``/admin_telegram_api`` / ``/admin_netconns`` — application
  socket state (L4-L7).

None answer the L3 question: **"does the kernel know how to reach
the outside world?"**. Wrong default gateway, missing route to
api.telegram.org's network, or a misconfigured podman/k8s
container — these show up as opaque connect timeouts in the L7
cards while the actual failure lives in /proc/net/route.

What we render: every IPv4 route the kernel has, with hex
little-endian destination / gateway / mask decoded to dotted-quad
form. Default routes (destination 0.0.0.0/0) are highlighted as
a separate "default routes" block so the operator's eye lands on
them — they're the single most-asked field in any networking
incident.

We deliberately stick to IPv4 (/proc/net/route). The IPv6 table
lives at /proc/net/ipv6_route with a completely different schema;
that's a separate diagnostic card if it's ever needed. Splitting
means each card stays scannable.

Cry-wolf posture: single ⚠ on **no default route**. A host
without a default gateway can't reach anything not on its own
LAN — that's the unambiguous "this network is broken" signal
the card exists to detect. Other oddities (multiple defaults,
unusual metrics, route on lo) are operator policy and not ⚠'d.

Forward-compat: /proc/net/route has carried 11 fields since the
2.6 era. Short lines degrade rather than crash. Hex decoding
tolerates unparseable values by rendering "?" in the dotted-quad
position — operator sees the raw hex on either side and can
sanity-check manually.

Same posture as every other admin card — silent-drop, private-only,
pure stdlib.
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


log = logger.bind(component="handlers.admin.route")


_ROUTE_PATH = Path("/proc/net/route")


# Render row cap — IPv4 route tables on container hosts can be
# 50+ rows (CNI plugins, podman networks, libvirt bridges, …).
# Same logic as /admin_mounts: cap with explicit hidden-count
# rather than silent truncation.
_RENDER_ROW_CAP = 30


# /proc/net/route flag bits — kernel route.h. Only the ones an
# operator routinely encounters are mapped; others render as the
# raw hex value so unknown flags don't silently disappear.
_FLAG_BITS: tuple[tuple[int, str], ...] = (
    (0x0001, "U"),  # UP: route is active
    (0x0002, "G"),  # GATEWAY: indirect (uses gateway field)
    (0x0004, "H"),  # HOST: host-specific (single-address route)
    (0x0008, "R"),  # REINSTATE: reinstate after failure
    (0x0010, "D"),  # DYNAMIC: dynamically installed (e.g. by ICMP redirect)
    (0x0020, "M"),  # MODIFIED: by ICMP redirect
)


class _RouteRow:
    """One /proc/net/route line.

    Hex fields are little-endian byte-order on /proc — we decode
    once at parse time and keep the dotted-quad string on the row.
    The raw hex is kept too so the operator can sanity-check
    against `ip route` output (which uses the dotted-quad).
    """

    __slots__ = (
        "destination",
        "destination_hex",
        "flags",
        "flags_raw",
        "gateway",
        "gateway_hex",
        "iface",
        "mask",
        "mask_hex",
        "metric",
    )

    def __init__(
        self,
        *,
        iface: str,
        destination: str,
        destination_hex: str,
        gateway: str,
        gateway_hex: str,
        mask: str,
        mask_hex: str,
        flags: str,
        flags_raw: int,
        metric: int,
    ) -> None:
        self.iface = iface
        self.destination = destination
        self.destination_hex = destination_hex
        self.gateway = gateway
        self.gateway_hex = gateway_hex
        self.mask = mask
        self.mask_hex = mask_hex
        self.flags = flags
        self.flags_raw = flags_raw
        self.metric = metric


class _RouteSnapshot:
    """Captured IPv4 routing table.

    * ``rows`` — every parsed route.
    * ``available`` — False when /proc/net/route is unreadable
      (macOS dev, container without /proc).
    """

    __slots__ = ("available", "rows")

    def __init__(
        self,
        *,
        rows: tuple[_RouteRow, ...],
        available: bool,
    ) -> None:
        self.rows = rows
        self.available = available


def _decode_hex_ipv4(hex_value: str) -> str:
    """Decode a /proc-style little-endian hex IPv4 to dotted-quad.

    /proc emits e.g. ``0102A8C0`` which is bytes 01 02 A8 C0 in
    memory order; on little-endian systems (every Linux target)
    that's address C0.A8.02.01 = 192.168.2.1. Return ``?`` on
    parse failure rather than raising — degrade-don't-crash so a
    single bogus row doesn't sink the whole card.
    """
    if len(hex_value) != 8:
        return "?"
    try:
        value = int(hex_value, 16)
    except ValueError:
        return "?"
    # Bytes in little-endian /proc representation: extract LSB
    # first, which corresponds to the FIRST octet of the address.
    b0 = value & 0xFF
    b1 = (value >> 8) & 0xFF
    b2 = (value >> 16) & 0xFF
    b3 = (value >> 24) & 0xFF
    return f"{b0}.{b1}.{b2}.{b3}"


def _decode_flags(flags_raw: int) -> str:
    """Decode the integer flags field into the BSD-ish letter
    string ``netstat -r`` and ``ip route`` use. Unknown bits are
    preserved as ``+0xNN`` so forward-compat doesn't silently drop
    new flags."""
    letters = [letter for bit, letter in _FLAG_BITS if flags_raw & bit]
    known = sum(bit for bit, _ in _FLAG_BITS if flags_raw & bit)
    leftover = flags_raw & ~known
    if leftover:
        letters.append(f"+0x{leftover:02X}")
    return "".join(letters) if letters else "-"


def _parse_route(text: str) -> tuple[_RouteRow, ...]:
    """Parse /proc/net/route.

    First line is the header (``Iface\\tDestination\\tGateway\\t…``);
    we skip it. Subsequent lines are tab-separated but we split on
    whitespace because trailing whitespace + a stray space have
    bit some kernels in the past — be liberal.
    """
    rows: list[_RouteRow] = []
    for idx, raw_line in enumerate(text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        if idx == 0 and line.startswith("Iface"):
            continue
        parts = line.split()
        if len(parts) < 11:
            continue
        iface = parts[0]
        destination_hex = parts[1]
        gateway_hex = parts[2]
        try:
            flags_raw = int(parts[3], 16)
            metric = int(parts[6])
        except ValueError:
            continue
        mask_hex = parts[7]
        rows.append(
            _RouteRow(
                iface=iface,
                destination=_decode_hex_ipv4(destination_hex),
                destination_hex=destination_hex,
                gateway=_decode_hex_ipv4(gateway_hex),
                gateway_hex=gateway_hex,
                mask=_decode_hex_ipv4(mask_hex),
                mask_hex=mask_hex,
                flags=_decode_flags(flags_raw),
                flags_raw=flags_raw,
                metric=metric,
            )
        )
    return tuple(rows)


def _capture(*, path: Path = _ROUTE_PATH) -> _RouteSnapshot:
    """Read /proc/net/route + build a snapshot.

    Keyword-only path parameter so tests can inject a tmp file —
    same hermetic-fixture pattern as every other diagnostic card.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _RouteSnapshot(rows=(), available=False)
    return _RouteSnapshot(rows=_parse_route(text), available=True)


def _default_routes(snap: _RouteSnapshot) -> tuple[_RouteRow, ...]:
    """Rows with destination 0.0.0.0/0 — the kernel uses these
    for any address not matched by a more-specific route. Bot
    failure mode this card exists to detect: zero default routes."""
    return tuple(r for r in snap.rows if r.destination == "0.0.0.0")  # noqa: S104


def _no_default_route(snap: _RouteSnapshot) -> bool:
    """⚠ predicate — no default route present in an available
    snapshot. Returns False if the snapshot itself is unavailable
    (macOS dev) — absence of data is NOT a warning."""
    if not snap.available:
        return False
    return len(_default_routes(snap)) == 0


def _render(snap: _RouteSnapshot) -> str:
    lines = ["🛣 <b>IPv4 routing table (/proc/net/route)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/net/route unavailable on this host — Linux-"
            "only card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        lines.append(
            "  <i>routing table is empty — extremely unusual; "
            "likely a sandboxed network namespace with no routes "
            "installed.</i>"
        )
        return "\n".join(lines)

    defaults = _default_routes(snap)
    if not defaults:
        # Hoist above the table — operator's eye lands at the top.
        # Same ergonomic as /admin_mounts rootfs-ro.
        lines.append(
            "  ⚠ <b>no default route</b> — host cannot reach any "
            "destination outside its own LAN. Outbound API calls "
            "(Telegram, DNS to non-link-local resolvers) will "
            "time out. Investigate immediately."
        )
        lines.append("")
    else:
        lines.append("  <b>Default routes:</b>")
        for d in defaults:
            lines.append(
                f"    • via <code>{d.gateway}</code> on "
                f"<code>{d.iface}</code> (metric "
                f"<code>{d.metric}</code>)"
            )
        lines.append("")

    lines.append("  <b>All routes:</b>")
    shown = snap.rows[:_RENDER_ROW_CAP]
    hidden = len(snap.rows) - len(shown)
    for row in shown:
        # Render dest/mask compact; gateway 0.0.0.0 means "direct"
        # which we mark inline so the operator doesn't have to
        # decode the convention.
        gateway_str = "direct" if row.gateway == "0.0.0.0" else row.gateway  # noqa: S104
        lines.append(
            f"  • <code>{row.destination}/{row.mask}</code> "
            f"→ <code>{gateway_str}</code> "
            f"on <code>{row.iface}</code> "
            f"(<i>{row.flags}, metric {row.metric}</i>)"
        )

    if hidden > 0:
        lines.append("")
        lines.append(
            f"  <i>{hidden} additional route(s) hidden — Telegram "
            f"text cap. Total routes: {len(snap.rows)}.</i>"
        )

    lines.append("")
    lines.append(
        "<i>⚠ markers: only when no default route is installed — "
        "the unambiguous &quot;host has no path to the outside "
        "world&quot; signal. Per-route oddities (multiple "
        "defaults, unusual metrics, lo routes) are operator "
        "policy and NOT marked. See /admin_resolver for DNS, "
        "/admin_netconns for socket state, /admin_dns for live "
        "api.telegram.org probe.</i>"
    )
    return "\n".join(lines)


async def handle_admin_route(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_route; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        route_count=len(snap.rows),
        default_route_count=len(_default_routes(snap)),
        no_default_route=_no_default_route(snap),
    ).info("/admin_route rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.route")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_route(message, settings)

    router.message.register(_entry, Command("admin_route", ignore_case=True))
    return router
