"""``/admin_limits`` — full ``ulimit -a`` equivalent.

/admin_fdlimit surfaces the three rlimits operators care about
for capacity planning (NOFILE / AS / RSS) — deliberately curated
to fit a single mental model. /admin_limits is the FULL table —
every RLIMIT_* the platform exposes — for the rarer but real
cases where one of the other limits actually fires (RLIMIT_NPROC
on a thread-leaking bot, RLIMIT_FSIZE on a runaway log file,
RLIMIT_STACK on a deep-recursion path).

Why both cards:

* /admin_fdlimit answers "is the bot about to run out of file
  descriptors / address space?" — the day-to-day capacity-
  planning question. Three rows, terse.
* /admin_limits answers "ulimit -a, please" — the exhaustive
  question, asked once per incident when the operator hits an
  unfamiliar errno (EFBIG, EMFILE-not-from-sockets, ENOMEM
  from RLIMIT_AS, etc.). Twelve+ rows, each annotated with
  the failure mode it gates.
* Splitting prevents either card from being unfit for purpose:
  /admin_fdlimit stays scannable; /admin_limits stays
  comprehensive. The duplication of NOFILE/AS/RSS rows across
  the two is intentional — the operator's eye finds the rlimit
  they want in whichever card they're already looking at.

Cry-wolf posture: NO ⚠ on any limit. A configured limit is
operator intent (the systemd unit or container declared this
ceiling); we're surfacing the value, not judging it. The single
exception that would have justified a ⚠ — the soft > hard
inversion — is impossible because the kernel enforces soft ≤ hard
at setrlimit time. So this card is pure information.

Pure stdlib (:mod:`resource`); same posture as every other admin
card — silent-drop, private-only.
"""

from __future__ import annotations

import resource
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.limits")


# (display_label, RLIMIT_* attribute name, one-liner failure mode).
# We resolve via getattr at runtime — some rlimits are Linux-only
# (RLIMIT_MSGQUEUE, RLIMIT_RTPRIO, RLIMIT_RTTIME, RLIMIT_SIGPENDING,
# RLIMIT_NICE) and macOS will skip them gracefully.
#
# Order is loosely "capacity-planning first, niche last" — same
# ergonomic principle as /admin_fdlimit, just expanded.
_RLIMITS: tuple[tuple[str, str, str], ...] = (
    (
        "Open files (NOFILE)",
        "RLIMIT_NOFILE",
        "EMFILE — accept / open / connect refused",
    ),
    (
        "Address space (AS)",
        "RLIMIT_AS",
        "ENOMEM — virtual memory cap (mmap / brk fail)",
    ),
    (
        "Resident set (RSS)",
        "RLIMIT_RSS",
        "no enforcement on modern Linux — historical only",
    ),
    (
        "Processes / threads (NPROC)",
        "RLIMIT_NPROC",
        "EAGAIN on fork / pthread_create — thread-leak signal",
    ),
    (
        "File size (FSIZE)",
        "RLIMIT_FSIZE",
        "EFBIG — single-file write cap (runaway logs)",
    ),
    (
        "CPU time (CPU)",
        "RLIMIT_CPU",
        "SIGXCPU after soft, SIGKILL after hard — runaway-loop guard",
    ),
    (
        "Stack size (STACK)",
        "RLIMIT_STACK",
        "SIGSEGV on overflow — deep-recursion ceiling",
    ),
    (
        "Core dump (CORE)",
        "RLIMIT_CORE",
        "core file size cap — 0 disables dumps",
    ),
    (
        "Data segment (DATA)",
        "RLIMIT_DATA",
        "ENOMEM on brk — heap ceiling (rarely set)",
    ),
    (
        "Locks (LOCKS)",
        "RLIMIT_LOCKS",
        "deprecated on Linux 2.4+; flock cap",
    ),
    (
        "Locked memory (MEMLOCK)",
        "RLIMIT_MEMLOCK",
        "EAGAIN on mlock — pinned-page cap",
    ),
    (
        "POSIX msg queues (MSGQUEUE)",
        "RLIMIT_MSGQUEUE",
        "EAGAIN on mq_open — queue-bytes cap",
    ),
    (
        "Nice priority (NICE)",
        "RLIMIT_NICE",
        "ceiling on setpriority() upward bumps",
    ),
    (
        "RT priority (RTPRIO)",
        "RLIMIT_RTPRIO",
        "EPERM on sched_setscheduler real-time class",
    ),
    (
        "RT CPU time (RTTIME)",
        "RLIMIT_RTTIME",
        "SIGXCPU on RT-scheduled hot-loop microseconds",
    ),
    (
        "Pending signals (SIGPENDING)",
        "RLIMIT_SIGPENDING",
        "EAGAIN on sigqueue — real-time signal flood guard",
    ),
)


