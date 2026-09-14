"""``/admin_nr_open`` — kernel hard ceiling on per-process RLIMIT_NOFILE.

The fd admin surface now has four cards:

* /admin_fds — per-process: current open fds (count of
  /proc/self/fd entries).
* /admin_fdlimit — per-process: current RLIMIT_NOFILE soft/hard
  via getrlimit(2).
* /admin_limits — per-process: /proc/self/limits dump (includes
  NOFILE again).
* /admin_file_nr (Stage 133) — system-wide: fs.file-nr vs
  fs.file-max — total open fds across every process on the host.

All four answer "where are we now / what's our ceiling," but
NONE of them surface the **kernel** ceiling on the ceiling:
``fs.nr_open``. This is the value above which even root cannot
raise RLIMIT_NOFILE — setrlimit(2)/prlimit(2) return EPERM
for hard values exceeding nr_open even with CAP_SYS_RESOURCE.

The diagnostic story: an operator tries to raise the bot's fd
ceiling via systemd ``LimitNOFILE=`` or ``ulimit -n`` because
/admin_file_nr or /admin_fds is creeping high, and the
operation silently caps at fs.nr_open (default 1048576 on
modern kernels, but cloud images and stripped containers
sometimes ship with it lowered to 65536 or even less). Without
this card the operator has no way to see WHY the limit refused
to rise — systemd just logs "limit applied" and the new value
is whatever nr_open allowed.

Why this bot specifically cares: the strangler plan's five WAL
SQLite databases plus aiohttp connection pools plus the
aiogram long-poll connection routinely push the bot toward
the low-thousands-of-fds range. Plenty of headroom under the
default nr_open, but the very-low-nr_open container case is
the one where a quietly-capped ulimit becomes a mystery
ENFILE in the bot's own logs.

⚠ predicate: hard-RLIMIT_NOFILE / nr_open >= ``_NR_OPEN_WARN_RATIO``
(0.8). The signal is "our ceiling is close to the kernel
ceiling on the ceiling" — once we cross it, raising the limit
further requires sysctl, not setrlimit. Pinned cry-wolf
must-not-fire on a realistic healthy sample (NOFILE=1024 of
nr_open=1048576).

Sources:

* ``/proc/sys/fs/nr_open`` — single int, kernel-imposed hard
  ceiling on per-process RLIMIT_NOFILE.
* ``resource.getrlimit(RLIMIT_NOFILE)`` — current process's
  soft/hard. The hard value is what gets compared against
  nr_open.

Same wiring as every other admin card — silent-drop, private-
only, pure stdlib, hermetic via keyword-only injection.
"""

from __future__ import annotations

import resource
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.nr_open")


_NR_OPEN_PATH = Path("/proc/sys/fs/nr_open")


# Same threshold as the host-ceiling trio + max_map_count.
# The cliff here is EPERM on prlimit/setrlimit when trying
# to raise the hard limit above nr_open — operator-actionable
# (raise vm.nr_open via sysctl, then the per-process bump
# can succeed).
_NR_OPEN_WARN_RATIO = 0.8


# Injectable for tests so the rlimit read is hermetic — same
# pattern as the other host-ceiling cards use for path
# injection. Default is the live resource module call.
def _live_rlimit_nofile() -> tuple[int, int]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return soft, hard


class _NrOpenSnapshot:
    """Captured fs.nr_open + per-process RLIMIT_NOFILE.

    Fields use -1 sentinel for "couldn't read this source" so
    render distinguishes "kernel said 0" (legitimate on a
    weird custom kernel) from "we failed to open the sysctl."
    The two sources are independent — a CONFIG-stripped kernel
    might not expose fs.nr_open while RLIMIT_NOFILE is still
    queryable, or a sandboxed container might fail rlimit
    while sysctl reads succeed.
    """

    __slots__ = ("available", "nofile_hard", "nofile_soft", "nr_open")

    def __init__(
        self,
        *,
        nr_open: int,
        nofile_soft: int,
        nofile_hard: int,
        available: bool,
    ) -> None:
        self.nr_open = nr_open
        self.nofile_soft = nofile_soft
        self.nofile_hard = nofile_hard
        self.available = available

    @property
    def usage_ratio(self) -> float:
        # Defensive: ceiling<=0 or sentinel → 0.0 ("we don't
        # know, so don't warn"). The signal is hard/nr_open
        # because the hard limit is what the kernel actually
        # caps prlimit at — soft is just where the bot
        # currently lives within that envelope.
        if self.nr_open <= 0 or self.nofile_hard < 0:
            return 0.0
        return self.nofile_hard / self.nr_open

    @property
    def under_pressure(self) -> bool:
        return self.usage_ratio >= _NR_OPEN_WARN_RATIO


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return -1


