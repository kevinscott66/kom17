"""``/admin_dirty`` — dirty-page writeback pressure vs vm.dirty_ratio.

/admin_meminfo already surfaces ``Dirty`` and ``Writeback``
(raw kB modified-but-not-yet-flushed and currently-flushing).
What it does NOT surface is the *threshold* the kernel will
enforce against those numbers: ``vm.dirty_ratio`` (default 20%
of available memory — once dirty pages cross it every write(2)
in user-space stalls synchronously until writeback catches up)
and ``vm.dirty_background_ratio`` (default 10% — kernel kicks
flush threads but writes are still non-blocking).

The synchronous-stall mode is the dangerous one for *this*
bot: SQLite WAL fsync on the five databases (see strangler
plan) plus aiohttp body buffering can suddenly take seconds
on an otherwise-healthy box when the dirty page ratio crosses
``vm.dirty_ratio``. From the bot's perspective this looks
like Telegram polling timeouts or aiosqlite hangs — and is
the kind of failure mode where /admin_meminfo shows "lots
of free memory" because the dirty pages count as cached.

This card adds the *ratio*: how close are we to vm.dirty_ratio
right now? When the ratio crosses our 80%-of-threshold mark,
⚠ fires with the writeback-stall narrative.

Sources:

* ``/proc/meminfo`` — Dirty, Writeback, MemTotal (kB).
* ``/proc/sys/vm/dirty_ratio`` — percent (int 0-100). When
  set to 0 the kernel uses ``dirty_bytes`` instead.
* ``/proc/sys/vm/dirty_background_ratio`` — percent (int).

⚠ predicate: dirty / (MemTotal * dirty_ratio / 100) >= 0.8.
Dirty_ratio=0 (bytes-mode) → we skip the ratio and render
'unknown' rather than misleading 100%. Pinned cry-wolf must-
not-fire on canonical-healthy sample (Dirty=2MB, MemTotal=8GB,
dirty_ratio=20 — well under the stall threshold).

Same wiring as every other admin card — silent-drop, private-
only, pure stdlib, hermetic via keyword-only path injection.
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


log = logger.bind(component="handlers.admin.dirty")


_MEMINFO_PATH = Path("/proc/meminfo")
_DIRTY_RATIO_PATH = Path("/proc/sys/vm/dirty_ratio")
_DIRTY_BG_RATIO_PATH = Path("/proc/sys/vm/dirty_background_ratio")


# Same 80%-of-threshold mark as the host-ceiling trio
# (file_nr / pid_max / aio_nr). Operator-intuitive
# "approaching limit" — the cliff here is the synchronous
# writeback stall at 100% of dirty_ratio, far enough that
# the operator can tune or investigate before user-facing
# Telegram timeouts appear.
_DIRTY_WARN_RATIO = 0.8


class _DirtySnapshot:
    """Captured dirty-pages / writeback state.

    Per-field ``-1`` sentinel means "couldn't read this source"
    — render shows 'unknown' rather than misleading 0. The four
    sources are independent files; rendering degrades per-field
    so a stripped container that only exposes /proc/meminfo still
    gets useful raw numbers, just without the ratio.
    """

    __slots__ = (
        "available",
        "dirty_bg_ratio",
        "dirty_kb",
        "dirty_ratio",
        "memtotal_kb",
        "writeback_kb",
    )

    def __init__(
        self,
        *,
        dirty_kb: int,
        writeback_kb: int,
        memtotal_kb: int,
        dirty_ratio: int,
        dirty_bg_ratio: int,
        available: bool,
    ) -> None:
        self.dirty_kb = dirty_kb
        self.writeback_kb = writeback_kb
        self.memtotal_kb = memtotal_kb
        self.dirty_ratio = dirty_ratio
        self.dirty_bg_ratio = dirty_bg_ratio
        self.available = available

    @property
    def usage_ratio(self) -> float:
        # Defensive: any of MemTotal/dirty_ratio/dirty_kb being
        # unknown (-1) or zero collapses to 0.0 ("don't warn on
        # unknown"). Specifically: dirty_ratio=0 is *legal* and
        # means the kernel is in bytes-mode (vm.dirty_bytes set
        # instead) — we honestly don't know the threshold from
        # the ratio knob alone, so don't pretend.
        if self.memtotal_kb <= 0 or self.dirty_ratio <= 0 or self.dirty_kb < 0:
            return 0.0
        threshold_kb = self.memtotal_kb * self.dirty_ratio / 100.0
        if threshold_kb <= 0:
            return 0.0
        return self.dirty_kb / threshold_kb

    @property
    def under_pressure(self) -> bool:
        return self.usage_ratio >= _DIRTY_WARN_RATIO


def _read_int(path: Path) -> int:
    """Read a single int from a sysctl file; ``-1`` on any
    failure. Same contract as the host-ceiling trio."""
    try:
        return int(path.read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return -1


def _parse_meminfo(text: str) -> tuple[int, int, int]:
    """Extract (Dirty, Writeback, MemTotal) in kB from /proc/meminfo.
    Each field missing or unparseable becomes ``-1``. Format is
    one ``Key:   <value> kB`` per line, stable since 2.6.
    """
    dirty = writeback = memtotal = -1
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        key = key.strip()
        if key not in ("Dirty", "Writeback", "MemTotal"):
            continue
        tokens = rest.strip().split()
        if not tokens:
            continue
        try:
            value = int(tokens[0])
        except ValueError:
            continue
        if key == "Dirty":
            dirty = value
        elif key == "Writeback":
            writeback = value
        else:
            memtotal = value
    return dirty, writeback, memtotal


def _capture(
    *,
    meminfo_path: Path = _MEMINFO_PATH,
    dirty_ratio_path: Path = _DIRTY_RATIO_PATH,
    dirty_bg_ratio_path: Path = _DIRTY_BG_RATIO_PATH,
) -> _DirtySnapshot:
    try:
        meminfo_text = meminfo_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        meminfo_text = ""
    dirty_kb, writeback_kb, memtotal_kb = _parse_meminfo(meminfo_text)
    dirty_ratio = _read_int(dirty_ratio_path)
    dirty_bg_ratio = _read_int(dirty_bg_ratio_path)

    # "available" is True iff at least one source produced a
    # usable value — matches the host-ceiling-trio posture.
    # All-missing means non-procfs (macOS dev) and we render
    # the dedicated unavailable note.
    available = (
        dirty_kb >= 0
        or writeback_kb >= 0
        or memtotal_kb >= 0
        or dirty_ratio >= 0
        or dirty_bg_ratio >= 0
    )
    return _DirtySnapshot(
        dirty_kb=dirty_kb,
        writeback_kb=writeback_kb,
        memtotal_kb=memtotal_kb,
        dirty_ratio=dirty_ratio,
        dirty_bg_ratio=dirty_bg_ratio,
        available=available,
    )


def _fmt_kb(value: int) -> str:
    if value < 0:
        return "unknown"
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.2f} GiB"
    if value >= 1024:
        return f"{value / 1024:.2f} MiB"
    return f"{value} kB"


def _fmt_pct_int(value: int) -> str:
    if value < 0:
        return "unknown"
    return f"{value}%"


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _render(snap: _DirtySnapshot) -> str:
    lines = ["💧 <b>Dirty-page writeback pressure</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>None of /proc/meminfo or /proc/sys/vm/dirty_* "
            "readable — Linux-only surface (macOS dev / "
            "non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    warn = snap.under_pressure

    lines.append(f"  Dirty=<code>{_fmt_kb(snap.dirty_kb)}</code>")
    lines.append(f"  Writeback=<code>{_fmt_kb(snap.writeback_kb)}</code>")
    lines.append(f"  MemTotal=<code>{_fmt_kb(snap.memtotal_kb)}</code>")
    lines.append("")
    lines.append("  <b>Kernel thresholds:</b>")
    lines.append(f"    vm.dirty_ratio=<code>{_fmt_pct_int(snap.dirty_ratio)}</code>")
    lines.append(f"    vm.dirty_background_ratio=<code>{_fmt_pct_int(snap.dirty_bg_ratio)}</code>")

    if snap.memtotal_kb > 0 and snap.dirty_ratio > 0 and snap.dirty_kb >= 0:
        marker = " ⚠" if warn else ""
        lines.append(
            f"  usage=<code>{_fmt_pct(snap.usage_ratio)}</code> of vm.dirty_ratio threshold{marker}"
        )
    else:
        # dirty_ratio==0 means the kernel uses dirty_bytes (the
        # absolute knob) — we honestly don't know the threshold
        # from the percent knob, so render 'unknown' rather than
        # crash or fabricate.
        lines.append("  usage=<code>unknown</code> (vm.dirty_bytes mode or missing source)")

    lines.append("")
    if warn:
        lines.append(
            f"<i>⚠ dirty pages at <code>{_fmt_pct(snap.usage_ratio)}</code> "
            "of <code>vm.dirty_ratio</code> threshold. When the "
            "ratio hits 100%, every write(2) on the host blocks "
            "synchronously until writeback drains — surfacing as "
            "second-scale aiosqlite hangs and Telegram polling "
            "timeouts for this bot. /admin_meminfo shows the raw "
            "Dirty kB but not the ratio against the kernel knob; "
            "that's the gap this card closes. Either raise "
            "<code>vm.dirty_ratio</code> if the host has RAM "
            "headroom, or tune the writer (busy WAL database, "
            "log spam, big aiohttp uploads) that's filling the "
            "page cache faster than the disk drains.</i>"
        )
    else:
        lines.append(
            f"<i>No warnings — dirty pages under "
            f"<code>{int(_DIRTY_WARN_RATIO * 100)}%</code> of "
            "vm.dirty_ratio. /admin_meminfo carries the raw "
            "Dirty/Writeback numbers; this card adds the ratio "
            "against the kernel knob — the bit that actually "
            "decides whether write(2) blocks.</i>"
        )
    return "\n".join(lines)


async def handle_admin_dirty(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_dirty; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        dirty_kb=snap.dirty_kb,
        writeback_kb=snap.writeback_kb,
        memtotal_kb=snap.memtotal_kb,
        dirty_ratio=snap.dirty_ratio,
        dirty_bg_ratio=snap.dirty_bg_ratio,
        usage_ratio=snap.usage_ratio,
    ).info("/admin_dirty rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.dirty")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_dirty(message, settings)

    router.message.register(_entry, Command("admin_dirty", ignore_case=True))
    return router
