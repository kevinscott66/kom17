"""``/admin_thp`` — Transparent Huge Pages mode + usage.

Transparent Huge Pages is the kernel feature that opportun-
istically backs anonymous memory mappings with 2 MiB pages
instead of the usual 4 KiB. On most workloads it is a quiet
TLB-pressure win; on database-shaped workloads (random small
reads, mmap-backed page cache, frequent realloc) it is a
notorious latency footgun.

Why this bot specifically cares: every major database project
that gives THP a position statement — PostgreSQL, MongoDB,
Redis, MariaDB — recommends disabling ``always``-mode THP
because the *khugepaged* compaction kthread can stall a
syscall for tens to hundreds of milliseconds while it scans
for 2 MiB-aligned free regions and migrates pages. The
strangler plan keeps five SQLite databases on WAL served
through aiosqlite; SQLite mmap-reads the database file by
default, and the WAL writer fsyncs frequently — both shapes
are exactly what triggers the khugepaged-stall mode. From
the bot's perspective this surfaces as random 100-300ms
spikes in handler latency that don't correlate with any
visible load.

⚠ predicate: ``enabled=always``. Distros' modern defaults
already favour ``madvise`` (opt-in via ``madvise(MADV_HUGEPAGE)``)
or ``never``, but the *kernel* default is still ``always`` and
many cloud base images inherit it. ``madvise`` and ``never`` do
not fire ⚠ — they are the safe-for-databases postures.

Sources:

* ``/sys/kernel/mm/transparent_hugepage/enabled`` — the mode
  selector, format ``always [madvise] never`` with the active
  entry bracketed.
* ``/sys/kernel/mm/transparent_hugepage/defrag`` — controls
  the khugepaged compaction policy; rendered informationally,
  no ⚠ (its safe value depends on the enabled mode).
* ``/proc/meminfo`` — ``AnonHugePages``: how much anonymous
  memory is *actually* backed by THP right now. Useful
  context: a host with enabled=madvise but AnonHugePages=0
  means nothing is using THP — the knob is moot.

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


log = logger.bind(component="handlers.admin.thp")


_THP_ENABLED_PATH = Path("/sys/kernel/mm/transparent_hugepage/enabled")
_THP_DEFRAG_PATH = Path("/sys/kernel/mm/transparent_hugepage/defrag")
_MEMINFO_PATH = Path("/proc/meminfo")


# The only mode that fires ⚠. madvise is opt-in (databases
# that know better can stay clear); never is unconditionally
# safe. always is the only setting where the kernel may stall
# a syscall in khugepaged compaction without warning.
_UNSAFE_MODE = "always"


class _ThpSnapshot:
    """Captured THP state.

    Each field carries an empty string / -1 sentinel for
    "couldn't read" so render distinguishes "kernel doesn't
    expose this" from a real value. The three sources are
    independent (THP can be CONFIG=n while /proc/meminfo
    still exists, or vice versa on stripped sysfs).
    """

    __slots__ = ("anon_hugepages_kb", "available", "defrag", "enabled")

    def __init__(
        self, *, enabled: str, defrag: str, anon_hugepages_kb: int, available: bool
    ) -> None:
        self.enabled = enabled
        self.defrag = defrag
        self.anon_hugepages_kb = anon_hugepages_kb
        self.available = available

    @property
    def under_pressure(self) -> bool:
        return self.enabled == _UNSAFE_MODE


def _parse_bracketed(text: str) -> str:
    """Extract the ``[active]`` token from ``a [b] c`` format.
    Returns empty string if no bracket found — every sysfs
    file in /sys/kernel/mm/transparent_hugepage uses this
    shape, so absence of brackets means we got garbage or
    a CONFIG_TRANSPARENT_HUGEPAGE=n kernel returned empty.
    """
    for token in text.split():
        if token.startswith("[") and token.endswith("]"):
            return token[1:-1]
    return ""


def _read_bracketed(path: Path) -> str:
    try:
        return _parse_bracketed(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _parse_anon_hugepages(text: str) -> int:
    """Extract AnonHugePages kB from /proc/meminfo; -1 on
    absence or parse failure. Same defensive shape as the
    dirty / file_nr cards — partial-availability rendering."""
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key.strip() != "AnonHugePages":
            continue
        tokens = rest.strip().split()
        if not tokens:
            return -1
        try:
            return int(tokens[0])
        except ValueError:
            return -1
    return -1


def _capture(
    *,
    enabled_path: Path = _THP_ENABLED_PATH,
    defrag_path: Path = _THP_DEFRAG_PATH,
    meminfo_path: Path = _MEMINFO_PATH,
) -> _ThpSnapshot:
    enabled = _read_bracketed(enabled_path)
    defrag = _read_bracketed(defrag_path)
    try:
        meminfo_text = meminfo_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        meminfo_text = ""
    anon_hugepages_kb = _parse_anon_hugepages(meminfo_text)

    # available iff at least one source produced something
    # useful. All-empty means non-procfs / non-sysfs (macOS
    # dev) and we render the dedicated unavailable note.
    available = bool(enabled) or bool(defrag) or anon_hugepages_kb >= 0
    return _ThpSnapshot(
        enabled=enabled,
        defrag=defrag,
        anon_hugepages_kb=anon_hugepages_kb,
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


def _fmt_str(value: str) -> str:
    return value if value else "unknown"


def _render(snap: _ThpSnapshot) -> str:
    lines = ["🐘 <b>Transparent Huge Pages</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/sys/kernel/mm/transparent_hugepage/* and "
            "/proc/meminfo unreadable — CONFIG_TRANSPARENT_HUGEPAGE=n "
            "kernel, macOS dev, or non-procfs/sysfs container.</i>"
        )
        return "\n".join(lines)

    warn = snap.under_pressure
    marker = " ⚠" if warn else ""

    lines.append(f"  enabled=<code>{_fmt_str(snap.enabled)}</code>{marker}")
    lines.append(f"  defrag=<code>{_fmt_str(snap.defrag)}</code>")
    lines.append(f"  AnonHugePages=<code>{_fmt_kb(snap.anon_hugepages_kb)}</code>")

    lines.append("")
    if warn:
        lines.append(
            "<i>⚠ <code>transparent_hugepage/enabled=always</code> — "
            "every database project with a position on THP "
            "(PostgreSQL, MongoDB, Redis, MariaDB) recommends "
            "against this mode. khugepaged compaction can stall "
            "syscalls for tens to hundreds of ms while it scans "
            "for 2 MiB-aligned free regions; SQLite's mmap reads "
            "and WAL fsyncs are exactly the shape that triggers "
            "the stall. Surfaces as opaque 100-300ms latency "
            "spikes in handler timings that don't correlate with "
            "load. Fix: <code>echo madvise &gt; "
            "/sys/kernel/mm/transparent_hugepage/enabled</code> "
            "(opt-in via madvise(MADV_HUGEPAGE)), or "
            "<code>never</code> for unconditional safety.</i>"
        )
    else:
        lines.append(
            "<i>No warnings — THP enabled mode is not "
            "<code>always</code>, so khugepaged compaction won't "
            "stall syscalls behind the bot's back. AnonHugePages "
            "shows how much anonymous memory is actually backed "
            "by THP right now — a non-zero value with "
            "enabled=madvise means some allocator opted in "
            "(glibc since 2.x does for arenas above tunable "
            "thresholds).</i>"
        )
    return "\n".join(lines)


async def handle_admin_thp(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_thp; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        enabled=snap.enabled,
        defrag=snap.defrag,
        anon_hugepages_kb=snap.anon_hugepages_kb,
    ).info("/admin_thp rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.thp")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_thp(message, settings)

    router.message.register(_entry, Command("admin_thp", ignore_case=True))
    return router