def _read_rlimit_safe(reader: Callable[[], tuple[int, int]]) -> tuple[int, int]:
    """Wrap the rlimit read in defensive handling. resource.getrlimit
    can raise ValueError on a kernel that doesn't expose RLIMIT_NOFILE
    (theoretical; modern kernels always do) — and OSError on a
    sandbox that intercepts the syscall. Either way we return -1/-1
    and let render show 'unknown'."""
    try:
        return reader()
    except (OSError, ValueError):
        return (-1, -1)


def _capture(
    *,
    nr_open_path: Path = _NR_OPEN_PATH,
    rlimit_reader: Callable[[], tuple[int, int]] = _live_rlimit_nofile,
) -> _NrOpenSnapshot:
    nr_open = _read_int(nr_open_path)
    soft, hard = _read_rlimit_safe(rlimit_reader)
    # available iff at least one source produced a usable
    # value. Both-failed means non-procfs sandbox / macOS dev
    # and we render the dedicated unavailable note.
    available = nr_open >= 0 or soft >= 0 or hard >= 0
    return _NrOpenSnapshot(
        nr_open=nr_open,
        nofile_soft=soft,
        nofile_hard=hard,
        available=available,
    )


def _fmt(value: int) -> str:
    if value < 0:
        return "unknown"
    return f"{value:,}"


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _render(snap: _NrOpenSnapshot) -> str:
    lines = ["🚪 <b>RLIMIT_NOFILE vs fs.nr_open</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>Neither /proc/sys/fs/nr_open nor "
            "RLIMIT_NOFILE readable — non-procfs container, "
            "macOS dev, or a sandbox intercepting both.</i>"
        )
        return "\n".join(lines)

    warn = snap.under_pressure

    lines.append(f"  RLIMIT_NOFILE soft=<code>{_fmt(snap.nofile_soft)}</code>")
    lines.append(f"  RLIMIT_NOFILE hard=<code>{_fmt(snap.nofile_hard)}</code>")
    lines.append(f"  fs.nr_open=<code>{_fmt(snap.nr_open)}</code>")
    if snap.nr_open > 0 and snap.nofile_hard >= 0:
        marker = " ⚠" if warn else ""
        lines.append(f"  hard/nr_open=<code>{_fmt_pct(snap.usage_ratio)}</code>{marker}")
    else:
        lines.append("  hard/nr_open=<code>unknown</code>")

    lines.append("")
    if warn:
        lines.append(
            f"<i>⚠ hard RLIMIT_NOFILE at "
            f"<code>{_fmt_pct(snap.usage_ratio)}</code> of "
            "<code>fs.nr_open</code>. prlimit(2)/setrlimit(2) "
            "return EPERM above the kernel ceiling — systemd "
            "<code>LimitNOFILE=</code> and shell <code>ulimit -n</code> "
            "silently cap at fs.nr_open even with CAP_SYS_RESOURCE. "
            "If the bot needs more fds, raise "
            "<code>sysctl -w fs.nr_open=1048576</code> first, then "
            "restart so the process inherits the new hard limit. "
            "Distinct from /admin_file_nr (system-wide table) and "
            "/admin_fds (our current count) — those say WHERE we "
            "are; this says how high we can go without root tuning.</i>"
        )
    else:
        lines.append(
            f"<i>No warnings — our hard RLIMIT_NOFILE is under "
            f"<code>{int(_NR_OPEN_WARN_RATIO * 100)}%</code> of "
            "fs.nr_open. Headroom to raise via setrlimit(2) "
            "without sysctl tuning if the bot ever needs it. "
            "Completes the fd story alongside /admin_fds (current), "
            "/admin_fdlimit (per-process soft/hard), /admin_file_nr "
            "(system-wide allocated).</i>"
        )
    return "\n".join(lines)


async def handle_admin_nr_open(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_nr_open; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        nr_open=snap.nr_open,
        nofile_soft=snap.nofile_soft,
        nofile_hard=snap.nofile_hard,
        usage_ratio=snap.usage_ratio,
    ).info("/admin_nr_open rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.nr_open")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_nr_open(message, settings)

    router.message.register(_entry, Command("admin_nr_open", ignore_case=True))
    return router
