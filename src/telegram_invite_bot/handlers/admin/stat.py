"""``/admin_stat`` — system-wide kernel counters from /proc/stat.

The existing CPU/scheduling surface covers PER-CPU breakdowns
(/admin_cpu), load averages (/admin_loadavg), interrupts
(/admin_interrupts), softirqs (/admin_softirqs) and pressure
(/admin_psi). Each pulls one slice of the kernel scheduler view.
What none of them surface is the **cumulative scalars** at the
top of /proc/stat that aggregate across the whole running
kernel:

* **btime.** Unix epoch seconds at which the kernel booted.
  Differs from /admin_uptime (which renders the *process*
  startup time) — btime is the host's boot, which matters for
  blue/green deploys ("did the kernel actually come up
  recently?") and for cross-referencing with audit logs.
* **processes.** Total ``fork()`` count since boot. A
  monotonically-increasing counter that, divided by uptime,
  gives the long-term process-creation rate. A sudden uptick
  is a fork-bomb fingerprint.
* **ctxt.** Total context switches since boot. Same shape as
  ``processes`` — useful as a denominator for "context
  switches per second" over an interval if the operator
  samples twice.
* **procs_running.** Tasks in R state right now. Roughly
  bounded by ``nr_cpus`` on a healthy system; persistently
  above means scheduler is saturated.
* **procs_blocked.** Tasks in D state (uninterruptible sleep —
  almost always I/O wait, sometimes futex / RCU stalls). Non-
  zero is normal momentarily; sustained ≥ ``_BLOCKED_WARN_THRESHOLD``
  is the single ⚠ predicate. Pairs with /admin_io and
  /admin_psi (which show the same signal from the I/O-pressure
  side) but /proc/stat is the cheapest sample we can take.

⚠ predicate: one. ``procs_blocked >= 5``. Threshold-based not
binary because momentary D-state of one or two tasks is normal
(e.g. fsync mid-flight). Five sustained means the I/O
subsystem is genuinely backed up. Pinned with must-not-fire
test on the canonical-healthy sample.

Per-CPU ``cpu0`` / ``cpu1`` lines are deliberately not surfaced
here — /admin_cpu already renders that view, and re-rendering
would double the card length for no operator benefit. Same for
the ``intr`` and ``softirq`` lines (covered by /admin_interrupts
and /admin_softirqs). We surface only the scalars no other card
exposes.

Format is line-oriented::

    cpu  3357 0 4313 1362393 13455 0 51 1 0 0
    cpu0 ...
    intr 8345881 ...
    ctxt 13458
    btime 1693847562
    processes 23456
    procs_running 1
    procs_blocked 0
    softirq ...

Stable since 2.4. We pick ``ctxt``, ``btime``, ``processes``,
``procs_running``, ``procs_blocked`` and ignore the rest.

Same wiring as every other admin card — silent-drop, private-only,
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


log = logger.bind(component="handlers.admin.stat")


_STAT_PATH = Path("/proc/stat")


# Sustained D-state count above which we surface ⚠. Five is the
# operationally observed inflection — momentary fsync /
# uninterruptible-RCU stalls produce 0–2; a backed-up storage
# subsystem on a busy host trends toward double digits. Tunable
# at module level if a future deployment's normal baseline
# differs.
_BLOCKED_WARN_THRESHOLD = 5


# Keys we surface. Anything else /proc/stat emits (per-cpu lines,
# intr, softirq) is parsed-but-dropped to keep the card focused
# on the scalars no other admin card already covers.
_INTERESTING = frozenset({"ctxt", "btime", "processes", "procs_running", "procs_blocked"})


class _StatSnapshot:
    """Captured /proc/stat (scalar subset).

    Fields stored as ints with ``-1`` sentinel for "key absent
    from this kernel" so render can distinguish "0 blocked"
    from "kernel didn't tell us". ``available`` is False on
    macOS / non-procfs.
    """

    __slots__ = (
        "available",
        "btime",
        "ctxt",
        "procs_blocked",
        "procs_running",
        "processes",
    )

    def __init__(
        self,
        *,
        ctxt: int,
        btime: int,
        processes: int,
        procs_running: int,
        procs_blocked: int,
        available: bool,
    ) -> None:
        self.ctxt = ctxt
        self.btime = btime
        self.processes = processes
        self.procs_running = procs_running
        self.procs_blocked = procs_blocked
        self.available = available

    @property
    def blocked_warn(self) -> bool:
        return self.procs_blocked >= _BLOCKED_WARN_THRESHOLD


def _parse_stat(text: str) -> dict[str, int]:
    """Parse /proc/stat into a scalar key:int dict.

    Only keys in ``_INTERESTING`` are kept. Lines whose value is
    non-integer drop defensively — a future kernel adding a new
    field shape under one of our keys shouldn't crash the card.
    """
    out: dict[str, int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        key = parts[0]
        if key not in _INTERESTING:
            continue
        try:
            out[key] = int(parts[1])
        except ValueError:
            continue
    return out


def _capture(*, path: Path = _STAT_PATH) -> _StatSnapshot:
    """Read /proc/stat + build snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _StatSnapshot(
            ctxt=-1,
            btime=-1,
            processes=-1,
            procs_running=-1,
            procs_blocked=-1,
            available=False,
        )
    parsed = _parse_stat(text)
    return _StatSnapshot(
        ctxt=parsed.get("ctxt", -1),
        btime=parsed.get("btime", -1),
        processes=parsed.get("processes", -1),
        procs_running=parsed.get("procs_running", -1),
        procs_blocked=parsed.get("procs_blocked", -1),
        available=True,
    )


