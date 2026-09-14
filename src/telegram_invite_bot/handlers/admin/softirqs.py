"""``/admin_softirqs`` — per-CPU softirq distribution from /proc/softirqs.

Existing CPU-axis cards (/admin_cpu posture, /admin_loadavg
run-queue, /admin_psi cpu) all view CPU as homogeneous. /proc/softirqs
exposes a different lens: which softirq kinds (NET_RX, NET_TX,
TIMER, SCHED, RCU, BLOCK, …) ran on which CPU and how often.

The single operationally interesting pattern here is **NET_RX skew**.
On a multi-CPU host without RSS (Receive Side Scaling) or RPS
(Receive Packet Steering) configured, all inbound packet
processing piles onto whichever CPU got the NIC's IRQ — that one
CPU saturates while the others sit idle, the host's effective
network capacity is 1/N of what its core count suggests, and
nothing in /admin_cpu or /admin_loadavg makes this visible because
both report aggregate CPU posture. This card surfaces the per-kind
per-CPU counts so an operator can see "NET_RX is 95% on CPU0".

Zero-⚠ by design (same posture as /admin_sockstat and /admin_swaps).
Picking a universal skew threshold is operator policy not card
policy: on a low-traffic host any natural variance looks skewed
in a percentage view; on a single-CPU host or container, "skew"
is undefined. We render the distribution and let the operator
read it. The disclaimer footer points at the RSS/RPS tuning
context so a future refactor doesn't accidentally add a marker.

Card surface is potentially large — many CPUs × ~10 softirq kinds.
We curate the kinds (NET_RX, NET_TX, TIMER, SCHED, RCU, BLOCK,
HRTIMER) because those are the operationally interesting ones,
and per-row we render only the per-CPU total + the max-CPU share
(percentage). Full per-CPU counts are kept on the snapshot so a
future card can drill down without re-parsing.

Forward-compat: kernel format has been stable since 2.6 — first
column header is "CPUN" repeating; data rows are
``KIND: c0 c1 c2 …``. We parse CPU count from the header row and
keep rows whose token count matches. New softirq kinds (e.g.
TASKLET on older kernels, IRQ_POLL on newer) parse identically.

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


log = logger.bind(component="handlers.admin.softirqs")


_SOFTIRQS_PATH = Path("/proc/softirqs")


# Curated softirq kinds rendered in the card. Order is operator-
# interest priority: NET_RX first (the skew-prone one), then NET_TX,
# then scheduler/timer/RCU, then BLOCK. Other kinds parsed (and on
# the snapshot) but not surfaced — keeps the card scannable.
_CURATED_KINDS: tuple[str, ...] = (
    "NET_RX",
    "NET_TX",
    "TIMER",
    "SCHED",
    "RCU",
    "BLOCK",
    "HRTIMER",
)


class _SoftirqRow:
    """One softirq kind + its per-CPU counts.

    ``per_cpu`` is a tuple positionally indexed by CPU number (so
    per_cpu[0] is CPU0). ``total`` is the sum; ``max_share`` is the
    largest per-CPU count divided by total, in [0.0, 1.0]. When
    total is zero (uncalled kind) max_share is 0.0 by convention.
    """

    __slots__ = ("kind", "max_share", "per_cpu", "total")

    def __init__(
        self,
        *,
        kind: str,
        per_cpu: tuple[int, ...],
        total: int,
        max_share: float,
    ) -> None:
        self.kind = kind
        self.per_cpu = per_cpu
        self.total = total
        self.max_share = max_share


class _SoftirqsSnapshot:
    """Captured /proc/softirqs.

    * ``rows`` — every parsed kind (curated + non-curated alike).
    * ``cpu_count`` — number of CPU columns in the header.
    * ``available`` — False on macOS dev / non-procfs container.
    """

    __slots__ = ("available", "cpu_count", "rows")

    def __init__(
        self,
        *,
        rows: tuple[_SoftirqRow, ...],
        cpu_count: int,
        available: bool,
    ) -> None:
        self.rows = rows
        self.cpu_count = cpu_count
        self.available = available


def _parse_softirqs(text: str) -> tuple[tuple[_SoftirqRow, ...], int]:
    """Parse /proc/softirqs. Returns (rows, cpu_count).

    First line is the CPU header (``CPU0 CPU1 …``). We count its
    tokens to learn cpu_count. Each subsequent line begins with
    ``<KIND>:`` followed by cpu_count integers. Lines whose token
    count after the colon doesn't match cpu_count are dropped (the
    file is being read mid-update or the kernel format changed).
    """
    lines = text.splitlines()
    if not lines:
        return ((), 0)
    header = lines[0].split()
    cpu_count = sum(1 for tok in header if tok.startswith("CPU"))
    if cpu_count == 0:
        return ((), 0)
    rows: list[_SoftirqRow] = []
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        kind_part, _, counts_part = line.partition(":")
        kind = kind_part.strip()
        if not kind:
            continue
        tokens = counts_part.split()
        if len(tokens) != cpu_count:
            continue
        per_cpu: list[int] = []
        parse_failed = False
        for tok in tokens:
            try:
                per_cpu.append(int(tok))
            except ValueError:
                # Single bad column poisons the row — kernel emits
                # ints here without exception in normal operation.
                parse_failed = True
                break
        if parse_failed:
            continue
        total = sum(per_cpu)
        max_share = (max(per_cpu) / total) if total > 0 else 0.0
        rows.append(
            _SoftirqRow(
                kind=kind,
                per_cpu=tuple(per_cpu),
                total=total,
                max_share=max_share,
            )
        )
    return (tuple(rows), cpu_count)


def _capture(*, path: Path = _SOFTIRQS_PATH) -> _SoftirqsSnapshot:
    """Read /proc/softirqs + build a snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _SoftirqsSnapshot(rows=(), cpu_count=0, available=False)
    rows, cpu_count = _parse_softirqs(text)
    return _SoftirqsSnapshot(rows=rows, cpu_count=cpu_count, available=True)


