"""``/admin_buddyinfo`` — memory fragmentation from /proc/buddyinfo.

Every other memory-axis card on this surface shows totals or rates:

* /admin_meminfo — total/free/available bytes.
* /admin_memory — RSS / VMS / per-arena.
* /admin_vmstat — pgmajfault / oom_kill / pswpin rates.
* /admin_smaps — per-mapping memory.

None of them answer the question this card answers: **is the kernel
able to allocate large contiguous pages?** /proc/buddyinfo exposes
the free-list arrays of the buddy allocator — for each NUMA node ×
zone, a row of 11 counts giving the number of free blocks at each
power-of-two order (order-0 = 4 KiB, order-1 = 8 KiB, … order-10 =
4 MiB). When the high-order columns trend toward zero while order-0
remains plentiful, you have **memory fragmentation**: lots of free
RAM, but in pieces too small to satisfy THP / hugepage / jumbo-SKB
allocations. The symptom an operator sees first is slow allocations,
direct-reclaim spikes, or transparent-huge-page failures — and
nothing on this surface points at the cause until you read this file.

Why this matters even on small hosts. NIC drivers ask for order-3 or
order-4 contiguous pages for jumbo frames and ring buffers; failure
falls back to copying or dropping. THP wants order-9 (2 MiB);
failure means apps get 512 × 4 KiB instead of one 2 MiB page, with
the corresponding TLB-pressure tax. Even containerized workloads
inherit the host's fragmentation state.

The one operationally interesting predicate: **high-order
exhaustion**. We flag with ⚠ if any zone has zero free blocks at
order ≥ _HIGH_ORDER_THRESHOLD (default 4 → 64 KiB contiguous). Below
that order the buddy allocator can almost always find pages by
coalescing or reclaiming, so the threshold focuses the marker on
the genuinely-stuck case. Lower-order exhaustion shows up as the
order-3-or-below columns visibly thinning, but is operator-readable
without a marker.

Forward-compat: /proc/buddyinfo format is fixed: ``Node N, zone
NAME c0 c1 … cK``, K typically 10 (MAX_ORDER-1) but kernel
configs can change it. We parse cpu_count from the actual column
count rather than hardcoding 11, so a future kernel with
MAX_ORDER=12 (some embedded builds) parses identically.

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


log = logger.bind(component="handlers.admin.buddyinfo")


_BUDDYINFO_PATH = Path("/proc/buddyinfo")

# Order at which exhaustion (count == 0) is operationally
# concerning. Order 4 = 64 KiB contiguous; below this the buddy
# allocator nearly always succeeds via coalescing/reclaim. THP wants
# order 9 (2 MiB), but flagging only order-9 misses the more common
# jumbo-frame / large-skb allocation pressure that lives at orders
# 3–5. Picking 4 catches the common case without firing on routine
# small-order pressure.
_HIGH_ORDER_THRESHOLD = 4


class _BuddyRow:
    """One Node × zone row of free-block counts.

    ``counts`` is a tuple indexed by order (counts[k] = free blocks
    of size ``2**k`` pages). ``exhausted_orders`` is the sorted
    tuple of order indices where count is zero AND order >=
    _HIGH_ORDER_THRESHOLD — used by ⚠ predicate and rendering both.
    Pre-computed so the render path doesn't re-walk the tuple.
    """

    __slots__ = ("counts", "exhausted_orders", "node", "zone")

    def __init__(
        self,
        *,
        node: int,
        zone: str,
        counts: tuple[int, ...],
        exhausted_orders: tuple[int, ...],
    ) -> None:
        self.node = node
        self.zone = zone
        self.counts = counts
        self.exhausted_orders = exhausted_orders


class _BuddyinfoSnapshot:
    """Captured /proc/buddyinfo.

    * ``rows`` — every parsed Node × zone row.
    * ``max_order`` — number of order columns seen across all rows
      (typically 11 → orders 0..10; some kernels differ).
    * ``available`` — False on macOS dev / non-procfs container.
    """

    __slots__ = ("available", "max_order", "rows")

    def __init__(
        self,
        *,
        rows: tuple[_BuddyRow, ...],
        max_order: int,
        available: bool,
    ) -> None:
        self.rows = rows
        self.max_order = max_order
        self.available = available


def _parse_buddyinfo(text: str) -> tuple[tuple[_BuddyRow, ...], int]:
    """Parse /proc/buddyinfo. Returns (rows, max_order).

    Line format: ``Node N, zone NAME c0 c1 c2 … cK``. We split into
    tokens, expect ``Node`` and ``zone`` keywords at fixed positions,
    parse the node number from token[1] (stripped of trailing comma),
    the zone name from token[3], and the remaining tokens as the
    counts vector. Rows with fewer than 1 count token are dropped
    (a kernel emitting that would be broken; we degrade rather than
    crash). max_order is the max column count seen across all parsed
    rows — most kernels emit a uniform width but we don't assume.
    """
    rows: list[_BuddyRow] = []
    max_order = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("Node"):
            continue
        tokens = line.split()
        # Minimum viable: ``Node N, zone NAME c0`` — 5 tokens.
        if len(tokens) < 5 or tokens[2] != "zone":
            continue
        node_tok = tokens[1].rstrip(",")
        try:
            node = int(node_tok)
        except ValueError:
            # Malformed Node line — kernel emits this as a decimal
            # int; non-int means we're not looking at buddyinfo.
            continue
        zone = tokens[3]
        count_tokens = tokens[4:]
        counts: list[int] = []
        parse_failed = False
        for tok in count_tokens:
            try:
                counts.append(int(tok))
            except ValueError:
                parse_failed = True
                break
        if parse_failed or not counts:
            continue
        exhausted = tuple(i for i, c in enumerate(counts) if c == 0 and i >= _HIGH_ORDER_THRESHOLD)
        rows.append(
            _BuddyRow(
                node=node,
                zone=zone,
                counts=tuple(counts),
                exhausted_orders=exhausted,
            )
        )
        max_order = max(max_order, len(counts))
    return (tuple(rows), max_order)


def _capture(*, path: Path = _BUDDYINFO_PATH) -> _BuddyinfoSnapshot:
    """Read /proc/buddyinfo + build a snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _BuddyinfoSnapshot(rows=(), max_order=0, available=False)
    rows, max_order = _parse_buddyinfo(text)
    return _BuddyinfoSnapshot(rows=rows, max_order=max_order, available=True)


