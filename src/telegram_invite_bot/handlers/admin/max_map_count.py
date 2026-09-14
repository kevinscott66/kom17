"""``/admin_max_map_count`` — VMA count vs vm.max_map_count.

The Linux kernel caps the number of distinct virtual memory
areas (VMAs) a single process can hold. The knob is
``vm.max_map_count``; default 65530. Hitting it returns
``ENOMEM`` from ``mmap(2)``, ``mprotect(2)``, and any glibc
allocation path that needs a fresh arena. Surfaces in
user-space as opaque MemoryError or ``Cannot allocate memory``
even on a host with abundant free RAM.

Why this bot specifically cares: every mmap-heavy consumer in
the process tree contributes to the count.

* SQLite (five WAL databases per the strangler plan) mmap-reads
  database files by default — one VMA per file, plus more as
  the database grows past the initial mmap window.
* Python's import machinery mmap-loads .so extensions —
  aiohttp, pydantic, SQLAlchemy, aiosqlite, loguru, pillow
  all contribute multiple VMAs each.
* glibc's per-thread allocator arenas occupy one VMA each;
  threadpool growth multiplies them.
* JIT'd extensions and ctypes-loaded libraries add more.

A long-running bot with growing thread pools and a few
megabytes of mmap'd databases routinely crosses 10-20k VMAs.
That's still safe with the default 65530 ceiling, but cloud
images and container runtimes sometimes ship with the knob
lowered to a few thousand to fit a "small process" memory
model. When the bot suddenly can't open a new database
connection or load a lazy extension, vm.max_map_count is
the place to check — and the symptoms look nothing like
out-of-memory because there's plenty of free RAM.

Sources:

* ``/proc/sys/vm/max_map_count`` — the kernel ceiling, single int.
* ``/proc/self/maps`` — one line per VMA for *this* process.
  We count lines.

⚠ predicate: maps_count / max_map_count >= ``_MAP_WARN_RATIO``
(0.8). Same 80% mark as the host-ceiling trio for operator
consistency. Distinct from the trio in scope — this one is
per-process, not host-wide — but the operator signal "approaching
the cliff" is identical. Pinned cry-wolf must-not-fire on a
realistic healthy sample (200 maps of 65530 default).

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


log = logger.bind(component="handlers.admin.max_map_count")


_MAX_MAP_COUNT_PATH = Path("/proc/sys/vm/max_map_count")
_SELF_MAPS_PATH = Path("/proc/self/maps")


# Same threshold as the host-ceiling trio. The cliff here is
# different (ENOMEM from mmap(2) vs EAGAIN from fork/io_setup)
# but the operator-intuitive "approaching limit" mark is the
# same — far enough from the hard cap that the operator can
# raise vm.max_map_count or audit the leaker before the bot
# starts failing to import lazy extensions or open new database
# connections.
_MAP_WARN_RATIO = 0.8


class _MaxMapSnapshot:
    """Captured VMA count vs vm.max_map_count.

    Each field carries ``-1`` for "couldn't read this source"
    so render shows 'unknown' rather than misleading 0. The
    two sources are independent (a stripped container may
    expose /proc/sys/vm/max_map_count but seal /proc/self/maps
    via hidepid, or vice versa).
    """

    __slots__ = ("available", "maps_count", "max_map_count")

    def __init__(self, *, maps_count: int, max_map_count: int, available: bool) -> None:
        self.maps_count = maps_count
        self.max_map_count = max_map_count
        self.available = available

    @property
    def usage_ratio(self) -> float:
        # Defensive: ceiling<=0 or maps sentinel returns 0.0
        # ("we don't know, so don't warn") — matches the host-
        # ceiling trio posture. The alternative ("treat unknown
        # as full") would fire ⚠ on every macOS-dev or stripped-
        # container run.
        if self.max_map_count <= 0 or self.maps_count < 0:
            return 0.0
        return self.maps_count / self.max_map_count

    @property
    def under_pressure(self) -> bool:
        return self.usage_ratio >= _MAP_WARN_RATIO


def _read_int(path: Path) -> int:
    """Read a single int from a sysctl file; ``-1`` on any
    failure. Same contract as the host-ceiling trio."""
    try:
        return int(path.read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return -1


def _count_maps(path: Path) -> int:
    """Count non-empty lines in /proc/self/maps. ``-1`` on
    OSError (file absent, EPERM under hidepid). Empty lines
    are skipped defensively even though the kernel never
    emits them — keeps the count honest if a future kernel
    or LSM intercepts the read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return -1
    return sum(1 for line in text.splitlines() if line.strip())


