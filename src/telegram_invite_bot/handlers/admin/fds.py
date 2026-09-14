"""``/admin_fds`` — open-file-descriptor census (count + by-kind tally).

Complements /admin_fdlimit (the soft/hard NOFILE rlimit) with the
**current consumption** view: how many descriptors are open right
now, and what kind. /admin_fdlimit answers "what is the ceiling";
this one answers "how close are we to it, and where are we leaking
if we are".

Why an operator wants this:

* "Why are we hitting EMFILE / 'Too many open files'?" — the
  rlimit is one half of the equation, the actual count is the
  other. A bot leaking unclosed aiohttp connections, SQLite
  cursors, or PIL.Image handles will silently climb toward the
  rlimit ceiling. Without this card the diagnosis is shell-only
  (``ls /proc/$(pidof bot)/fd | wc -l``).
* "Which subsystem is leaking?" — the by-kind breakdown
  (regular file / socket / pipe / anon_inode / other) is the
  routing signal: regular-file leak → image processing, socket
  leak → aiohttp / DB pool, anon_inode → epoll / inotify /
  eventfd churn. The kind itself is a first-pass triage step
  before we go digging through the code.
* "Is the trend stable?" — operators sample this twice (now and
  five minutes later) to confirm whether the count is steady
  (normal) or climbing (leak). The card doesn't sample over time
  — it gives a single read; the trend is the operator's job.

Linux-only at the directory-walk layer (reads /proc/self/fd). On
macOS / Windows the directory doesn't exist; the card surfaces
"unavailable" and the kind breakdown is skipped. Mirrors the
posture of /admin_cpu's sched_getaffinity branch.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.fds")


# Tally bucket order — operationally most informative first. The
# renderer iterates this tuple so an operator scans the same shape
# every time, regardless of which kinds happen to be zero on any
# given sample. "other" is a deliberate catch-all so we never lose
# a count to an unrecognised stat type.
_KINDS: tuple[str, ...] = ("regular", "socket", "pipe", "anon_inode", "other")


class _FDSnapshot:
    """Captured fd census.

    ``total`` is the headline number; ``by_kind`` is the dict the
    renderer walks. ``available`` is the Linux-only flag — on
    non-Linux ``total`` and ``by_kind`` are both meaningless and the
    renderer takes the "unavailable" branch.
    """

    __slots__ = ("available", "by_kind", "total")

    def __init__(
        self,
        *,
        available: bool,
        total: int,
        by_kind: dict[str, int],
    ) -> None:
        self.available = available
        self.total = total
        self.by_kind = by_kind


def _classify(fd_path: Path) -> str:
    """Map an fd's target to one of the ``_KINDS`` buckets.

    ``/proc/self/fd/N`` is a symlink; ``os.lstat`` on the link
    itself isn't what we want — ``os.stat`` follows it to the
    underlying inode, where the mode tells us socket vs pipe vs
    regular. ``anon_inode`` (epoll, eventfd, timerfd, signalfd,
    inotify) shows up via the symlink's ``readlink`` target rather
    than a stat mode, so we check that first.

    Errors collapse to "other" — an fd can be racily closed by
    another thread between ``listdir`` and ``stat``, and we'd
    rather lose granularity on one row than crash the whole
    card.
    """
    try:
        target = os.readlink(fd_path)
    except OSError:
        return "other"
    if target.startswith(("anon_inode:", "/[anon_inode:")):
        return "anon_inode"
    try:
        st = os.stat(fd_path)
    except OSError:
        return "other"
    mode = st.st_mode
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISFIFO(mode):
        return "pipe"
    if stat.S_ISREG(mode):
        return "regular"
    return "other"


def _capture(fd_root: Path = Path("/proc/self/fd")) -> _FDSnapshot:
    """Walk ``/proc/self/fd`` and tally by kind.

    ``fd_root`` is parameterised purely so the tests can point us
    at a fake tree; at runtime the default is the real path.

    A read failure on the root directory means we're not on Linux
    (``/proc`` absent) — the card renders "unavailable" rather
    than zeros, which would be misleading (zero open fds would
    require the bot to have closed even its own stdin/stdout).
    """
    by_kind: dict[str, int] = {kind: 0 for kind in _KINDS}
    try:
        entries = list(fd_root.iterdir())
    except OSError:
        return _FDSnapshot(available=False, total=0, by_kind=by_kind)
    total = 0
    for entry in entries:
        total += 1
        by_kind[_classify(entry)] += 1
    return _FDSnapshot(available=True, total=total, by_kind=by_kind)


def _render(snap: _FDSnapshot) -> str:
    lines = ["📂 <b>Open file descriptors</b>", ""]
    if not snap.available:
        lines.append(
            "  <code>unavailable</code> <i>(/proc/self/fd not readable — non-Linux host?)</i>"
        )
        lines.append("")
        lines.append(
            "<i>Linux-only diagnostic. On macOS / Windows the fd "
            "directory doesn't exist; use lsof or platform-specific "
            "tooling for the same triage.</i>"
        )
        return "\n".join(lines)

    lines.append(f"  • <b>total open:</b> <code>{snap.total}</code>")
    lines.append("")
    lines.append("  <b>by kind:</b>")
    for kind in _KINDS:
        count = snap.by_kind.get(kind, 0)
        lines.append(f"    • <code>{kind}</code>: <code>{count}</code>")
    lines.append("")
    lines.append(
        "<i>Headline number is the load-bearing one — compare to "
        "the soft NOFILE on /admin_fdlimit. Kind breakdown routes "
        "leak-hunts: regular→image/temp, socket→aiohttp/DB pool, "
        "anon_inode→epoll/eventfd churn, pipe→subprocess pipes "
        "left open. Sample twice (5 min apart) to spot a climbing "
        "trend vs. steady consumption.</i>"
    )
    return "\n".join(lines)


async def handle_admin_fds(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_fds; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info(
        "/admin_fds rendered",
    )


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.fds")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_fds(message, settings)

    router.message.register(_entry, Command("admin_fds", ignore_case=True))
    return router
