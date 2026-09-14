"""``/admin_swaps`` — per-device swap configuration from /proc/swaps.

/admin_meminfo surfaces SwapTotal/SwapFree as host-wide totals —
that answers "is the host using swap right now?". It can't answer
"how is swap configured?" — and the configuration is what tells
an operator whether the box is operating as intended:

* Is swap on a partition or a file? (Performance + crash-recovery
  difference; swapfiles can land on slow filesystems.)
* How many swap areas are active? (One is normal; multiple with
  different priorities is a deliberate tiered-paging setup;
  zero means the kernel will OOM-kill instead of paging.)
* What are the priorities? (Higher = preferred. Wrong priorities
  silently route paging to the wrong device.)

This is /proc/swaps territory — the kernel's authoritative
single-page view of swap layout.

Cry-wolf posture: ZERO ⚠ on this card. Same logic as /admin_limits
— a swap configuration is operator intent. Zero swap is a
deliberate choice on many production hosts (Kubernetes nodes
historically required swap off; some DB workloads disable it to
keep OOM-kill semantics predictable). A swapfile vs partition is
a deliberate choice. Priority ordering is a deliberate choice.
There's no value here we can ⚠ on without presuming operator
intent — exactly the cry-wolf failure mode this codebase rejects.

For the actual "host is paging hard" health signal, /admin_meminfo
is the right card — but even there we don't ⚠ on swap usage,
because the kernel paging cold pages out IS swap doing its job.

Forward-compat: /proc/swaps format has been stable since the 90s
(header line + space-separated tuples), but we tolerate short or
malformed lines by skipping them — degrade-don't-crash.

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


log = logger.bind(component="handlers.admin.swaps")


_SWAPS_PATH = Path("/proc/swaps")


# /proc/swaps reports sizes in 1024-byte blocks (kB). Multiply at
# the parse boundary, not at render — keeping the snapshot in
# bytes means predicates and tests don't have to remember the
# unit. Same convention as /admin_smaps.
_BLOCK_BYTES = 1024


class _SwapRow:
    """One /proc/swaps line — a single swap area.

    /proc/swaps format: ``Filename Type Size Used Priority``.
    * ``filename`` — path on disk; identifies the device or file.
    * ``swap_type`` — "partition" or "file" (some kernels also
      report "raw"; we accept the raw string).
    * ``size_bytes`` / ``used_bytes`` — capacity / current
      utilisation, in bytes (converted from the kernel's 1024-B
      blocks at parse time).
    * ``priority`` — int; higher = preferred. Can be negative
      (the kernel's auto-assigned default is negative).
    """

    __slots__ = ("filename", "priority", "size_bytes", "swap_type", "used_bytes")

    def __init__(
        self,
        *,
        filename: str,
        swap_type: str,
        size_bytes: int,
        used_bytes: int,
        priority: int,
    ) -> None:
        self.filename = filename
        self.swap_type = swap_type
        self.size_bytes = size_bytes
        self.used_bytes = used_bytes
        self.priority = priority


class _SwapsSnapshot:
    """Captured swap layout.

    * ``rows`` — every active swap area. Empty tuple is legal
      and meaningful: zero swap is a valid (and common) production
      posture.
    * ``available`` — False when /proc/swaps can't be read at all
      (macOS dev, container without /proc).
    """

    __slots__ = ("available", "rows")

    def __init__(
        self,
        *,
        rows: tuple[_SwapRow, ...],
        available: bool,
    ) -> None:
        self.rows = rows
        self.available = available


def _parse_swaps(text: str) -> tuple[_SwapRow, ...]:
    """Parse /proc/swaps.

    First line is a header (``Filename Type Size Used Priority``)
    — we skip it. Subsequent lines are space-separated; the
    Filename may contain literal spaces in pathological cases,
    but the kernel emits the same octal-escape (``\\040``) it does
    in /proc/self/mounts, so we still split on whitespace and the
    operator sees the raw escape — same convention as
    /admin_mounts.
    """
    rows: list[_SwapRow] = []
    for idx, raw_line in enumerate(text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        if idx == 0 and line.startswith("Filename"):
            # Header — skip. We could also accept any first line
            # that has "Filename" as the first token, which would
            # tolerate slightly nonstandard variants.
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            size_blocks = int(parts[2])
            used_blocks = int(parts[3])
            priority = int(parts[4])
        except ValueError:
            continue
        rows.append(
            _SwapRow(
                filename=parts[0],
                swap_type=parts[1],
                size_bytes=size_blocks * _BLOCK_BYTES,
                used_bytes=used_blocks * _BLOCK_BYTES,
                priority=priority,
            )
        )
    return tuple(rows)


def _capture(*, path: Path = _SWAPS_PATH) -> _SwapsSnapshot:
    """Read /proc/swaps + build a snapshot.

    Keyword-only path parameter so tests can inject a tmp file —
    same hermetic-fixture pattern as every other diagnostic card.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _SwapsSnapshot(rows=(), available=False)
    return _SwapsSnapshot(rows=_parse_swaps(text), available=True)


def _fmt_bytes(value: int) -> str:
    if value >= 1024 * 1024 * 1024:
        return f"{value / (1024**3):.2f} GiB"
    if value >= 1024 * 1024:
        return f"{value / (1024**2):.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value:,} B"


def _render(snap: _SwapsSnapshot) -> str:
    lines = ["💤 <b>Swap layout (/proc/swaps)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/swaps unavailable on this host — Linux-only "
            "card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        # Zero swap is a legitimate posture (Kubernetes nodes, DB
        # boxes that prefer OOM-kill semantics). Render the
        # absence explicitly so the operator sees it's not a bug.
        lines.append(
            "  <i>no swap areas configured. The kernel will "
            "OOM-kill rather than page when memory is exhausted. "
            "Common posture for Kubernetes nodes and DB workloads "
            "that prefer predictable failure to silent slowdown.</i>"
        )
        return "\n".join(lines)

    for row in snap.rows:
        used = _fmt_bytes(row.used_bytes)
        size = _fmt_bytes(row.size_bytes)
        # Percentage as decoration only — operator-readable, no ⚠.
        pct = ""
        if row.size_bytes > 0:
            pct = f" ({row.used_bytes / row.size_bytes * 100:.1f}%)"
        lines.append(
            f"  • <code>{row.filename}</code> <i>({row.swap_type}, prio {row.priority})</i>"
        )
        lines.append(f"      used <code>{used}</code> / <code>{size}</code>{pct}")

    # Summarise so an operator running multiple swap areas can
    # see the aggregate without doing the addition in their head.
    if len(snap.rows) > 1:
        total_bytes = sum(r.size_bytes for r in snap.rows)
        used_bytes = sum(r.used_bytes for r in snap.rows)
        lines.append("")
        lines.append(
            f"  <b>Aggregate:</b> "
            f"<code>{_fmt_bytes(used_bytes)}</code> / "
            f"<code>{_fmt_bytes(total_bytes)}</code> "
            f"across {len(snap.rows)} swap areas"
        )

    lines.append("")
    lines.append(
        "<i>No warning markers on this card by design — a swap "
        "configuration is operator intent (presence/absence, "
        "partition vs file, priority ordering are all deliberate "
        "choices). For host memory-pressure signals see "
        "/admin_meminfo (MemAvailable). For per-process memory "
        "see /admin_memory + /admin_smaps.</i>"
    )
    return "\n".join(lines)


async def handle_admin_swaps(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_swaps; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        swap_area_count=len(snap.rows),
        total_bytes=sum(r.size_bytes for r in snap.rows),
    ).info("/admin_swaps rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.swaps")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_swaps(message, settings)

    router.message.register(_entry, Command("admin_swaps", ignore_case=True))
    return router