def _capture(
    *,
    max_map_count_path: Path = _MAX_MAP_COUNT_PATH,
    self_maps_path: Path = _SELF_MAPS_PATH,
) -> _MaxMapSnapshot:
    max_map_count = _read_int(max_map_count_path)
    maps_count = _count_maps(self_maps_path)

    # available iff at least one source produced something
    # usable. Both-missing means non-procfs (macOS dev) and
    # we render the dedicated unavailable note instead of
    # two 'unknown' rows.
    available = max_map_count >= 0 or maps_count >= 0
    return _MaxMapSnapshot(
        maps_count=maps_count,
        max_map_count=max_map_count,
        available=available,
    )


def _fmt(value: int) -> str:
    if value < 0:
        return "unknown"
    return f"{value:,}"


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _render(snap: _MaxMapSnapshot) -> str:
    lines = ["🗺 <b>VMA count vs vm.max_map_count</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/sys/vm/max_map_count and /proc/self/maps "
            "unreadable — non-procfs container, macOS dev, or "
            "hidepid sealing /proc/self/maps.</i>"
        )
        return "\n".join(lines)

    warn = snap.under_pressure

    lines.append(f"  current VMAs (/proc/self/maps)=<code>{_fmt(snap.maps_count)}</code>")
    lines.append(f"  vm.max_map_count=<code>{_fmt(snap.max_map_count)}</code>")
    if snap.max_map_count > 0 and snap.maps_count >= 0:
        marker = " ⚠" if warn else ""
        lines.append(f"  usage=<code>{_fmt_pct(snap.usage_ratio)}</code>{marker}")
    else:
        lines.append("  usage=<code>unknown</code>")

    lines.append("")
    if warn:
        lines.append(
            f"<i>⚠ VMA count at <code>{_fmt_pct(snap.usage_ratio)}</code> "
            "of <code>vm.max_map_count</code>. mmap(2), mprotect(2), "
            "and any glibc path that needs a fresh arena return "
            "ENOMEM above the cap — surfaces in Python as opaque "
            "MemoryError on import or database connect, even when "
            "/admin_meminfo shows plenty of free RAM. Audit with "
            "<code>wc -l /proc/self/maps</code>. Common leakers: "
            "SQLite mmap-extending past initial window, glibc "
            "per-thread arenas, ctypes loads, JIT'd extensions. "
            "Raise via <code>sysctl -w vm.max_map_count=262144</code> "
            "(Elasticsearch/databases ship with this as a baseline).</i>"
        )
    else:
        lines.append(
            f"<i>No warnings — VMA count under "
            f"<code>{int(_MAP_WARN_RATIO * 100)}%</code> of "
            "vm.max_map_count. This is the per-process ceiling — "
            "distinct from /admin_file_nr (system-wide fd table), "
            "/admin_pid_max (system-wide task count), /admin_aio_nr "
            "(system-wide AIO contexts). The failure mode is ENOMEM "
            "from mmap(2), not EAGAIN — looks nothing like an OOM "
            "in /admin_meminfo because there's free RAM, just no "
            "free VMA slots.</i>"
        )
    return "\n".join(lines)


async def handle_admin_max_map_count(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_max_map_count; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        maps_count=snap.maps_count,
        max_map_count=snap.max_map_count,
        usage_ratio=snap.usage_ratio,
    ).info("/admin_max_map_count rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.max_map_count")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_max_map_count(message, settings)

    router.message.register(_entry, Command("admin_max_map_count", ignore_case=True))
    return router