def _fmt(value: int) -> str:
    """Render an int with thousands separators; sentinel -1 →
    'unknown'."""
    if value < 0:
        return "unknown"
    return f"{value:,}"


def _render(snap: _StatSnapshot) -> str:
    lines = ["📊 <b>System counters (/proc/stat)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/stat unavailable — Linux-only surface "
            "(macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    warn = snap.blocked_warn
    warn_marker = " ⚠" if warn else ""

    lines.append("  <b>Boot:</b>")
    lines.append(f"    btime=<code>{_fmt(snap.btime)}</code> (epoch seconds)")
    lines.append("")
    lines.append("  <b>Cumulative since boot:</b>")
    lines.append(f"    processes (forks)=<code>{_fmt(snap.processes)}</code>")
    lines.append(f"    ctxt (context switches)=<code>{_fmt(snap.ctxt)}</code>")
    lines.append("")
    lines.append(f"  <b>Sample-time scheduler state:</b>{warn_marker}")
    lines.append(f"    procs_running=<code>{_fmt(snap.procs_running)}</code>")
    lines.append(f"    procs_blocked=<code>{_fmt(snap.procs_blocked)}</code>{' ⚠' if warn else ''}")

    lines.append("")
    if warn:
        lines.append(
            f"<i>⚠ procs_blocked ≥ <code>{_BLOCKED_WARN_THRESHOLD}</code> — "
            "multiple tasks are stuck in D state (uninterruptible sleep, "
            "almost always I/O wait, sometimes RCU / futex). Cross-check "
            "with /admin_io (per-device queue depth), /admin_psi (I/O "
            "pressure stall), and /admin_diskstats (in-flight count). "
            "On a well-running host this value is 0–2 transiently and "
            "never sustained.</i>"
        )
    else:
        lines.append(
            "<i>No warnings — scheduler is not backed up on D-state "
            "tasks. <code>procs_running</code> bounded by core count is "
            "healthy; sustained values above that indicate scheduler "
            "saturation (no warning for it because the bound varies per "
            "host — compare with /admin_cpu's logical-core count).</i>"
        )
    return "\n".join(lines)


async def handle_admin_stat(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_stat; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        procs_blocked=snap.procs_blocked,
        procs_running=snap.procs_running,
        processes=snap.processes,
    ).info("/admin_stat rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.stat")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_stat(message, settings)

    router.message.register(_entry, Command("admin_stat", ignore_case=True))
    return router
