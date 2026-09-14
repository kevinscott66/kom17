"""``/admin_oom`` — kernel OOM-killer posture for this process.

Complements /admin_memory (process-side RSS/VMS view) and
/admin_netconns (kernel-side socket view) by surfacing the
**kernel's view of THIS process's OOM-kill priority**: the
current oom_score, the operator-configurable oom_score_adj, and
the system-wide overcommit / panic-on-oom knobs that govern
whether an OOM event even happens.

Why an operator wants this:

* "Why does the bot mysteriously die under load?" — the OOM
  killer is silent unless you `dmesg` at the right moment, and
  systemd restart obscures the cause. ``oom_score`` rises with
  RSS (roughly: bigger process = bigger target). On a host
  shared with other services, if the bot's score is the highest
  of any user-space process, it's the first to go.
* ``oom_score_adj`` is the operator's only direct control: ``-1000``
  exempts the process entirely, ``+1000`` makes it the first to
  die. Default is ``0``. A positive value is almost always a
  misconfiguration — surfacing it as ⚠ catches "we configured the
  bot to be killed first as a test, then forgot" regressions.
* ``vm.overcommit_memory`` governs whether ``mmap(2)`` lies about
  available memory. Mode 0 (heuristic) is the default but can
  surprise long-lived processes; mode 1 (always) means an OOM is
  the only way the bot can find out it's out of memory; mode 2
  (strict, with ratio) bounds the failure mode but can starve
  allocations early. The card surfaces the value + ratio so the
  operator knows which regime they're in.
* ``vm.panic_on_oom`` is the doomsday switch: if 1, the entire
  host reboots on any OOM. On shared hosts the operator MUST
  know this — a single OOM event takes everything down, not just
  the bot. ⚠ when set.

Posture: silent-drop for non-devs, private-only at the router
level. Linux-only — non-Linux hosts render informational rather
than ⚠. Three small ``/proc`` reads, single-digit-ms.
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


log = logger.bind(component="handlers.admin.oom")


# /proc paths exposed as constants so the test suite can swap them
# for fixture files. Production always reads the kernel-real ones.
_PROC_OOM_SCORE = Path("/proc/self/oom_score")
_PROC_OOM_SCORE_ADJ = Path("/proc/self/oom_score_adj")
_PROC_OVERCOMMIT_MEMORY = Path("/proc/sys/vm/overcommit_memory")
_PROC_OVERCOMMIT_RATIO = Path("/proc/sys/vm/overcommit_ratio")
_PROC_PANIC_ON_OOM = Path("/proc/sys/vm/panic_on_oom")


# oom_score_adj > 0 means the bot is preferentially killed. A
# value of 0 is the default and benign; negative values protect
# the bot at the expense of other processes (the operator's
# explicit call to make, but never ⚠'d — that's a deliberate
# choice). Positive values are almost always a stale "we set this
# to test the OOM path" regression.
_ADJ_CONCERNING_THRESHOLD = 0


# Human-readable labels for /proc/sys/vm/overcommit_memory values.
# The kernel ABI is documented in Documentation/admin-guide/sysctl/vm.rst.
_OVERCOMMIT_LABELS: dict[int, str] = {
    0: "heuristic (default)",
    1: "always (no checks)",
    2: "strict (bounded by ratio)",
}


class _OomSnapshot:
    """Captured OOM-killer posture.

    Every field is tri-state: int when the corresponding /proc
    entry was read + parsed; None when the file was missing or
    unparseable. Render branches on None to distinguish "non-Linux
    host" / "unreadable" from real readings.
    """

    __slots__ = (
        "oom_score",
        "oom_score_adj",
        "overcommit_memory",
        "overcommit_ratio",
        "panic_on_oom",
    )

    def __init__(
        self,
        *,
        oom_score: int | None,
        oom_score_adj: int | None,
        overcommit_memory: int | None,
        overcommit_ratio: int | None,
        panic_on_oom: int | None,
    ) -> None:
        self.oom_score = oom_score
        self.oom_score_adj = oom_score_adj
        self.overcommit_memory = overcommit_memory
        self.overcommit_ratio = overcommit_ratio
        self.panic_on_oom = panic_on_oom


def _read_int(path: Path) -> int | None:
    """Read a one-line integer from a /proc file. Returns None on
    any failure mode (missing, OSError, unparseable). The render
    distinguishes None from 0 — `0` is the kernel saying "fine",
    `None` is "we can't tell".
    """
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _capture(
    *,
    oom_score_path: Path = _PROC_OOM_SCORE,
    oom_score_adj_path: Path = _PROC_OOM_SCORE_ADJ,
    overcommit_memory_path: Path = _PROC_OVERCOMMIT_MEMORY,
    overcommit_ratio_path: Path = _PROC_OVERCOMMIT_RATIO,
    panic_on_oom_path: Path = _PROC_PANIC_ON_OOM,
) -> _OomSnapshot:
    return _OomSnapshot(
        oom_score=_read_int(oom_score_path),
        oom_score_adj=_read_int(oom_score_adj_path),
        overcommit_memory=_read_int(overcommit_memory_path),
        overcommit_ratio=_read_int(overcommit_ratio_path),
        panic_on_oom=_read_int(panic_on_oom_path),
    )


def _adj_concerning(adj: int | None) -> bool:
    """⚠ only on positive adj — negative is operator intent (protect
    this process), zero is the default. None never concerning (we
    don't claim "bad" without data). Same cry-wolf posture as every
    other admin card."""
    return adj is not None and adj > _ADJ_CONCERNING_THRESHOLD


def _panic_concerning(panic: int | None) -> bool:
    """⚠ when the kernel will panic on any OOM. The operator needs
    to know — on shared hosts a single OOM takes everything down."""
    return panic is not None and panic != 0


def _render(snap: _OomSnapshot) -> str:
    lines = ["💀 <b>OOM-killer posture</b>", ""]

    lines.append("  <b>this process:</b>")
    if snap.oom_score is None:
        lines.append(
            "    • <i>/proc/self/oom_score not readable "
            "(non-Linux host or restricted namespace)</i>"
        )
    else:
        lines.append(
            f"    • <code>oom_score = {snap.oom_score}</code> "
            f"<i>(higher = bigger kill target; rises with RSS)</i>"
        )
    if snap.oom_score_adj is None:
        lines.append("    • <i>/proc/self/oom_score_adj not readable</i>")
    else:
        warn = " ⚠" if _adj_concerning(snap.oom_score_adj) else ""
        lines.append(
            f"    • <code>oom_score_adj = {snap.oom_score_adj}</code> "
            f"<i>(-1000 exempts, +1000 first to die)</i>{warn}"
        )

    lines.append("")
    lines.append("  <b>system policy:</b>")
    if snap.overcommit_memory is None:
        lines.append("    • <i>/proc/sys/vm/overcommit_memory not readable</i>")
    else:
        label = _OVERCOMMIT_LABELS.get(snap.overcommit_memory, "unknown mode")
        lines.append(
            f"    • <code>overcommit_memory = {snap.overcommit_memory}</code> <i>({label})</i>"
        )
    if snap.overcommit_ratio is None:
        lines.append("    • <i>/proc/sys/vm/overcommit_ratio not readable</i>")
    else:
        # Only meaningful in mode 2; we surface it always because
        # the operator's mental model is "this is the cap", but
        # note that it's ignored in modes 0/1.
        note = (
            "% of RAM + swap allowed (mode 2 only)"
            if snap.overcommit_memory == 2
            else "% (currently ignored — mode 0/1)"
        )
        lines.append(
            f"    • <code>overcommit_ratio = {snap.overcommit_ratio}</code> <i>({note})</i>"
        )
    if snap.panic_on_oom is None:
        lines.append("    • <i>/proc/sys/vm/panic_on_oom not readable</i>")
    else:
        warn = " ⚠" if _panic_concerning(snap.panic_on_oom) else ""
        lines.append(
            f"    • <code>panic_on_oom = {snap.panic_on_oom}</code> "
            f"<i>(0 = kill offender; 1 = kernel panic / reboot)</i>{warn}"
        )

    lines.append("")
    lines.append(
        "<i>⚠ markers: <code>oom_score_adj &gt; 0</code> (bot is "
        "preferentially killed — typical cause is a stale test "
        "tweak; reset via <code>systemctl set-property "
        "telegram-bot.service OOMScoreAdjust=0</code> or the unit "
        "file), or <code>panic_on_oom != 0</code> (any OOM reboots "
        "the host — on shared hosts this is a much bigger blast "
        "radius than the operator usually expects).</i>"
    )
    return "\n".join(lines)


async def handle_admin_oom(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_oom; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        oom_score=snap.oom_score,
        oom_score_adj=snap.oom_score_adj,
        overcommit_memory=snap.overcommit_memory,
        panic_on_oom=snap.panic_on_oom,
    ).info("/admin_oom rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.oom")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_oom(message, settings)

    router.message.register(_entry, Command("admin_oom", ignore_case=True))
    return router
