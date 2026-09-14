"""``/admin_io`` — cumulative disk I/O counters for this process.

Reads ``/proc/self/io`` (one of the most underused diagnostic
endpoints the kernel exposes) and surfaces the seven byte/syscall
counters. Complements /admin_rusage (page faults + ctx switches +
block I/O via getrusage) by giving the **byte-accurate** view —
getrusage's block I/O is in 512-byte blocks and excludes cache
hits; /proc/self/io counts every syscall byte regardless of cache.

Why an operator wants this:

* "Why is the bot's disk light blinking?" — ``write_bytes``
  (actual blocks queued to disk, post-cache) vs ``wchar``
  (total bytes passed to write syscalls) tells you whether a
  hot loop is hitting disk or just thrashing the page cache.
  A ratio of ``wchar`` ≫ ``write_bytes`` usually means a noisy
  logger spamming SQLite WAL pages that the kernel coalesces;
  ``write_bytes`` ≈ ``wchar`` means actual sustained disk load.
* ``cancelled_write_bytes`` is the truancy counter — bytes that
  were dirtied in page cache but the file was truncated/unlinked
  before the kernel flushed them. A nonzero value on a
  long-running bot usually means a log-rotation race or a
  tempfile churn pattern; benign in small numbers but worth
  surfacing.
* ``syscr`` / ``syscw`` (raw read/write syscall counts) catch
  the "tight loop calling write(2) per byte" anti-pattern that
  byte counters alone hide. A syscall-per-second rate computed
  by the operator over two samples is the canonical "is this
  bot busy or just hot?" check.
* Cumulative since process start — no first-derivative here.
  The operator pulls the card twice (say, 60s apart) and diffs
  by eye. That's the same workflow as /admin_proc's CPU
  counters, deliberately consistent so the muscle memory
  carries.

Posture: silent-drop for non-devs, private-only at the router
level. Linux-only — /proc/self/io is absent on macOS / BSD;
render an informational note and skip ⚠ markers there.

Cry-wolf prevention: no ⚠ on absolute byte counts. A bot that
has been up for 30 days WILL have terabytes of cumulative I/O
and that's not a problem. The only ⚠ is on a nonzero
``cancelled_write_bytes`` over a small threshold, because that
signals a real lifecycle race rather than steady-state load.
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


log = logger.bind(component="handlers.admin.io")


_PROC_SELF_IO = Path("/proc/self/io")


# /proc/self/io fields in the kernel's declaration order. Render
# walks this tuple so the layout is stable across kernel versions
# even if /proc/self/io ever grows new fields (we'll bucket those
# under "additional fields" — forward-compat without crashing).
_IO_FIELDS: tuple[str, ...] = (
    "rchar",
    "wchar",
    "syscr",
    "syscw",
    "read_bytes",
    "write_bytes",
    "cancelled_write_bytes",
)


# Human-readable one-liner per field. The kernel docs are at
# Documentation/filesystems/proc.rst — paraphrased to fit a
# Telegram message and to surface the operator-actionable nuance
# (cache vs disk; syscall counts vs byte counts) that the bare
# field name doesn't convey.
_IO_FIELD_NOTES: dict[str, str] = {
    "rchar": "total bytes read via syscalls (cache hits included)",
    "wchar": "total bytes written via syscalls (cache hits included)",
    "syscr": "read-family syscall count (read/pread/readv/…)",
    "syscw": "write-family syscall count (write/pwrite/writev/…)",
    "read_bytes": "bytes actually fetched from storage (cache misses)",
    "write_bytes": "bytes actually queued to storage (post-cache)",
    "cancelled_write_bytes": ("dirtied-then-truncated bytes — log-rotation race signal"),
}


# cancelled_write_bytes ⚠ floor. The kernel will report small
# nonzero values for benign log-rotation churn; we only cry when
# the count crosses a threshold suggesting a real misuse pattern.
# 1 MiB is conservative — a typical rotated log discards a few KiB
# of dirty pages at most.
_CANCELLED_WARN_BYTES = 1 * 1024 * 1024


class _IoSnapshot:
    """Captured /proc/self/io readout.

    ``fields`` maps each /proc/self/io field name to its int
    value, or None when the field was missing/unparseable.
    ``status_present`` distinguishes "the file exists but we
    couldn't read it cleanly" from "we're not on Linux".
    """

    __slots__ = ("fields", "status_present")

    def __init__(
        self,
        *,
        fields: dict[str, int | None],
        status_present: bool,
    ) -> None:
        self.fields = fields
        self.status_present = status_present


def _parse_io(text: str) -> dict[str, int | None]:
    """Parse /proc/self/io into a dict keyed by every field name
    in ``_IO_FIELDS`` (missing fields → None).

    The kernel format is one ``name: value`` line per field, with
    whitespace between the colon and the value. Future kernels may
    add fields — we bucket those under their own names (the dict
    is open) and the render walks ``_IO_FIELDS`` for canonical
    layout, then surfaces extras under an "additional fields"
    footer.
    """
    parsed: dict[str, int | None] = dict.fromkeys(_IO_FIELDS)
    for line in text.splitlines():
        name, _, raw = line.partition(":")
        name = name.strip()
        if not name:
            continue
        try:
            parsed[name] = int(raw.strip())
        except ValueError:
            # Unparseable value → None for that field. Forward-
            # compat: a future kernel emitting a non-integer here
            # must degrade rather than crash.
            parsed[name] = None
    return parsed


def _capture(*, io_path: Path = _PROC_SELF_IO) -> _IoSnapshot:
    try:
        text = io_path.read_text()
    except OSError:
        return _IoSnapshot(
            fields=dict.fromkeys(_IO_FIELDS),
            status_present=False,
        )
    return _IoSnapshot(fields=_parse_io(text), status_present=True)


def _cancelled_concerning(cancelled: int | None) -> bool:
    """⚠ predicate. Only the ``cancelled_write_bytes`` field has a
    cry-wolf-safe threshold — every other counter is monotonic +
    cumulative since process start, so any absolute value is
    meaningless without a second sample to diff against."""
    return cancelled is not None and cancelled >= _CANCELLED_WARN_BYTES


def _fmt_bytes(n: int) -> str:
    """Compact byte formatting. Cumulative byte counters reach
    GiB / TiB quickly on a long-lived bot — render raw integers
    only for syscall counts."""
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{n}B"  # pragma: no cover — loop covers all cases


def _is_byte_field(name: str) -> bool:
    """syscr / syscw are syscall counts; everything else is bytes.
    The render uses this to pick between ``_fmt_bytes`` and the
    raw int format."""
    return name not in ("syscr", "syscw")


def _render(snap: _IoSnapshot) -> str:
    lines = ["💾 <b>Disk I/O counters</b>", ""]

    if not snap.status_present:
        # Same informational posture as every other Linux-only
        # admin card. Zero ⚠ — non-Linux is not a problem.
        lines.append(
            "  <i>/proc/self/io not readable — non-Linux host or "
            "restricted namespace. No actionable signal.</i>"
        )
        lines.append("")
        lines.append(
            "<i>⚠ markers: only emitted on nonzero "
            "<code>cancelled_write_bytes</code> over 1 MiB "
            "(log-rotation race / tempfile churn signal). Every "
            "other counter is cumulative since process start — "
            "diff two samples to derive a rate.</i>"
        )
        return "\n".join(lines)

    lines.append("  <b>cumulative since process start:</b>")
    for name in _IO_FIELDS:
        value = snap.fields.get(name)
        note = _IO_FIELD_NOTES.get(name, "")
        if value is None:
            lines.append(f"    • <code>{name}</code>: <i>unreadable</i> <i>— {note}</i>")
            continue
        rendered_value = _fmt_bytes(value) if _is_byte_field(name) else f"{value:,}"
        warn = " ⚠" if name == "cancelled_write_bytes" and _cancelled_concerning(value) else ""
        lines.append(
            f"    • <code>{name}</code>: <code>{rendered_value}</code> <i>({note})</i>{warn}"
        )

    # Forward-compat: surface any /proc/self/io fields the kernel
    # added that aren't in our table. The operator gets a heads-up
    # that something new appeared, and we don't drop data silently.
    extras = sorted(set(snap.fields) - set(_IO_FIELDS))
    if extras:
        lines.append("")
        lines.append("  <b>additional fields (kernel-side additions):</b>")
        for name in extras:
            value = snap.fields[name]
            if value is None:
                lines.append(f"    • <code>{name}</code>: <i>unreadable</i>")
            else:
                lines.append(f"    • <code>{name}</code>: <code>{value}</code>")

    lines.append("")
    lines.append(
        "<i>⚠ markers: only emitted on nonzero "
        "<code>cancelled_write_bytes</code> over 1 MiB "
        "(log-rotation race / tempfile churn signal). Every other "
        "counter is cumulative since process start — diff two "
        "samples to derive a rate. <code>wchar</code> ≫ "
        "<code>write_bytes</code> usually means cache absorbs the "
        "writes; <code>write_bytes</code> ≈ <code>wchar</code> "
        "means sustained disk load.</i>"
    )
    return "\n".join(lines)


async def handle_admin_io(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_io; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        status_present=snap.status_present,
        read_bytes=snap.fields.get("read_bytes"),
        write_bytes=snap.fields.get("write_bytes"),
        cancelled_write_bytes=snap.fields.get("cancelled_write_bytes"),
    ).info("/admin_io rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.io")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_io(message, settings)

    router.message.register(_entry, Command("admin_io", ignore_case=True))
    return router
