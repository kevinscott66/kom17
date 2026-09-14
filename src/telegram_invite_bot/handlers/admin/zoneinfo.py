"""``/admin_zoneinfo`` — per-zone watermarks from /proc/zoneinfo.

Companion lens to /admin_buddyinfo, one rung up the memory-pressure
ladder. Where buddyinfo shows free-list arity per order (can the
kernel satisfy a contiguous allocation right now?), zoneinfo shows
free-page counts per zone alongside the kernel's three reclaim
thresholds: ``min``, ``low``, ``high``. Those thresholds drive
when the kernel starts working harder for memory:

* ``free > high`` — quiet. No reclaim activity.
* ``low < free <= high`` — kswapd is the goal-zone; if free dips
  toward low, kswapd wakes and reclaims in the background. Quiet
  to userspace.
* ``min < free <= low`` — kswapd is **actively** reclaiming. Still
  background, but the host is no longer at rest; if allocation
  pressure outpaces reclaim, you slide further.
* ``free <= min`` — **direct reclaim**. Allocating tasks block
  in-kernel doing the reclaim themselves. This is the latency
  spike an operator notices first: app-level p99 jumps because
  every allocation pays for cleanup that should have happened
  asynchronously. Sustained free<=min usually means swap or OOM
  is imminent.

Nothing else on this surface answers the question "is the host
currently in reclaim?" /admin_meminfo shows aggregate free; that
doesn't tell you whether the kernel considers it enough. /admin_psi
shows memory-pressure stalls; those are the *result* of free<low,
this card is the *cause*. /admin_vmstat's pgsteal_* counters are
the historical footprint; this card is point-in-time.

Two operationally meaningful predicates, each rendered as ⚠ on the
zone row:

* **free <= low** — kswapd actively reclaiming. Warning.
* **free <= min** — direct reclaim. Worse warning (rendered with
  the same ⚠ marker but the footer disambiguates).

We render only the most fragmentation-relevant zones — Normal,
DMA32, Movable — because DMA (tiny legacy zone, always near
empty) and Device (zones for hardware DAX) generate false-positive
markers and noise more than signal. The full parsed dict remains
on the snapshot so a drill-down caller can inspect every zone.

Forward-compat parsing: /proc/zoneinfo is structured by ``Node N,
zone NAME`` block headers followed by indented ``key value`` lines.
We track the current (node, zone) tuple and bucket every
``key value`` pair under it; unknown keys are kept but not
surfaced. New kernel versions add fields (boost_watermark on 5.x;
present_pages on older ones) without renaming the watermark trio,
so the operational predicate is stable across versions.

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


log = logger.bind(component="handlers.admin.zoneinfo")


_ZONEINFO_PATH = Path("/proc/zoneinfo")

# Zones we surface in the rendered card. DMA is intentionally
# excluded — it's a tiny legacy zone that's often near its
# watermarks even on idle hosts, producing meaningless ⚠ noise.
# Device zones (hardware DAX) are excluded for the same reason.
# Full parsed data for every zone remains on the snapshot so a
# drill-down caller can see what we filtered.
_RENDERED_ZONES: frozenset[str] = frozenset({"Normal", "DMA32", "Movable"})


class _ZoneStats:
    """One Node × zone's parsed kv pairs.

    ``fields`` is the full parsed dict; ``free``, ``min``, ``low``,
    ``high`` are convenience accessors with None when the kernel
    didn't emit them (some embedded builds drop fields). The
    point-in-time pressure predicate operates on those four; other
    fields (present_pages, managed, …) are kept for drill-down.
    """

    __slots__ = ("fields", "node", "zone")

    def __init__(
        self,
        *,
        node: int,
        zone: str,
        fields: dict[str, int],
    ) -> None:
        self.node = node
        self.zone = zone
        self.fields = fields

    @property
    def free(self) -> int | None:
        return self.fields.get("free")

    @property
    def min(self) -> int | None:
        return self.fields.get("min")

    @property
    def low(self) -> int | None:
        return self.fields.get("low")

    @property
    def high(self) -> int | None:
        return self.fields.get("high")

    @property
    def under_low(self) -> bool:
        """kswapd active. Requires both fields known; missing data
        defaults to False so we don't fire on incomplete parses."""
        if self.free is None or self.low is None:
            return False
        return self.free <= self.low

    @property
    def under_min(self) -> bool:
        """Direct reclaim. Strict subset of under_low — under_min
        implies under_low."""
        if self.free is None or self.min is None:
            return False
        return self.free <= self.min


class _ZoneinfoSnapshot:
    """Captured /proc/zoneinfo.

    ``zones`` is every parsed Node × zone block in file order.
    ``available`` distinguishes macOS-dev / non-procfs (False) from
    Linux-with-empty-file (True + empty tuple) — latter shouldn't
    happen in practice but the API distinguishes them anyway.
    """

    __slots__ = ("available", "zones")

    def __init__(self, *, zones: tuple[_ZoneStats, ...], available: bool) -> None:
        self.zones = zones
        self.available = available