def _row_by_kind(snap: _SoftirqsSnapshot, kind: str) -> _SoftirqRow | None:
    """Lookup helper — operator-facing API surface for callers that
    want a specific row without re-walking the tuple."""
    for row in snap.rows:
        if row.kind == kind:
            return row
    return None


def _render(snap: _SoftirqsSnapshot) -> str:
    lines = ["⚡ <b>Softirq distribution (/proc/softirqs)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/softirqs unavailable on this host — Linux-only "
            "card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if snap.cpu_count == 0 or not snap.rows:
        lines.append("  <i>parse failed or empty file — extremely unusual; check kernel build.</i>")
        return "\n".join(lines)

    lines.append(f"  <b>CPU count:</b> <code>{snap.cpu_count}</code>")
    lines.append("")
    lines.append("  <b>Curated kinds:</b>")
    for kind in _CURATED_KINDS:
        row = _row_by_kind(snap, kind)
        if row is None:
            lines.append(f"  • <code>{kind}</code>: <i>not exposed</i>")
            continue
        share_pct = row.max_share * 100.0
        lines.append(
            f"  • <code>{kind}</code>: total=<code>{row.total:,}</code> "
            f"max-CPU share=<code>{share_pct:.1f}%</code>"
        )

    lines.append("")
    # Other parsed kinds — render as a one-liner so operator sees the
    # full set without us forcing 10+ rows of decoration.
    extra = [r.kind for r in snap.rows if r.kind not in _CURATED_KINDS]
    if extra:
        lines.append(
            f"  <i>Other softirq kinds parsed (totals only): "
            f"{', '.join(f'<code>{k}</code>' for k in extra)}</i>"
        )
        lines.append("")

    lines.append(
        "<i>No warning markers on this card by design. NET_RX max-CPU "
        "share &gt;80% on a multi-CPU host typically indicates "
        "missing RSS/RPS configuration — packet processing piled on "
        "one CPU — but the threshold is workload-dependent and the "
        "single-CPU case makes &quot;skew&quot; meaningless. Operator "
        "policy, not card policy. See /admin_cpu for affinity + load, "
        "/admin_netdev for per-interface counters.</i>"
    )
    return "\n".join(lines)


async def handle_admin_softirqs(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_softirqs; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    net_rx = _row_by_kind(snap, "NET_RX")
    log.bind(
        user_id=user.id,
        available=snap.available,
        cpu_count=snap.cpu_count,
        row_count=len(snap.rows),
        net_rx_total=net_rx.total if net_rx else None,
        net_rx_max_share=net_rx.max_share if net_rx else None,
    ).info("/admin_softirqs rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.softirqs")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_softirqs(message, settings)

    router.message.register(_entry, Command("admin_softirqs", ignore_case=True))
    return router