def _has_high_order_exhaustion(snap: _BuddyinfoSnapshot) -> bool:
    """⚠ predicate: any zone with at least one exhausted high-order
    slot. Kept as a function so it's testable in isolation and so
    the rendering path can ask the same question the test asks."""
    return any(r.exhausted_orders for r in snap.rows)


def _render(snap: _BuddyinfoSnapshot) -> str:
    lines = ["🧩 <b>Memory fragmentation (/proc/buddyinfo)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/buddyinfo unavailable on this host — Linux-only "
            "card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        lines.append("  <i>parse failed or empty file — extremely unusual; check kernel build.</i>")
        return "\n".join(lines)

    lines.append(
        f"  <b>Zones reported:</b> <code>{len(snap.rows)}</code>"
        f"  <b>Max order:</b> <code>{snap.max_order - 1}</code> "
        f"(<code>{2 ** (snap.max_order - 1) * 4} KiB</code> blocks)"
    )
    lines.append("")
    lines.append("  <b>Per-zone free-block counts</b> (order → count):")
    for row in snap.rows:
        # Compact rendering: ``Node 0 Normal: o0=1234 o3=123 o9=0 …``.
        # Showing every column gets long; we show a curated subset
        # (o0, o3, the threshold order, and the max order) to give
        # the operator the fragmentation arc at a glance. Full counts
        # remain on the snapshot for any drill-down caller.
        curated_indices: list[int] = []
        for idx in (
            0,
            _HIGH_ORDER_THRESHOLD - 1,
            _HIGH_ORDER_THRESHOLD,
            min(9, len(row.counts) - 1),
            len(row.counts) - 1,
        ):
            if idx < len(row.counts) and idx not in curated_indices and idx >= 0:
                curated_indices.append(idx)
        parts = [f"o{i}=<code>{row.counts[i]:,}</code>" for i in sorted(curated_indices)]
        marker = " ⚠" if row.exhausted_orders else ""
        lines.append(f"  • <b>Node {row.node} {row.zone}:</b> " + " ".join(parts) + marker)

    if _has_high_order_exhaustion(snap):
        lines.append("")
        lines.append(
            "<i>⚠ markers indicate zones with zero free blocks at "
            f"order ≥ <code>{_HIGH_ORDER_THRESHOLD}</code> "
            f"(<code>{2**_HIGH_ORDER_THRESHOLD * 4} KiB</code> "
            "contiguous). High-order exhaustion means the kernel "
            "can't satisfy THP / hugepage / large-SKB requests from "
            "the free list without compaction or reclaim — apps may "
            "see allocation stalls, NIC drivers may fall back to "
            "copying. See /admin_vmstat for compaction/reclaim "
            "counters and /admin_meminfo for AnonHugePages.</i>"
        )
    else:
        lines.append("")
        lines.append(
            "<i>No high-order exhaustion at present. Free-list arc "
            "thins toward higher orders by design — buddyinfo is a "
            "point-in-time snapshot, not a trend; for the fragmentation "
            "footprint over time compare against /admin_vmstat counters "
            "(compact_stall, pgmigrate_*).</i>"
        )
    return "\n".join(lines)


async def handle_admin_buddyinfo(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_buddyinfo; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    exhausted_zones = [f"{r.node}:{r.zone}" for r in snap.rows if r.exhausted_orders]
    log.bind(
        user_id=user.id,
        available=snap.available,
        zone_count=len(snap.rows),
        max_order=snap.max_order,
        high_order_exhausted=bool(exhausted_zones),
        exhausted_zones=exhausted_zones,
    ).info("/admin_buddyinfo rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.buddyinfo")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_buddyinfo(message, settings)

    router.message.register(_entry, Command("admin_buddyinfo", ignore_case=True))
    return router
