"""``/admin_proc`` — process-level resource snapshot.

Complements /admin_engines (pool-level) and /admin_disk (volume-
level) by surfacing the **process** as the OS sees it: PID, peak
resident memory, accumulated user/system CPU time. The diagnostic
gap this closes is "is the process itself fat or hot?" — the
question /admin_engines and /admin_tasks together leave open. A
healthy bot's RSS plateaus within minutes of boot; a leak shows as
RSS climbing across snapshots an hour apart, without any
corresponding /admin_engines pool growth.

Why an operator wants this:

* Memory-leak detection. Compare RSS between two snapshots a few
  minutes apart. A linear climb on otherwise-idle traffic is a
  leak; flat is healthy. This is the cheapest detection short of
  attaching tracemalloc — and the only one available from inside
  Telegram.
* CPU-hog detection. ``ru_utime`` ÷ wall-elapsed approximates the
  process's CPU utilisation. An asyncio-driven bot should sit at
  single-digit-% on healthy traffic; a runaway tight loop in a
  handler pushes it toward 100%. The card lets an operator
  notice the symptom without ``top`` access.
* Crash-loop confirmation. PID changes between two invocations
  mean the process restarted — useful triangulation against
  /admin_uptime when an operator suspects systemd's restart-on-
  fail policy is masking a real crash.

Uses :mod:`resource` (stdlib, Linux + macOS). ``ru_maxrss`` is
KiB on Linux, **bytes** on macOS — the renderer normalises both
to MiB so an operator switching between dev (macOS) and prod
(Linux) reads the same number. The legend documents the platform
quirk so a surprised operator can verify the math.

Pure stdlib, no DB, no IO. Same posture as every other ``/admin_*``:
silent-drop for non-devs, private-only (PID + memory profile are
operator-only context).
"""

from __future__ import annotations

import os
import resource
import sys
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.proc")


class _ProcSnapshot:
    """One read of the process's resource state.

    All four fields captured back-to-back. Plain attribute container
    — same rationale as other admin cards: a single struct with one
    read site doesn't earn the dataclass ceremony, and ``__slots__``
    keeps the per-snapshot allocation tight (the card may be invoked
    in a tight loop during a triage session).
    """

    __slots__ = ("pid", "max_rss_bytes", "user_cpu_s", "system_cpu_s")

    def __init__(
        self,
        *,
        pid: int,
        max_rss_bytes: int,
        user_cpu_s: float,
        system_cpu_s: float,
    ) -> None:
        self.pid = pid
        self.max_rss_bytes = max_rss_bytes
        self.user_cpu_s = user_cpu_s
        self.system_cpu_s = system_cpu_s


def _maxrss_to_bytes(ru_maxrss: int) -> int:
    """Normalise :attr:`resource.struct_rusage.ru_maxrss` to bytes.

    macOS reports ``ru_maxrss`` in **bytes**; Linux reports it in
    **KiB**. The cross-platform note in :mod:`resource`'s own
    docs confirms this. Switching a dev (macOS) and prod (Linux)
    operator between snapshots without normalisation would
    silently show a 1024× difference and burn an hour of triage
    time chasing a "leak" that's only the units.
    """
    if sys.platform == "darwin":
        return int(ru_maxrss)
    return int(ru_maxrss) * 1024


def _capture() -> _ProcSnapshot:
    """Snapshot process resource use.

    ``RUSAGE_SELF`` covers the calling process only, not children —
    we have no children to worry about (no subprocesses spawned in
    this codebase), so SELF is the right scope.
    """
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return _ProcSnapshot(
        pid=os.getpid(),
        max_rss_bytes=_maxrss_to_bytes(ru.ru_maxrss),
        user_cpu_s=float(ru.ru_utime),
        system_cpu_s=float(ru.ru_stime),
    )


def _fmt_mib(n_bytes: int) -> str:
    """Bytes → MiB with two decimals.

    MiB is the right unit for a long-running bot: kilobytes are
    too noisy (every allocation moves the last digit), gigabytes
    are too coarse (a 100 MiB leak invisible at GiB resolution).
    """
    return f"{n_bytes / (1024 * 1024):.2f} MiB"


def _render(snap: _ProcSnapshot) -> str:
    lines = ["🧠 <b>Process resources</b>", ""]
    lines.append(f"• PID: <code>{snap.pid}</code>")
    lines.append(f"• Peak RSS: <code>{_fmt_mib(snap.max_rss_bytes)}</code>")
    # Render both CPU buckets — they signal different problems:
    # user-CPU spike means a busy handler loop, system-CPU spike
    # means a syscall-heavy path (disk I/O, dns). Single combined
    # number would obscure the difference.
    lines.append(f"• User CPU: <code>{snap.user_cpu_s:.3f}s</code>")
    lines.append(f"• System CPU: <code>{snap.system_cpu_s:.3f}s</code>")
    lines.append("")
    lines.append(
        "<i>Peak RSS is high-water — never decreases for the life "
        "of the process. Compare across snapshots: linear climb on "
        "idle traffic ⇒ leak. CPU values are cumulative since "
        "boot; divide by /admin_uptime elapsed for utilisation.</i>"
    )
    return "\n".join(lines)


async def handle_admin_proc(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_proc; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id, pid=snap.pid).info("/admin_proc rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.proc")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_proc(message, settings)

    router.message.register(_entry, Command("admin_proc", ignore_case=True))
    return router
