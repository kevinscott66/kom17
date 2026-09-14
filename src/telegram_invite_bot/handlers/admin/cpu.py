"""``/admin_cpu`` — CPU capacity + load + scheduling-affinity snapshot.

Complements /admin_proc (process CPU consumption) and /admin_fdlimit
(OS resource limits) with the **upstream** view: how many cores the
interpreter can see, how loaded the host is right now, and whether
scheduling affinity has been pinned narrower than the host.

Why an operator wants this:

* "Why is asyncio scheduling feeling slow?" — ``os.cpu_count()``
  reports the build-time count of logical cores; ``len(os.sched_-
  getaffinity(0))`` is the *runtime* count this process is allowed
  to use. The two diverge when a systemd unit ships with
  ``CPUAffinity=`` or a container with ``--cpuset-cpus``. On a
  16-core box the bot pinned to one core by accident is the
  difference between "comfortable" and "queue keeps growing".
* "Is the host under load?" — :func:`os.getloadavg` returns the 1/5/
  15-minute Unix load averages. A load > cpu_count sustained over
  the 5-min window means processes are queued waiting for CPU; the
  bot's latency budget is the first thing to suffer. We surface
  the ratio rather than just the raw numbers so the card answers
  "is the host saturated?" without the operator doing the division.
* "Did the deploy lose hyperthreading?" — comparing
  ``os.cpu_count()`` (logical) against
  ``len(os.sched_getaffinity(0))`` catches a kernel boot-param or
  cgroup change that silently cut the available core count in
  half — a common cause for the bot suddenly maxing out CPU on
  what looks like the same hardware.

Silent-drop for non-devs, private-only at the router level. Same
posture as every other ``/admin_*``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.cpu")


class _CPUSnapshot:
    """One-shot capture of CPU capacity + load + affinity.

    Every field is computed at capture time so the renderer can stay
    pure (no clock-state, no syscalls). ``loadavg`` and ``affinity``
    are :class:`tuple` to make accidental mutation a type error —
    the row is read-only by intent.
    """

    __slots__ = ("affinity", "cpu_count", "loadavg", "loadavg_available")

    def __init__(
        self,
        *,
        cpu_count: int | None,
        affinity: tuple[int, ...] | None,
        loadavg: tuple[float, float, float] | None,
        loadavg_available: bool,
    ) -> None:
        self.cpu_count = cpu_count
        self.affinity = affinity
        self.loadavg = loadavg
        self.loadavg_available = loadavg_available


def _capture() -> _CPUSnapshot:
    """Sample CPU state. Defensive on every syscall.

    * :func:`os.cpu_count` returns ``None`` if the count can't be
      determined (rare — embedded targets, exotic schedulers). We
      surface ``None`` faithfully rather than fall back to ``1``;
      a fake ``1`` would lie about the host shape.
    * :func:`os.sched_getaffinity` is Linux-only. On macOS / Windows
      the call raises ``AttributeError`` (no such function) at
      attribute-access time, so we ``getattr``-guard the lookup.
    * :func:`os.getloadavg` raises :exc:`OSError` if the load
      average is unavailable (some sandboxed environments). We
      catch and mark ``loadavg_available=False`` so the renderer
      can say "unavailable" instead of dropping the row.
    """
    cpu_count = os.cpu_count()

    affinity: tuple[int, ...] | None
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is None:
        affinity = None
    else:
        try:
            affinity = tuple(sorted(sched_getaffinity(0)))
        except OSError:
            # Linux returns the set; ``OSError`` would mean the pid
            # 0 trick failed (extremely unusual). Fall back to
            # unavailable rather than crash the card.
            affinity = None

    loadavg: tuple[float, float, float] | None
    try:
        one, five, fifteen = os.getloadavg()
        loadavg = (one, five, fifteen)
        loadavg_available = True
    except OSError:
        loadavg = None
        loadavg_available = False

    return _CPUSnapshot(
        cpu_count=cpu_count,
        affinity=affinity,
        loadavg=loadavg,
        loadavg_available=loadavg_available,
    )


def _affinity_narrowed(snap: _CPUSnapshot) -> bool:
    """``True`` if scheduling affinity is narrower than cpu_count.

    The "deploy lost cores" failure mode the module docstring
    documents: ``CPUAffinity=0`` in a systemd unit, ``--cpuset-cpus=0``
    on a container, kernel ``isolcpus=`` boot param. All three leave
    ``cpu_count`` reporting the full host while ``sched_getaffinity``
    reports the narrower set. If either is unknown we conservatively
    return ``False`` — we can't claim "narrowed" without both numbers.
    """
    if snap.cpu_count is None or snap.affinity is None:
        return False
    return len(snap.affinity) < snap.cpu_count


def _loadavg_saturated(snap: _CPUSnapshot) -> bool:
    """``True`` if the 5-minute load average exceeds cpu_count.

    1-min load is too noisy — a single ``find /`` can spike it on
    a quiet host. 15-min is too slow — a real saturation event
    that started 5 minutes ago wouldn't show up yet. The 5-minute
    window is the standard "is this sustained?" signal and is what
    most monitoring dashboards (Netdata, Grafana, Datadog) display
    as the load badge.

    Compared against ``cpu_count`` (not ``len(affinity)``) on
    purpose: load average is a host-level metric and a host-level
    saturation is what the operator wants to spot, even if our
    process can only use a subset of the cores.
    """
    if snap.loadavg is None or snap.cpu_count is None or snap.cpu_count <= 0:
        return False
    _one, five, _fifteen = snap.loadavg
    return five > snap.cpu_count


def _render(snap: _CPUSnapshot) -> str:
    lines = ["🧮 <b>CPU capacity</b>", ""]

    cpu_str = str(snap.cpu_count) if snap.cpu_count is not None else "unknown"
    lines.append(f"  • <b>logical cores:</b> <code>{cpu_str}</code>")

    if snap.affinity is None:
        lines.append("  • <b>scheduling affinity:</b> <code>unavailable</code> <i>(non-Linux)</i>")
    else:
        marker = " ⚠" if _affinity_narrowed(snap) else ""
        # The affinity set can be sparse — render the count first
        # (the operationally-relevant number) and only spell out
        # the CPU indices if there are 8 or fewer. On a 64-core
        # box the full list would blow the card past Telegram's
        # 4096-char limit.
        cpus_repr = (
            ", ".join(str(c) for c in snap.affinity)
            if len(snap.affinity) <= 8
            else f"{len(snap.affinity)} cpus"
        )
        lines.append(
            f"  • <b>scheduling affinity:</b> "
            f"<code>{len(snap.affinity)}</code> "
            f"<i>({cpus_repr})</i>{marker}"
        )

    if not snap.loadavg_available or snap.loadavg is None:
        lines.append("  • <b>load average:</b> <code>unavailable</code>")
    else:
        one, five, fifteen = snap.loadavg
        marker = " ⚠" if _loadavg_saturated(snap) else ""
        lines.append(
            f"  • <b>load average (1m/5m/15m):</b> "
            f"<code>{one:.2f}</code> / <code>{five:.2f}</code> / "
            f"<code>{fifteen:.2f}</code>{marker}"
        )

    lines.append("")
    lines.append(
        "<i>⚠ markers: scheduling affinity narrower than cpu_count "
        "(deploy/cgroup pinned the process to a subset), or 5-min "
        "load average above cpu_count (host CPU-saturated; latency "
        "budget at risk).</i>"
    )
    return "\n".join(lines)


async def handle_admin_cpu(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_cpu; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_cpu rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.cpu")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_cpu(message, settings)

    router.message.register(_entry, Command("admin_cpu", ignore_case=True))
    return router
