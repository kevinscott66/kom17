"""``/admin_loadavg`` — host load average + run-queue from /proc/loadavg.

``/admin_cpu`` already surfaces load average via :func:`os.getloadavg`,
but as three bare floats. /proc/loadavg carries two additional fields
that the libc wrapper deliberately hides:

* ``running/total`` — number of kernel tasks currently in R-state
  vs total task count. This is the closest legal observation of
  "is the CPU actually saturated, or are tasks blocked waiting
  for I/O?". A 1-minute load of 8 with running=1 means I/O-bound;
  the same load with running=8 means CPU-bound. /admin_cpu can't
  tell the operator which.
* ``last_pid`` — pid of the most-recently-spawned process.
  Sentinel-grade indicator for fork-rate observation: take two
  samples 5 seconds apart and the delta is a hard floor on
  "processes spawned per second", which is the cheapest possible
  fork-bomb / shell-exec-storm detector. We don't sample twice
  here (that would need state across handler calls), but we
  surface the field so an operator who runs the card twice gets
  the delta for free.

Why we still keep /admin_cpu: that card is about scheduling
*posture* (sched_getaffinity, cpu_count, nproc, loadavg headline).
/admin_loadavg is about the *kernel queue state* — same loadavg
numbers, but reinterpreted as "is the run queue actually busy".
Two purpose-built cards beat one unfit-for-either, same logic as
/admin_fdlimit + /admin_limits.

Cry-wolf posture: single ⚠ on ``1-min load / cpu_count > 2.0``.
That's the canonical "the run queue is double the host's
capacity" threshold every monitoring tool uses. We deliberately
do NOT ⚠ on 5-min or 15-min load — short-lived spikes are normal
(a deploy, a backup, a heavy /admin_smaps walk), and ⚠'ing on the
longer windows would surface yesterday's spike instead of
right-now health.

Forward-compat: /proc/loadavg format has been stable since ~2.0
but we tolerate malformed lines by degrading to "n/a" rather than
crashing. CPU count comes from :func:`os.cpu_count` which can
return None on exotic platforms — we render the ratio only when
the denominator is known.

Same posture as every other admin card — silent-drop, private-only,
pure stdlib.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.loadavg")


_LOADAVG_PATH = Path("/proc/loadavg")


# 2.0 × cpu_count is the canonical "run queue is double host
# capacity" threshold every monitoring tool uses. Tighter (1.0)
# misses short bursts; looser (4.0) misses real saturation. Pick
# the well-known number rather than reinventing.
_OVERLOAD_RATIO = 2.0


class _LoadSnapshot:
    """Captured /proc/loadavg reading.

    * ``load_1`` / ``load_5`` / ``load_15`` — three load averages
      as floats, or None if unparseable.
    * ``running`` / ``total`` — task-count snapshot at the instant
      /proc was read. The "/" in the raw file separates them.
    * ``last_pid`` — pid of the most-recently-spawned process;
      sentinel-grade fork-rate signal when sampled twice.
    * ``cpu_count`` — :func:`os.cpu_count`; None on exotic
      platforms. Stored on the snapshot so the render function
      doesn't need a second :mod:`os` call (also lets tests inject).
    * ``available`` — False when /proc/loadavg couldn't be read at
      all (macOS dev, container without /proc).
    """

    __slots__ = (
        "available",
        "cpu_count",
        "last_pid",
        "load_1",
        "load_15",
        "load_5",
        "running",
        "total",
    )

    def __init__(
        self,
        *,
        load_1: float | None,
        load_5: float | None,
        load_15: float | None,
        running: int | None,
        total: int | None,
        last_pid: int | None,
        cpu_count: int | None,
        available: bool,
    ) -> None:
        self.load_1 = load_1
        self.load_5 = load_5
        self.load_15 = load_15
        self.running = running
        self.total = total
        self.last_pid = last_pid
        self.cpu_count = cpu_count
        self.available = available


def _parse_loadavg(
    text: str,
) -> tuple[
    float | None,
    float | None,
    float | None,
    int | None,
    int | None,
    int | None,
]:
    """Parse a single /proc/loadavg line.

    Format: ``load1 load5 load15 running/total last_pid`` (5 fields,
    space-separated; field 4 contains a ``/``). We tolerate missing
    trailing fields and unparseable numbers by returning None — the
    operator can still see which fields did parse.
    """
    line = text.strip()
    if not line:
        return None, None, None, None, None, None
    parts = line.split()

    def _f(idx: int) -> float | None:
        if idx >= len(parts):
            return None
        try:
            return float(parts[idx])
        except ValueError:
            return None

    def _i(idx: int) -> int | None:
        if idx >= len(parts):
            return None
        try:
            return int(parts[idx])
        except ValueError:
            return None

    load_1 = _f(0)
    load_5 = _f(1)
    load_15 = _f(2)
    running: int | None = None
    total: int | None = None
    if len(parts) >= 4 and "/" in parts[3]:
        rs, _, ts = parts[3].partition("/")
        try:
            running = int(rs)
        except ValueError:
            running = None
        try:
            total = int(ts)
        except ValueError:
            total = None
    last_pid = _i(4)
    return load_1, load_5, load_15, running, total, last_pid


def _capture(
    *,
    path: Path = _LOADAVG_PATH,
    cpu_count: int | None = None,
) -> _LoadSnapshot:
    """Read /proc/loadavg + build a snapshot.

    ``cpu_count`` is a keyword override so tests can drive the ⚠
    predicate boundary without depending on the host's CPU count.
    Default is :func:`os.cpu_count`.
    """
    cpus = cpu_count if cpu_count is not None else os.cpu_count()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _LoadSnapshot(
            load_1=None,
            load_5=None,
            load_15=None,
            running=None,
            total=None,
            last_pid=None,
            cpu_count=cpus,
            available=False,
        )
    load_1, load_5, load_15, running, total, last_pid = _parse_loadavg(text)
    return _LoadSnapshot(
        load_1=load_1,
        load_5=load_5,
        load_15=load_15,
        running=running,
        total=total,
        last_pid=last_pid,
        cpu_count=cpus,
        available=True,
    )


def _overloaded(snap: _LoadSnapshot) -> bool:
    """⚠ predicate — 1-min load > 2 × cpu_count.

    Returns False if either field is missing — absence of data is
    NOT a warning, same cry-wolf-prevention posture as every other
    card. Explicitly uses 1-min load only; 5/15-min spikes are
    too stale to warrant a right-now health flag.
    """
    if snap.load_1 is None or snap.cpu_count is None or snap.cpu_count <= 0:
        return False
    return (snap.load_1 / snap.cpu_count) > _OVERLOAD_RATIO


def _fmt_load(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _render(snap: _LoadSnapshot) -> str:
    lines = ["⚖️ <b>Load average + run queue (/proc/loadavg)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/loadavg unavailable on this host — Linux-"
            "only card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    marker = " ⚠" if _overloaded(snap) else ""
    lines.append(f"  <b>1-minute load:</b>  <code>{_fmt_load(snap.load_1)}</code>{marker}")
    lines.append(f"  <b>5-minute load:</b>  <code>{_fmt_load(snap.load_5)}</code>")
    lines.append(f"  <b>15-minute load:</b> <code>{_fmt_load(snap.load_15)}</code>")
    lines.append("")

    if snap.cpu_count is not None and snap.load_1 is not None and snap.cpu_count > 0:
        ratio = snap.load_1 / snap.cpu_count
        lines.append(
            f"  <b>CPU count:</b> <code>{snap.cpu_count}</code> · "
            f"1-min load / cpu = <code>{ratio:.2f}</code>"
        )
    elif snap.cpu_count is not None:
        lines.append(f"  <b>CPU count:</b> <code>{snap.cpu_count}</code>")

    lines.append("")
    running_str = "n/a" if snap.running is None else str(snap.running)
    total_str = "n/a" if snap.total is None else str(snap.total)
    lines.append(f"  <b>Tasks R/total:</b> <code>{running_str}</code> / <code>{total_str}</code>")
    lines.append(
        "      <i>R = currently scheduled or on the run queue. "
        "High load + low R = I/O-bound (not CPU-bound).</i>"
    )

    if snap.last_pid is not None:
        lines.append("")
        lines.append(f"  <b>Last spawned PID:</b> <code>{snap.last_pid}</code>")
        lines.append(
            "      <i>Run the card twice; the delta is a hard floor "
            "on processes spawned per second — cheap fork-storm "
            "detector.</i>"
        )

    lines.append("")
    lines.append(
        "<i>⚠ markers: only when 1-minute load exceeds twice the "
        "host CPU count — the canonical &quot;run queue is double "
        "capacity&quot; threshold. 5-min and 15-min loads are "
        "shown but NOT ⚠'d: yesterday's deploy spike isn't "
        "today's health signal. See /admin_cpu for scheduling "
        "posture (affinity, cpu_count, governor).</i>"
    )
    return "\n".join(lines)


async def handle_admin_loadavg(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_loadavg; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        load_1=snap.load_1,
        cpu_count=snap.cpu_count,
        overloaded=_overloaded(snap),
    ).info("/admin_loadavg rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.loadavg")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_loadavg(message, settings)

    router.message.register(_entry, Command("admin_loadavg", ignore_case=True))
    return router
