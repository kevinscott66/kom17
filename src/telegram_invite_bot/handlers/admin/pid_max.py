"""``/admin_pid_max`` — system-wide PID / task-count vs kernel ceiling.

/admin_loadavg already surfaces the running/total task count
from /proc/loadavg's 4th column. It does NOT surface the
**ceiling**: ``kernel.pid_max`` (the kernel PID number space,
default 32768 on 32-bit, 4194304 on 64-bit) and ``kernel.
threads-max`` (the cap on total clone()-able tasks, computed
at boot from RAM). Once we cross either, ``fork(2)`` /
``clone(2)`` return ``EAGAIN`` — anywhere on the host — and
the bot stops being able to spawn helpers (subprocess for
ffmpeg, asyncio thread-pool growth, etc).

This is a distinct failure mode from /admin_file_nr (Stage 133):
file-nr is about *open fds*, this card is about *spawnable tasks*.
Both are host-scoped sysctls with global EAGAIN/ENFILE blast
radius, and neither is visible from /admin_fdlimit (per-process
RLIMIT_NOFILE) or /admin_limits (per-process rlimits).

Inputs:

* ``/proc/sys/kernel/pid_max`` — PID number-space upper bound.
* ``/proc/sys/kernel/threads-max`` — total-task ceiling.
* ``/proc/loadavg`` field 4 — ``running/total`` — current task
  count. We only consume the ``total`` half here (running is
  /admin_loadavg's job).

⚠ predicate: any of (total / pid_max), (total / threads-max)
>= ``_PID_WARN_RATIO`` (0.8). Same threshold as file-nr and
key_users — operator-intuitive "approaching limit." We check
BOTH ceilings because the binding constraint flips depending
on host tuning: a memory-tight box hits threads-max first
(it's RAM-derived), a heavily containerised box hits pid_max
first (often capped by systemd / container runtime to 1024
per cgroup, even though the kernel-global value is 4M).

Cry-wolf must-not-fire on the canonical-healthy sample where
total << min(pid_max, threads-max).

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


log = logger.bind(component="handlers.admin.pid_max")


_PID_MAX_PATH = Path("/proc/sys/kernel/pid_max")
_THREADS_MAX_PATH = Path("/proc/sys/kernel/threads-max")
_LOADAVG_PATH = Path("/proc/loadavg")


_PID_WARN_RATIO = 0.8


class _PidMaxSnapshot:
    """Captured PID/task ceiling state.

    Each field carries ``-1`` for "couldn't read this source" so
    render surfaces 'unknown' rather than misleading 0%. The
    three sources are independent files — a stripped container
    may have /proc/loadavg but no /proc/sys/kernel/* (or vice
    versa) — and rendering should degrade per-field.
    """

    __slots__ = ("available", "pid_max", "tasks_total", "threads_max")

    def __init__(
        self, *, pid_max: int, threads_max: int, tasks_total: int, available: bool
    ) -> None:
        self.pid_max = pid_max
        self.threads_max = threads_max
        self.tasks_total = tasks_total
        self.available = available

    def _ratio_against(self, ceiling: int) -> float:
        # Defensive: ceiling<=0 or task sentinel returns 0.0
        # ("we don't know, so don't warn") — matches the file_nr
        # posture. The alternative ("treat unknown as full") would
        # fire ⚠ on every macOS-dev / stripped-container run.
        if ceiling <= 0 or self.tasks_total < 0:
            return 0.0
        return self.tasks_total / ceiling

    @property
    def pid_ratio(self) -> float:
        return self._ratio_against(self.pid_max)

    @property
    def thread_ratio(self) -> float:
        return self._ratio_against(self.threads_max)

    @property
    def under_pressure(self) -> bool:
        return self.pid_ratio >= _PID_WARN_RATIO or self.thread_ratio >= _PID_WARN_RATIO


def _read_int(path: Path) -> int:
    """Read a single int from a /proc sysctl file. ``-1`` on any
    failure (file absent, non-numeric, permission denied). The
    caller propagates ``-1`` to the snapshot so render can
    show 'unknown' instead of crashing or misleading."""
    try:
        return int(path.read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return -1


def _parse_loadavg_total(text: str) -> int:
    """Extract ``total`` from /proc/loadavg field 4 (``running/total``).
    Returns ``-1`` on any malformation. The file shape has been
    stable since 2.0, but a defensive parser shields against a
    future kernel adding columns or changing the separator.
    """
    parts = text.split()
    if len(parts) < 4:
        return -1
    field = parts[3]
    if "/" not in field:
        return -1
    _, _, total_token = field.partition("/")
    try:
        return int(total_token)
    except ValueError:
        return -1


def _capture(
    *,
    pid_max_path: Path = _PID_MAX_PATH,
    threads_max_path: Path = _THREADS_MAX_PATH,
    loadavg_path: Path = _LOADAVG_PATH,
) -> _PidMaxSnapshot:
    pid_max = _read_int(pid_max_path)
    threads_max = _read_int(threads_max_path)
    try:
        loadavg_text = loadavg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        loadavg_text = ""
    tasks_total = _parse_loadavg_total(loadavg_text)

    # "available" is True iff at least one of the three sources
    # produced a usable value. All-three-missing means the host
    # is procfs-less (macOS dev) and we should render the
    # unavailable note rather than three "unknown" lines that
    # imply we tried and partially failed.
    available = pid_max >= 0 or threads_max >= 0 or tasks_total >= 0
    return _PidMaxSnapshot(
        pid_max=pid_max,
        threads_max=threads_max,
        tasks_total=tasks_total,
        available=available,
    )


def _fmt(value: int) -> str:
    if value < 0:
        return "unknown"
    return f"{value:,}"


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _render(snap: _PidMaxSnapshot) -> str:
    lines = ["🧬 <b>PID / task-count ceilings</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>None of /proc/sys/kernel/{pid_max,threads-max} "
            "or /proc/loadavg are readable — Linux-only surface "
            "(macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    warn = snap.under_pressure

    lines.append(f"  <b>Current total tasks:</b> <code>{_fmt(snap.tasks_total)}</code>")
    lines.append("")
    lines.append("  <b>Kernel ceilings:</b>")

    pid_marker = " ⚠" if snap.pid_ratio >= _PID_WARN_RATIO else ""
    thr_marker = " ⚠" if snap.thread_ratio >= _PID_WARN_RATIO else ""

    lines.append(
        f"    kernel.pid_max=<code>{_fmt(snap.pid_max)}</code> "
        f"(usage <code>{_fmt_pct(snap.pid_ratio)}</code>){pid_marker}"
    )
    lines.append(
        f"    kernel.threads-max=<code>{_fmt(snap.threads_max)}</code> "
        f"(usage <code>{_fmt_pct(snap.thread_ratio)}</code>){thr_marker}"
    )

    lines.append("")
    if warn:
        lines.append(
            f"<i>⚠ task-count above <code>{int(_PID_WARN_RATIO * 100)}%</code> "
            "of at least one kernel ceiling. fork(2)/clone(2) "
            "return EAGAIN host-wide above the cap — including our "
            "subprocess spawns (ffmpeg, asyncio thread-pool growth, "
            "etc). Distinct from /admin_fdlimit (per-process "
            "RLIMIT_NPROC); this is the system-wide ceiling. Audit "
            "with <code>ps -eLf | wc -l</code> or "
            "<code>find /proc -maxdepth 2 -name task -type d | "
            "wc -l</code>. Raise via "
            "<code>/proc/sys/kernel/pid_max</code> + "
            "<code>threads-max</code> if the host has headroom; "
            "otherwise hunt the leaker.</i>"
        )
    else:
        lines.append(
            f"<i>No warnings — task-count is under "
            f"<code>{int(_PID_WARN_RATIO * 100)}%</code> of both "
            "kernel.pid_max and kernel.threads-max. The binding "
            "constraint flips by host: containers usually hit "
            "pid_max first (often capped per-cgroup), bare-metal "
            "hits threads-max first (RAM-derived). /admin_loadavg "
            "renders the running/total split — this card adds the "
            "ceiling.</i>"
        )
    return "\n".join(lines)


async def handle_admin_pid_max(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_pid_max; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        pid_max=snap.pid_max,
        threads_max=snap.threads_max,
        tasks_total=snap.tasks_total,
    ).info("/admin_pid_max rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.pid_max")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_pid_max(message, settings)

    router.message.register(_entry, Command("admin_pid_max", ignore_case=True))
    return router
