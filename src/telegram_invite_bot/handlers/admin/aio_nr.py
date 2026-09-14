"""``/admin_aio_nr`` — kernel AIO context count vs fs.aio-max-nr.

Third in the host-wide-ceiling trio:

* /admin_file_nr (Stage 133) — open file handles vs fs.file-max.
  Saturation → ENFILE from open(2) host-wide.
* /admin_pid_max (Stage 134) — task count vs kernel.pid_max +
  threads-max. Saturation → EAGAIN from fork(2) host-wide.
* **This card** — AIO contexts vs fs.aio-max-nr. Saturation →
  EAGAIN from io_setup(2) host-wide.

The three sit at the same architectural layer (kernel-global
sysctl ceilings, distinct from per-process rlimits), and the
operator-correlation story is the same: when our application
starts seeing one of these errnos, the per-process cards
(/admin_fdlimit, /admin_limits) typically look fine because
the squeeze is on the *host*, not on us.

Why this one matters specifically: SQLAlchemy + aiosqlite on
busy WAL-mode databases (we have five — see the strangler
plan) routes I/O through libaio context creation. Postgres
clients, asyncpg, several Python AIO wrappers also call
io_setup(2). Hitting fs.aio-max-nr produces an opaque "couldn't
init AIO context" error in user-space — easy to misdiagnose
as the application's bug instead of the kernel cap. Surfacing
the ratio lets the operator see the squeeze coming.

Sources:

* ``/proc/sys/fs/aio-nr`` — single int, currently allocated
  AIO contexts system-wide.
* ``/proc/sys/fs/aio-max-nr`` — single int, the cap. Default
  65536; tunable, often raised to 1048576 on databases.

⚠ predicate: aio-nr / aio-max-nr >= ``_AIO_WARN_RATIO`` (0.8).
Same threshold and shape as file-nr / pid_max — operator-
intuitive "approaching limit." Pinned with must-not-fire test
on the canonical-healthy sample (aio-nr << aio-max-nr).

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


log = logger.bind(component="handlers.admin.aio_nr")


_AIO_NR_PATH = Path("/proc/sys/fs/aio-nr")
_AIO_MAX_NR_PATH = Path("/proc/sys/fs/aio-max-nr")


# Same 80% mark used by file-nr / pid_max / key_users for
# symmetry across the host-ceiling trio. Operator-intuitive
# "approaching limit" — far enough from the cliff that
# fs.aio-max-nr can be raised in time, close enough that the
# warning is actionable.
_AIO_WARN_RATIO = 0.8


class _AioNrSnapshot:
    """Captured fs.aio-nr / fs.aio-max-nr state.

    Both fields carry ``-1`` for "couldn't read" so render can
    distinguish 'kernel said zero' (legitimate — no AIO has
    happened yet) from 'we failed to open the sysctl'. The two
    sources are independent files — a kernel without CONFIG_AIO
    has neither, a kernel with CONFIG_AIO but mounted read-only
    has both — so we don't require both succeed; one present is
    enough for ``available=True`` and partial render.
    """

    __slots__ = ("aio_max_nr", "aio_nr", "available")

    def __init__(self, *, aio_nr: int, aio_max_nr: int, available: bool) -> None:
        self.aio_nr = aio_nr
        self.aio_max_nr = aio_max_nr
        self.available = available

    @property
    def usage_ratio(self) -> float:
        # Defensive: ceiling<=0 (sentinel or exotic zero-cap)
        # returns 0.0 ("we don't know, so don't warn"). Same
        # posture as file-nr / pid_max.
        if self.aio_max_nr <= 0 or self.aio_nr < 0:
            return 0.0
        return self.aio_nr / self.aio_max_nr

    @property
    def under_pressure(self) -> bool:
        return self.usage_ratio >= _AIO_WARN_RATIO


def _read_int(path: Path) -> int:
    """Read a single int from a /proc sysctl file; ``-1`` on
    any failure. Identical contract to the other host-ceiling
    cards — keeps the partial-render story uniform."""
    try:
        return int(path.read_text(encoding="utf-8", errors="replace").strip())
    except (OSError, ValueError):
        return -1


def _capture(
    *,
    aio_nr_path: Path = _AIO_NR_PATH,
    aio_max_nr_path: Path = _AIO_MAX_NR_PATH,
) -> _AioNrSnapshot:
    aio_nr = _read_int(aio_nr_path)
    aio_max_nr = _read_int(aio_max_nr_path)
    # "available" requires at least one source — both-absent
    # means CONFIG_AIO=n or non-procfs (macOS dev), and we
    # surface the dedicated unavailable note instead of two
    # 'unknown' rows that imply partial success.
    available = aio_nr >= 0 or aio_max_nr >= 0
    return _AioNrSnapshot(aio_nr=aio_nr, aio_max_nr=aio_max_nr, available=available)


def _fmt(value: int) -> str:
    if value < 0:
        return "unknown"
    return f"{value:,}"


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _render(snap: _AioNrSnapshot) -> str:
    lines = ["⚡ <b>Kernel AIO contexts (/proc/sys/fs/aio-*)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/sys/fs/aio-{nr,max-nr} unreadable — "
            "CONFIG_AIO=n kernel, macOS dev, or non-procfs "
            "container.</i>"
        )
        return "\n".join(lines)

    warn = snap.under_pressure

    lines.append(f"  fs.aio-nr=<code>{_fmt(snap.aio_nr)}</code>")
    lines.append(f"  fs.aio-max-nr=<code>{_fmt(snap.aio_max_nr)}</code>")
    if snap.aio_max_nr > 0 and snap.aio_nr >= 0:
        marker = " ⚠" if warn else ""
        lines.append(f"  usage=<code>{_fmt_pct(snap.usage_ratio)}</code>{marker}")
    else:
        lines.append("  usage=<code>unknown</code>")

    lines.append("")
    if warn:
        lines.append(
            f"<i>⚠ AIO context table at "
            f"<code>{_fmt_pct(snap.usage_ratio)}</code> of "
            "<code>fs.aio-max-nr</code> — io_setup(2) starts "
            "returning EAGAIN above the cap. Affects libaio "
            "consumers host-wide: aiosqlite on busy WAL "
            "databases, asyncpg/postgres clients, several "
            "AIO-backed Python libraries. In user-space this "
            "surfaces as opaque 'couldn't init AIO context' "
            "errors — easy to misdiagnose as application bugs. "
            "Raise <code>fs.aio-max-nr</code> (common bump: "
            "<code>65536 → 1048576</code>) or audit which "
            "process is leaking contexts.</i>"
        )
    else:
        lines.append(
            f"<i>No warnings — AIO context usage under "
            f"<code>{int(_AIO_WARN_RATIO * 100)}%</code> of "
            "fs.aio-max-nr. Third of the host-ceiling trio "
            "alongside /admin_file_nr (fd table) and "
            "/admin_pid_max (task count) — same architectural "
            "layer, distinct failure mode (EAGAIN from "
            "io_setup vs ENFILE/EAGAIN from open/fork).</i>"
        )
    return "\n".join(lines)


async def handle_admin_aio_nr(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_aio_nr; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        aio_nr=snap.aio_nr,
        aio_max_nr=snap.aio_max_nr,
        usage_ratio=snap.usage_ratio,
    ).info("/admin_aio_nr rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.aio_nr")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_aio_nr(message, settings)

    router.message.register(_entry, Command("admin_aio_nr", ignore_case=True))
    return router
