"""``/admin_rusage`` — :func:`resource.getrusage` cumulative counters.

Complements /admin_proc (peak RSS + CPU headline) and /admin_memory
(memory anatomy) with the **cumulative kernel-side counters** view:
major/minor page faults, voluntary/involuntary context switches, and
block I/O ops. These are the numbers that distinguish "the bot is
slow because of itself" from "the bot is slow because the kernel
keeps preempting it" or "because every read is hitting disk".

Why an operator wants this:

* "Are we paging from disk?" — ``ru_majflt`` (major page faults)
  counts every page-in from the swap device or backing store. On
  a steady-state warm bot this should be tiny (low thousands at
  most over hours of uptime). A climbing number means the working
  set doesn't fit in RAM and the kernel is pulling pages back in;
  request latency tracks that count linearly.
* "Is something else stealing CPU?" — ``ru_nivcsw`` (involuntary
  context switches) counts kernel preemption: the scheduler kicked
  us off CPU because something else wanted it. A high involuntary
  count alongside a healthy ``ru_nvcsw`` (voluntary, i.e. we
  blocked on IO) means we're contending for CPU even though we
  weren't blocking. Pairs with /admin_cpu's load average — load
  > cpu_count plus rising ``ru_nivcsw`` is the saturated-host
  signal in stereo.
* "Are we doing disk I/O we didn't expect?" — ``ru_inblock`` /
  ``ru_oublock`` are kernel-counted block ops. An aiohttp / DB
  bot should have low ``ru_oublock`` (writes) — anything climbing
  fast means a log sink turned synchronous, a SQLite checkpoint
  is thrashing, or a temp-file cleanup loop is misbehaving.
* "Sanity check the headline" — ``ru_maxrss`` cross-validates
  /admin_proc's peak RSS reading; ``ru_utime`` + ``ru_stime``
  cross-validate cumulative CPU. Two diagnostics agreeing is
  evidence the readings are real; disagreement is itself a bug
  worth knowing.

The :mod:`resource` module is POSIX-only (Linux + macOS); on
Windows the import fails. We guard the import at module-load time
and surface "unavailable" if the module is absent. Mirrors the
posture of /admin_fds / /admin_memory for the non-Linux branch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

# resource is POSIX-only. On Windows the import raises ImportError;
# we catch and let the renderer take the unavailable branch. The
# ``Any``-typed fallback keeps mypy happy on the non-POSIX path
# without forcing every reader of this file to wade through
# platform-specific stubs.
_resource: object | None
try:
    import resource as _resource_module
except ImportError:  # pragma: no cover - macOS / Linux always have it
    _resource = None
else:
    _resource = _resource_module

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.rusage")


class _RUsageSnapshot:
    """Captured getrusage(RUSAGE_SELF) counters.

    Every field maps 1:1 to a ``struct rusage`` member. Times are
    floats (seconds); counts are ints. ``maxrss_unit`` is "kB" on
    Linux and "bytes" on macOS — different platforms report the
    same field in different units (POSIX leaves it implementation-
    defined). We surface the unit explicitly rather than try to
    normalise, because normalising and getting it wrong is worse
    than asking the operator to read the unit suffix.
    """

    __slots__ = (
        "available",
        "inblock",
        "majflt",
        "maxrss",
        "maxrss_unit",
        "minflt",
        "nivcsw",
        "nvcsw",
        "oublock",
        "stime",
        "utime",
    )

    def __init__(
        self,
        *,
        available: bool,
        utime: float = 0.0,
        stime: float = 0.0,
        maxrss: int = 0,
        maxrss_unit: str = "kB",
        minflt: int = 0,
        majflt: int = 0,
        inblock: int = 0,
        oublock: int = 0,
        nvcsw: int = 0,
        nivcsw: int = 0,
    ) -> None:
        self.available = available
        self.utime = utime
        self.stime = stime
        self.maxrss = maxrss
        self.maxrss_unit = maxrss_unit
        self.minflt = minflt
        self.majflt = majflt
        self.inblock = inblock
        self.oublock = oublock
        self.nvcsw = nvcsw
        self.nivcsw = nivcsw


def _detect_maxrss_unit() -> str:
    """Return "kB" on Linux, "bytes" on macOS.

    POSIX leaves the unit of ``ru_maxrss`` implementation-defined.
    Linux reports kilobytes; Darwin (and *BSD) reports bytes. The
    unit difference is a 1024× factor, so guessing wrong would
    silently misreport peak RSS by three orders of magnitude. We
    use :data:`sys.platform` rather than ``platform.system()``
    because the former is what the CPython source itself keys on.
    """
    import sys

    return "bytes" if sys.platform == "darwin" else "kB"


def _capture() -> _RUsageSnapshot:
    """Sample :func:`resource.getrusage`.

    The :mod:`resource` import can fail on Windows — we already
    caught that at module load. A successful import effectively
    guarantees the call works (RUSAGE_SELF is always supported by
    the kernel for the current process), but we still catch OSError
    defensively in case a future seccomp profile blocks it.
    """
    if _resource is None:
        return _RUsageSnapshot(available=False)
    try:
        ru = _resource.getrusage(_resource.RUSAGE_SELF)  # type: ignore[attr-defined]
    except OSError:
        return _RUsageSnapshot(available=False)
    return _RUsageSnapshot(
        available=True,
        utime=float(ru.ru_utime),
        stime=float(ru.ru_stime),
        maxrss=int(ru.ru_maxrss),
        maxrss_unit=_detect_maxrss_unit(),
        minflt=int(ru.ru_minflt),
        majflt=int(ru.ru_majflt),
        inblock=int(ru.ru_inblock),
        oublock=int(ru.ru_oublock),
        nvcsw=int(ru.ru_nvcsw),
        nivcsw=int(ru.ru_nivcsw),
    )


# Threshold for "anything climbing fast" warnings. Cumulative
# counters since process start — what counts as "fast" depends on
# uptime, so the absolute thresholds are deliberately generous and
# the operator's actual triage step is "sample twice, diff". The
# ⚠ markers exist to flag *obviously* high numbers that no healthy
# steady-state would reach. A long-running production bot with a
# benign workload will sit well below these.
_MAJFLT_CONCERNING = 10_000
_NIVCSW_CONCERNING = 1_000_000


def _majflt_concerning(snap: _RUsageSnapshot) -> bool:
    """``True`` if major page faults exceed ``_MAJFLT_CONCERNING``.

    A bot that fits comfortably in RAM and isn't swapping touches
    very few major faults over its lifetime — most page-ins are
    the initial code/data load at start, totalling low thousands.
    The 10k threshold is generous; any production bot hitting it
    is either swapping (cross-check /admin_memory's VmSwap) or the
    working set genuinely doesn't fit (consider more RAM).
    """
    return snap.majflt > _MAJFLT_CONCERNING


def _nivcsw_concerning(snap: _RUsageSnapshot) -> bool:
    """``True`` if involuntary context switches exceed the threshold.

    The scheduler preempts us when it has to schedule something else.
    For a single-process bot on a quiet host this should stay low —
    a million involuntary switches means we're being preempted
    constantly. Pairs with /admin_cpu's load average for the "host
    is saturated" diagnosis.
    """
    return snap.nivcsw > _NIVCSW_CONCERNING


def _fmt_maxrss(snap: _RUsageSnapshot) -> str:
    """Render ``maxrss`` as MiB regardless of platform unit.

    Linux reports kB, macOS bytes — we convert both to MiB at
    render time so the column reads the same on either platform.
    Surface the source unit in italics so an operator running both
    a Linux prod box and a macOS dev laptop can spot which capture
    used which conversion.
    """
    mib = snap.maxrss / (1024 * 1024) if snap.maxrss_unit == "bytes" else snap.maxrss / 1024
    return f"{mib:.1f} MiB <i>(from {snap.maxrss_unit})</i>"


def _render(snap: _RUsageSnapshot) -> str:
    lines = ["📊 <b>getrusage(RUSAGE_SELF)</b>", ""]
    if not snap.available:
        lines.append(
            "  <code>unavailable</code> <i>(resource module not importable — Windows host?)</i>"
        )
        lines.append("")
        lines.append(
            "<i>POSIX-only diagnostic. On Windows use perfmon / "
            "Process Explorer for equivalent counters.</i>"
        )
        return "\n".join(lines)

    lines.append(
        f"  • <b>CPU time:</b> "
        f"<code>{snap.utime:.2f}s</code> user / "
        f"<code>{snap.stime:.2f}s</code> system"
    )
    lines.append(f"  • <b>peak RSS:</b> <code>{_fmt_maxrss(snap)}</code>")
    majflt_marker = " ⚠" if _majflt_concerning(snap) else ""
    lines.append(
        f"  • <b>page faults:</b> "
        f"<code>{snap.minflt}</code> minor / "
        f"<code>{snap.majflt}</code> major{majflt_marker}"
    )
    nivcsw_marker = " ⚠" if _nivcsw_concerning(snap) else ""
    lines.append(
        f"  • <b>context switches:</b> "
        f"<code>{snap.nvcsw}</code> voluntary / "
        f"<code>{snap.nivcsw}</code> involuntary{nivcsw_marker}"
    )
    lines.append(
        f"  • <b>block I/O:</b> <code>{snap.inblock}</code> in / <code>{snap.oublock}</code> out"
    )
    lines.append("")
    lines.append(
        f"<i>⚠ markers: major page faults &gt; {_MAJFLT_CONCERNING} "
        f"(working set spilling to swap / disk — cross-check "
        f"/admin_memory VmSwap), involuntary context switches "
        f"&gt; {_NIVCSW_CONCERNING} (kernel preempting us; pairs "
        f"with /admin_cpu load avg for host-saturation diagnosis). "
        f"All counters cumulative since process start — diff two "
        f"samples to spot rates.</i>"
    )
    return "\n".join(lines)


async def handle_admin_rusage(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_rusage; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_rusage rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.rusage")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_rusage(message, settings)

    router.message.register(_entry, Command("admin_rusage", ignore_case=True))
    return router
