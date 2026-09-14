"""``/admin_file_nr`` — system-wide kernel file-handle counter.

The FD admin surface already covers two scopes:

* /admin_fds — per-process: count of entries in /proc/self/fd
  for *this* bot process. Local — saturating it means we leaked
  fds in our own handler.
* /admin_fdlimit — per-process: RLIMIT_NOFILE soft/hard ceiling
  from getrlimit(2). Local — the ceiling our process is held to.
* /admin_limits — /proc/self/limits — all rlimits including
  RLIMIT_NOFILE again. Same scope.

What none surface is the **system-wide** counter: how many file
handles are open across *every* process on the host, vs the
kernel's global ``fs.file-max`` ceiling. Hitting that ceiling
returns ``ENFILE`` from open(2) for everyone — including us —
even when our per-process RLIMIT_NOFILE has plenty of headroom.
This is the classic "noisy neighbour on a shared host fills the
fd table and our innocent open() returns ENFILE" footgun.

/proc/sys/fs/file-nr emits three space-or-tab separated ints
on a single line::

    1024	0	9223372036854775807

Columns (stable since 2.6):

1. **allocated** — file structs currently in use system-wide.
2. **unused** — historically the free-list count; on modern
   kernels (since the slab allocator rewrite) always 0 because
   freed file structs return to the slab immediately. We render
   it but the operator signal lives in column 1.
3. **maximum** — fs.file-max — the global ceiling.

⚠ predicate: allocated / maximum >= ``_FILE_NR_WARN_RATIO``
(0.8). Ratio-based because fs.file-max is heavily tuned per
host (default on 64-bit is ~9.2e18 i.e. effectively unlimited,
but many distros and containers cap it explicitly to single
or double-digit millions). A fixed absolute threshold would
be wrong on either tail. Pinned with must-not-fire test on
the canonical-healthy sample (allocated << max).

Cross-card: when this ⚠ fires, /admin_fds and /admin_fdlimit
will likely show our process is *fine* — that's the whole
point. The squeeze is on the host, not on us.

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


log = logger.bind(component="handlers.admin.file_nr")


_FILE_NR_PATH = Path("/proc/sys/fs/file-nr")


# Same 80% threshold the keyring-quota and disk/fd cards use —
# operator-intuitive "approaching limit" mark. Far enough from
# the cliff that the operator has time to raise fs.file-max or
# audit which process is leaking.
_FILE_NR_WARN_RATIO = 0.8


class _FileNrSnapshot:
    """Captured /proc/sys/fs/file-nr.

    ``-1`` sentinel for "couldn't parse" on each field so render
    can distinguish "kernel said zero" from "we failed to read."
    ``available`` is False when the file is absent (macOS dev /
    a procfs-stripped container) so render shows an explicit
    unavailable note rather than spurious zeros.
    """

    __slots__ = ("allocated", "available", "maximum", "unused")

    def __init__(self, *, allocated: int, unused: int, maximum: int, available: bool) -> None:
        self.allocated = allocated
        self.unused = unused
        self.maximum = maximum
        self.available = available

    @property
    def usage_ratio(self) -> float:
        # Defensive division: a kernel emitting maximum<=0 (or
        # the -1 parse-failure sentinel) would crash a naive
        # ratio. Treat as 0.0 — "we don't know, so don't warn"
        # is the safer posture than "everything is full".
        if self.maximum <= 0:
            return 0.0
        if self.allocated < 0:
            return 0.0
        return self.allocated / self.maximum

    @property
    def under_pressure(self) -> bool:
        return self.usage_ratio >= _FILE_NR_WARN_RATIO


def _parse_file_nr(text: str) -> tuple[int, int, int]:
    """Parse /proc/sys/fs/file-nr → (allocated, unused, maximum).
    Anything we can't parse becomes ``-1`` so render can show
    'unknown' rather than crashing. The file is conventionally
    one line of three whitespace-separated ints, but defensive
    parsing protects against a kernel change or weird container.
    """
    parts = text.split()
    if len(parts) < 3:
        return (-1, -1, -1)
    out: list[int] = []
    for token in parts[:3]:
        try:
            out.append(int(token))
        except ValueError:
            out.append(-1)
    return (out[0], out[1], out[2])


def _capture(*, path: Path = _FILE_NR_PATH) -> _FileNrSnapshot:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _FileNrSnapshot(allocated=-1, unused=-1, maximum=-1, available=False)
    allocated, unused, maximum = _parse_file_nr(text)
    return _FileNrSnapshot(allocated=allocated, unused=unused, maximum=maximum, available=True)


def _fmt(value: int) -> str:
    if value < 0:
        return "unknown"
    return f"{value:,}"


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _render(snap: _FileNrSnapshot) -> str:
    lines = ["📂 <b>System-wide file handles (/proc/sys/fs/file-nr)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/sys/fs/file-nr unavailable — Linux-only "
            "surface (macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    warn = snap.under_pressure

    lines.append(f"  allocated=<code>{_fmt(snap.allocated)}</code>")
    lines.append(
        f"  unused=<code>{_fmt(snap.unused)}</code> "
        "(always 0 on modern kernels — slab returns freed file* immediately)"
    )
    lines.append(f"  maximum (fs.file-max)=<code>{_fmt(snap.maximum)}</code>")
    if snap.maximum > 0 and snap.allocated >= 0:
        marker = " ⚠" if warn else ""
        lines.append(f"  usage=<code>{_fmt_pct(snap.usage_ratio)}</code>{marker}")
    else:
        lines.append("  usage=<code>unknown</code>")

    lines.append("")
    if warn:
        lines.append(
            f"<i>⚠ system-wide file-handle table at "
            f"<code>{_fmt_pct(snap.usage_ratio)}</code> of "
            "<code>fs.file-max</code> — open(2) anywhere on the "
            "host (including this bot) starts returning ENFILE "
            "above the ceiling, regardless of our per-process "
            "RLIMIT_NOFILE headroom. Audit with "
            "<code>lsof | wc -l</code> or "
            "<code>find /proc/*/fd -type l | awk -F/ '{print $3}' "
            "| sort | uniq -c | sort -n</code> to find the "
            "leaker. /admin_fds / /admin_fdlimit will show *our* "
            "process is fine — the squeeze is host-wide.</i>"
        )
    else:
        lines.append(
            f"<i>No warnings — system-wide file-handle usage is "
            f"under <code>{int(_FILE_NR_WARN_RATIO * 100)}%</code> "
            "of fs.file-max. This is the *host* counter, distinct "
            "from /admin_fds (per-process count) and /admin_fdlimit "
            "(per-process ceiling) — ENFILE on this counter affects "
            "every process on the box, not just us.</i>"
        )
    return "\n".join(lines)


async def handle_admin_file_nr(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_file_nr; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        allocated=snap.allocated,
        maximum=snap.maximum,
        usage_ratio=snap.usage_ratio,
    ).info("/admin_file_nr rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.file_nr")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_file_nr(message, settings)

    router.message.register(_entry, Command("admin_file_nr", ignore_case=True))
    return router