class _LimitRow:
    """One rlimit's (soft, hard) reading.

    ``available`` is False when the platform doesn't expose this
    rlimit. Render those explicitly as "n/a" rather than skip —
    the operator should see the absence, not be left wondering
    whether the read failed silently.
    """

    __slots__ = ("available", "hard", "label", "note", "soft")

    def __init__(
        self,
        *,
        label: str,
        note: str,
        soft: int | None,
        hard: int | None,
        available: bool,
    ) -> None:
        self.label = label
        self.note = note
        self.soft = soft
        self.hard = hard
        self.available = available


def _capture() -> tuple[_LimitRow, ...]:
    rows: list[_LimitRow] = []
    for label, attr, note in _RLIMITS:
        const = getattr(resource, attr, None)
        if const is None:
            rows.append(
                _LimitRow(
                    label=label,
                    note=note,
                    soft=None,
                    hard=None,
                    available=False,
                )
            )
            continue
        try:
            soft, hard = resource.getrlimit(const)
        except (OSError, ValueError):
            # ValueError can happen if the kernel exposes a const
            # the libc resource binding doesn't accept — degrade
            # rather than crash, same posture as every other card.
            rows.append(
                _LimitRow(
                    label=label,
                    note=note,
                    soft=None,
                    hard=None,
                    available=False,
                )
            )
            continue
        rows.append(
            _LimitRow(
                label=label,
                note=note,
                soft=soft,
                hard=hard,
                available=True,
            )
        )
    return tuple(rows)


def _fmt_value(value: int | None) -> str:
    """``resource.RLIM_INFINITY`` is the sentinel for "no limit".
    Render as "unlimited" — the operator's mental model. None
    means platform doesn't expose the rlimit at all → "n/a"."""
    if value is None:
        return "n/a"
    if value == resource.RLIM_INFINITY:
        return "unlimited"
    return f"{value:,}"


def _render(rows: tuple[_LimitRow, ...]) -> str:
    lines = ["📊 <b>Resource limits (full ulimit -a)</b>", ""]
    lines.append(
        "<i>Full RLIMIT_* table. See /admin_fdlimit for the curated "
        "capacity-planning subset (NOFILE / AS / RSS only).</i>"
    )
    lines.append("")
    for row in rows:
        soft = _fmt_value(row.soft)
        hard = _fmt_value(row.hard)
        if not row.available:
            lines.append(f"  • <b>{row.label}</b>: <i>n/a on this platform</i>")
        else:
            lines.append(
                f"  • <b>{row.label}</b>: soft=<code>{soft}</code> hard=<code>{hard}</code>"
            )
        lines.append(f"      <i>{row.note}</i>")
    lines.append("")
    lines.append(
        "<i>No ⚠ markers on this card by design — a configured "
        "limit is operator intent (systemd unit / container "
        "policy), not a problem. /admin_engines + /admin_fdlimit "
        "is the place to spot growth-toward-ceiling.</i>"
    )
    return "\n".join(lines)


async def handle_admin_limits(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_limits; silently dropped"
        )
        return
    rows = _capture()
    await message.answer(_render(rows))
    log.bind(
        user_id=user.id,
        rlimit_count=sum(1 for r in rows if r.available),
    ).info("/admin_limits rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.limits")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_limits(message, settings)

    router.message.register(_entry, Command("admin_limits", ignore_case=True))
    return router
