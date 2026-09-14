"""``/admin_interrupts`` — per-CPU hardware IRQ distribution from /proc/interrupts.

Companion lens to /admin_softirqs. Where softirqs surface the
kernel-internal softirq machinery (NET_RX, TIMER, SCHED, …) on
each CPU, /proc/interrupts surfaces the layer below it: which
hardware IRQs (NIC queues, NVMe queues, USB, timers, the kernel's
own NMI/LOC/RES/CAL/TLB pseudo-interrupts) fired on which CPU.

Why both. The NET_RX skew that /admin_softirqs surfaces has its
root cause one layer down in this file — a single NIC IRQ pinned
to CPU0 is exactly the configuration that produces NET_RX skew.
But the hardware-IRQ view also surfaces patterns softirqs can't:

* **NVMe queue distribution** — modern NVMe drives expose one IRQ
  per submission queue (``nvme0q1``, ``nvme0q2``, …). If your
  workload is hitting only the first queue, you're losing
  parallelism that hardware can offer. /admin_io shows the
  per-device totals; this card shows the per-queue counts.
* **Per-CPU pseudo-IRQs (NMI, LOC, RES, CAL, TLB)** — these are
  the kernel's own bookkeeping interrupts. LOC (local APIC timer)
  fires once per HZ per CPU and is a quiet liveness signal:
  a CPU whose LOC count is frozen relative to its peers is wedged
  in a tickless-but-non-idle path. RES (rescheduling) skew across
  CPUs is the scheduler's footprint.

Zero-⚠ by design. Like softirqs/sockstat/swaps, the "is this skew
bad?" question is workload- and topology-dependent: on a single-IRQ
device pinned by smp_affinity, "skew" is the configured behavior;
on a multi-queue device with RSS, even distribution is the goal;
on a low-traffic host any variance looks skewed in percentage view.
The disclaimer footer makes this explicit and points at
/admin_softirqs (the layer above) and /admin_cpu (CPU affinity)
for context.

Card surface is potentially very large — modern hosts have 100+
IRQ lines × N CPUs. We curate: render only IRQs whose total
exceeds a small floor (drops the dozens of zero-count lines for
unused PCI slots) and cap at ``_RENDER_ROW_CAP``. Pseudo-IRQs
(named lines like NMI, LOC) get their own block so they're not
crowded out by numbered IRQs. Full per-CPU counts kept on the
snapshot so a future drill-down card / log enrichment can read
them without re-parsing.

Forward-compat: /proc/interrupts format has been stable for
decades. Header is ``CPU0 CPU1 …``; rows are
``<id>: <c0> <c1> … <device>``. ``<id>`` is numeric for hardware
IRQs and alphabetic (NMI, LOC, RES, CAL, TLB, MCE, MIS, IWI, …)
for pseudo-IRQs. The trailing device-name column is everything
after the counts — we keep it raw because new IRQ controllers
keep adding their own annotations (``IR-IO-APIC``, ``DMAR-MSI``,
``PCI-MSI-…``).

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


log = logger.bind(component="handlers.admin.interrupts")


_INTERRUPTS_PATH = Path("/proc/interrupts")

# Floor for inclusion in the curated render. A row whose total
# count is at or below this is dropped from display — modern hosts
# have dozens of zero-count PCI-MSI lines for unused slots and
# rendering them all defeats the card. The full snapshot keeps
# them for any drill-down caller.
_DISPLAY_TOTAL_FLOOR = 1000

# Hard cap on rendered rows. Even after the floor filter, a busy
# server with many NIC/NVMe queues can have 50+ active IRQs;
# Telegram message length is the real constraint here.
_RENDER_ROW_CAP = 30

# Pseudo-IRQ ids we explicitly surface in their own block. These
# are the operationally interesting kernel-internal ones; others
# (MCE, MIS, IWI, RTR, …) get rolled into the "others" footer.
_PSEUDO_KEEP: tuple[str, ...] = ("NMI", "LOC", "RES", "CAL", "TLB")


class _IrqRow:
    """One IRQ line: id, per-CPU counts, device label, totals.

    ``irq_id`` keeps the raw label so callers can distinguish "0"
    (the legacy timer IRQ) from "NMI". ``device`` is everything
    after the count columns — controller name and device name
    concatenated by the kernel, kept raw because IRQ-controller
    annotations vary across hardware and kernel versions.
    ``max_share`` is largest per-CPU count over total in [0.0, 1.0],
    or 0.0 when total is zero (no-call row).
    """

    __slots__ = ("device", "irq_id", "max_share", "per_cpu", "total")

    def __init__(
        self,
        *,
        irq_id: str,
        per_cpu: tuple[int, ...],
        device: str,
        total: int,
        max_share: float,
    ) -> None:
        self.irq_id = irq_id
        self.per_cpu = per_cpu
        self.device = device
        self.total = total
        self.max_share = max_share


class _InterruptsSnapshot:
    """Captured /proc/interrupts.

    ``rows`` — every parsed line, in file order. ``cpu_count`` — number
    of CPU columns. ``available`` — False on macOS dev / non-procfs
    container.
    """

    __slots__ = ("available", "cpu_count", "rows")

    def __init__(
        self,
        *,
        rows: tuple[_IrqRow, ...],
        cpu_count: int,
        available: bool,
    ) -> None:
        self.rows = rows
        self.cpu_count = cpu_count
        self.available = available


def _parse_interrupts(text: str) -> tuple[tuple[_IrqRow, ...], int]:
    """Parse /proc/interrupts. Returns (rows, cpu_count).

    Header line lists ``CPU0 CPU1 …`` — we count those tokens.
    Each subsequent line: ``<id>: <c0> <c1> … <device-name>``.
    The first cpu_count integers after the colon are counts;
    everything after that is the device label (joined with single
    spaces). Lines whose post-colon int prefix is shorter than
    cpu_count are dropped (mid-update read, kernel oddity).
    """
    lines = text.splitlines()
    if not lines:
        return ((), 0)
    header = lines[0].split()
    cpu_count = sum(1 for tok in header if tok.startswith("CPU"))
    if cpu_count == 0:
        return ((), 0)
    rows: list[_IrqRow] = []
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        id_part, _, rest = line.partition(":")
        irq_id = id_part.strip()
        if not irq_id:
            continue
        tokens = rest.split()
        if len(tokens) < cpu_count:
            # Not enough columns to be a full row — file mid-update
            # or unusual kernel format. Drop, don't guess.
            continue
        count_tokens = tokens[:cpu_count]
        per_cpu: list[int] = []
        parse_failed = False
        for tok in count_tokens:
            try:
                per_cpu.append(int(tok))
            except ValueError:
                # Single non-int column poisons the row. The kernel
                # emits ints in the count region; non-int here means
                # the line is not actually an IRQ data row (or the
                # format unexpectedly changed).
                parse_failed = True
                break
        if parse_failed:
            continue
        device = " ".join(tokens[cpu_count:])
        total = sum(per_cpu)
        max_share = (max(per_cpu) / total) if total > 0 else 0.0
        rows.append(
            _IrqRow(
                irq_id=irq_id,
                per_cpu=tuple(per_cpu),
                device=device,
                total=total,
                max_share=max_share,
            )
        )
    return (tuple(rows), cpu_count)


def _capture(*, path: Path = _INTERRUPTS_PATH) -> _InterruptsSnapshot:
    """Read /proc/interrupts + build a snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _InterruptsSnapshot(rows=(), cpu_count=0, available=False)
    rows, cpu_count = _parse_interrupts(text)
    return _InterruptsSnapshot(rows=rows, cpu_count=cpu_count, available=True)