def _parse_zoneinfo(text: str) -> tuple[_ZoneStats, ...]:
    """Parse /proc/zoneinfo into per-zone blocks.

    State machine over lines. ``Node N, zone NAME`` opens a new
    block; indented ``key value`` lines feed the current block's
    fields dict; anything else (``pagesets``, ``protection:`` and
    its multi-value tuple) is skipped because we don't currently
    surface it. The ``pages`` keyword block is special: lines
    start with ``pages free 1234`` (3 tokens, "pages" is the
    group label, "free" the field) — we collapse those by
    detecting the ``pages`` prefix and dropping it before
    delegating to the kv parser.
    """
    zones: list[_ZoneStats] = []
    current: _ZoneStats | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        stripped = line.lstrip()
        if stripped.startswith("Node"):
            # ``Node 0, zone Normal``: tokens[1] is "0,", tokens[3]
            # is "Normal". Same shape as buddyinfo, intentionally.
            tokens = stripped.split()
            if len(tokens) < 4 or tokens[2] != "zone":
                continue
            node_tok = tokens[1].rstrip(",")
            try:
                node = int(node_tok)
            except ValueError:
                continue
            current = _ZoneStats(node=node, zone=tokens[3], fields={})
            zones.append(current)
            continue
        if current is None:
            # Pre-amble before the first Node block (some kernels
            # emit a sysctl header). Skip cleanly.
            continue
        tokens = stripped.split()
        # The watermark block is rendered as ``pages free 1234``
        # etc.; the ``pages`` prefix is a group label not a field
        # name. Drop it before kv parsing.
        if tokens and tokens[0] == "pages":
            tokens = tokens[1:]
        if len(tokens) < 2:
            continue
        # protection: appears as ``protection: (0, 0, …)`` —
        # multi-value tuple, not a single int. Skip; if we ever
        # need it we'd parse differently.
        if tokens[0].endswith(":"):
            continue
        key = tokens[0]
        try:
            value = int(tokens[1])
        except ValueError:
            # Non-int value (some kernels emit boolean-ish strings
            # in newer fields). Skip the pair, not the whole block.
            continue
        current.fields[key] = value
    return tuple(zones)


def _capture(*, path: Path = _ZONEINFO_PATH) -> _ZoneinfoSnapshot:
    """Read /proc/zoneinfo + build a snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _ZoneinfoSnapshot(zones=(), available=False)
    return _ZoneinfoSnapshot(zones=_parse_zoneinfo(text), available=True)


def _has_pressure(snap: _ZoneinfoSnapshot) -> bool:
    """⚠ predicate: any rendered zone is under_low. Kept as a
    function so it's testable in isolation and so the render path
    asks the same question the test asks."""
    return any(z.under_low for z in snap.zones if z.zone in _RENDERED_ZONES)


def _has_direct_reclaim(snap: _ZoneinfoSnapshot) -> bool:
    """Stricter predicate for the footer wording: any rendered
    zone under_min (direct reclaim ongoing)."""
    return any(z.under_min for z in snap.zones if z.zone in _RENDERED_ZONES)


def _render(snap: _ZoneinfoSnapshot) -> str:
    lines = ["💧 <b>Zone watermarks (/proc/zoneinfo)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/zoneinfo unavailable on this host — Linux-only "
            "card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if not snap.zones:
        lines.append("  <i>parse failed or empty file — extremely unusual; check kernel build.</i>")
        return "\n".join(lines)

    rendered_zones = [z for z in snap.zones if z.zone in _RENDERED_ZONES]
    if not rendered_zones:
        lines.append(
            "  <i>none of the curated zones (Normal/DMA32/Movable) are "
            "present on this kernel — the snapshot still has every "
            "parsed zone for drill-down callers.</i>"
        )
        return "\n".join(lines)

    lines.append("  <b>Per-zone watermarks</b> (pages):")
    for zone in rendered_zones:
        free = zone.free
        wm_min = zone.min
        wm_low = zone.low
        wm_high = zone.high

        def _fmt(value: int | None) -> str:
            return "n/a" if value is None else f"{value:,}"

        marker = " ⚠" if zone.under_low else ""
        lines.append(
            f"  • <b>Node {zone.node} {zone.zone}:</b> "
            f"free=<code>{_fmt(free)}</code> "
            f"min=<code>{_fmt(wm_min)}</code> "
            f"low=<code>{_fmt(wm_low)}</code> "
            f"high=<code>{_fmt(wm_high)}</code>"
            f"{marker}"
        )

    lines.append("")
    if _has_direct_reclaim(snap):
        lines.append(
            "<i>⚠ direct reclaim — at least one zone has "
            "<code>free &lt;= min</code>, so allocating tasks are "
            "doing reclaim themselves in-kernel. Expect app-level "
            "latency spikes. See /admin_vmstat (pgsteal_direct, "
            "pgscan_direct), /admin_psi (memory.full), and "
            "/admin_buddyinfo for fragmentation context.</i>"
        )
    elif _has_pressure(snap):
        lines.append(
            "<i>⚠ kswapd active — at least one zone has "
            "<code>free &lt;= low</code>, the kernel is reclaiming "
            "in the background. Still quiet to userspace, but if "
            "allocation outpaces reclaim, free will reach <code>min</code> "
            "and direct reclaim begins. See /admin_vmstat counters and "
            "/admin_psi for the pressure footprint.</i>"
        )
    else:
        lines.append(
            "<i>No reclaim activity — all rendered zones have "
            "<code>free &gt; low</code>. DMA / Device zones are "
            "excluded by design from the markers (often near "
            "watermark on idle hosts, would create false-positives); "
            "full parsed zone data remains on the snapshot.</i>"
        )
    return "\n".join(lines)


async def handle_admin_zoneinfo(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_zoneinfo; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    under_low_zones = [f"{z.node}:{z.zone}" for z in snap.zones if z.under_low]
    under_min_zones = [f"{z.node}:{z.zone}" for z in snap.zones if z.under_min]
    log.bind(
        user_id=user.id,
        available=snap.available,
        zone_count=len(snap.zones),
        under_low_zones=under_low_zones,
        under_min_zones=under_min_zones,
    ).info("/admin_zoneinfo rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.zoneinfo")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_zoneinfo(message, settings)

    router.message.register(_entry, Command("admin_zoneinfo", ignore_case=True))
    return router