def _is_pseudo(irq_id: str) -> bool:
    """Pseudo-IRQs have alphabetic ids (NMI, LOC, …); hardware
    IRQs have numeric ids. ``isdigit`` is the cleanest split."""
    return not irq_id.isdigit()


def _render(snap: _InterruptsSnapshot) -> str:
    lines = ["⚡ <b>Hardware IRQ distribution (/proc/interrupts)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/interrupts unavailable on this host — Linux-only "
            "card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if snap.cpu_count == 0 or not snap.rows:
        lines.append("  <i>parse failed or empty file — extremely unusual; check kernel build.</i>")
        return "\n".join(lines)

    lines.append(f"  <b>CPU count:</b> <code>{snap.cpu_count}</code>")
    lines.append("")

    # Hardware IRQs above the floor, sorted by total descending so
    # the highest-firing devices appear first — that's what the
    # operator wants to scan.
    hw_rows = [r for r in snap.rows if not _is_pseudo(r.irq_id) and r.total > _DISPLAY_TOTAL_FLOOR]
    hw_rows.sort(key=lambda r: r.total, reverse=True)
    truncated = len(hw_rows) > _RENDER_ROW_CAP
    hw_rows = hw_rows[:_RENDER_ROW_CAP]

    lines.append(
        f"  <b>Active hardware IRQs</b> (total &gt; <code>{_DISPLAY_TOTAL_FLOOR:,}</code>):"
    )
    if not hw_rows:
        lines.append("  <i>(none above the floor)</i>")
    else:
        for row in hw_rows:
            share_pct = row.max_share * 100.0
            # Device label can be long; we keep it intact because
            # truncating it loses the very info operator needs
            # (which queue / which controller).
            lines.append(
                f"  • <code>{row.irq_id}</code> "
                f"(<i>{row.device}</i>): "
                f"total=<code>{row.total:,}</code> "
                f"max-CPU share=<code>{share_pct:.1f}%</code>"
            )
        if truncated:
            lines.append(
                f"  <i>… more active IRQs not shown (cap <code>{_RENDER_ROW_CAP}</code>).</i>"
            )

    lines.append("")
    lines.append("  <b>Pseudo-IRQs (kernel-internal):</b>")
    pseudo_seen: set[str] = set()
    for kept in _PSEUDO_KEEP:
        match = next((r for r in snap.rows if r.irq_id == kept), None)
        if match is None:
            lines.append(f"  • <code>{kept}</code>: <i>not exposed</i>")
            continue
        pseudo_seen.add(kept)
        share_pct = match.max_share * 100.0
        lines.append(
            f"  • <code>{kept}</code> "
            f"(<i>{match.device}</i>): "
            f"total=<code>{match.total:,}</code> "
            f"max-CPU share=<code>{share_pct:.1f}%</code>"
        )

    # Other pseudo-IRQs — name only in the footer to keep things
    # scannable. Kernel adds new ones over time (RTR, IWI on x86_64;
    # PMI on some configs); naming them ensures the operator sees
    # what their kernel build exposes.
    extra_pseudo = [
        r.irq_id for r in snap.rows if _is_pseudo(r.irq_id) and r.irq_id not in pseudo_seen
    ]
    if extra_pseudo:
        lines.append("")
        lines.append(
            "  <i>Other pseudo-IRQ kinds parsed: "
            f"{', '.join(f'<code>{k}</code>' for k in extra_pseudo)}</i>"
        )

    lines.append("")
    lines.append(
        "<i>No warning markers on this card by design — IRQ skew is "
        "workload- and topology-dependent. A single-IRQ NIC pinned by "
        "<code>smp_affinity</code> intentionally lands all traffic on "
        "one CPU; that &quot;skew&quot; is the configuration. See "
        "/admin_softirqs for the kernel-side packet processing lens "
        "(NET_RX is the softirq fed by these hardware IRQs), and "
        "/admin_cpu for affinity context. Zero-count IRQ lines for "
        "unused PCI slots are filtered by the display floor; full "
        "counts remain on the snapshot for any drill-down caller.</i>"
    )
    return "\n".join(lines)


async def handle_admin_interrupts(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_interrupts; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    # Log signal: how many active rows we ended up rendering, the
    # NET_RX-feeding hardware IRQ family if discoverable. We can't
    # know which IRQ corresponds to NET_RX from this file alone (the
    # device label varies), so we log the highest-total HW IRQ as a
    # crude "where is the traffic landing" signal.
    hw_active = [
        r for r in snap.rows if not _is_pseudo(r.irq_id) and r.total > _DISPLAY_TOTAL_FLOOR
    ]
    top_hw = max(hw_active, key=lambda r: r.total, default=None)
    log.bind(
        user_id=user.id,
        available=snap.available,
        cpu_count=snap.cpu_count,
        row_count=len(snap.rows),
        hw_active_count=len(hw_active),
        top_hw_irq=top_hw.irq_id if top_hw else None,
        top_hw_device=top_hw.device if top_hw else None,
        top_hw_total=top_hw.total if top_hw else None,
        top_hw_max_share=top_hw.max_share if top_hw else None,
    ).info("/admin_interrupts rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.interrupts")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_interrupts(message, settings)

    router.message.register(_entry, Command("admin_interrupts", ignore_case=True))
    return router
