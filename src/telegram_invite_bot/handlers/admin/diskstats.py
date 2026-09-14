"""``/admin_diskstats`` — per-device block I/O from /proc/diskstats.

/admin_io covers /proc/self/io — the cumulative bytes/syscalls
THIS process has issued. That's per-process accounting. What it
*can't* answer is "which device on the host is currently saturated"
— the same RSS-mapped read can come from a fast NVMe drive or a
contended USB stick, and process-scoped counters won't tell you.

/proc/diskstats answers that, per-device:

* read_ios / write_ios — completed operations, cumulative since
  boot. Useful for delta-sampling, like /admin_loadavg's last_pid.
* sectors_read / sectors_written — actual data volume (×512 to
  get bytes; kernel still uses 512-byte sectors as the unit even
  for 4K-physical drives).
* ios_in_progress — *instantaneous* queue depth at the moment
  /proc was read. This is the field worth ⚠'ing on: a saturated
  block device drives this to 10+ while everything else stays at 0.

Why ios_in_progress specifically and not e.g. weighted_time_doing_ios:
  ios_in_progress is a snapshot; weighted_time is integrated since
  boot. The integrated counter is useful for "has this disk been
  busy lately?" but firing ⚠ on it would mean "this disk WAS busy
  some time in the last week" — exactly the cry-wolf failure mode
  this codebase rejects. Snapshot-style metrics give right-now
  health, integrated counters give history. We ⚠ on right-now.

Curated render: kernel loop / ram / dm-* / sr / fd devices are
included by default — operator might genuinely want to see them
(loopback mount thrashing, ramdisk consumption). The render caps
at 30 rows for Telegram's 4096 limit, with explicit hidden-count.

Forward-compat: /proc/diskstats had 14 fields per line before
Linux 4.18, 20 fields after (discard/flush counters added).
We accept either length — only the first 12 fields are referenced
by name; the rest are forwards-compatible bonus.

Same posture as every other admin card — silent-drop, private-only,
pure stdlib.
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


log = logger.bind(component="handlers.admin.diskstats")


_DISKSTATS_PATH = Path("/proc/diskstats")


# Saturated I/O queue. Modern NVMe handles 32+ in-flight ops without
# trouble; spinning rust starts queuing past ~4. 10 is a "definitely
# something is busy" threshold that catches both classes without
# crying wolf on a healthy multi-queue NVMe under nominal load.
_INFLIGHT_WARN = 10


# Render row cap — same logic as /admin_mounts (~40-row Telegram
# budget). Diskstats lines are shorter than mount lines so we can
# afford a bit more.
_RENDER_ROW_CAP = 30


# 512-byte sectors — kernel-internal unit, unchanged since the
# /proc/diskstats format was defined. Even 4K-physical drives
# report in 512-byte sectors here.
_SECTOR_BYTES = 512


class _DiskRow:
    """One /proc/diskstats line.

    All cumulative counters are int; ``in_flight`` is the only
    instantaneous reading and the only field that drives ⚠.
    """

    __slots__ = (
        "device",
        "in_flight",
        "major",
        "minor",
        "reads_completed",
        "sectors_read",
        "sectors_written",
        "writes_completed",
    )

    def __init__(
        self,
        *,
        major: int,
        minor: int,
        device: str,
        reads_completed: int,
        sectors_read: int,
        writes_completed: int,
        sectors_written: int,
        in_flight: int,
    ) -> None:
        self.major = major
        self.minor = minor
        self.device = device
        self.reads_completed = reads_completed
        self.sectors_read = sectors_read
        self.writes_completed = writes_completed
        self.sectors_written = sectors_written
        self.in_flight = in_flight


class _DiskstatsSnapshot:
    """Captured /proc/diskstats reading.

    * ``rows`` — every parsed device.
    * ``available`` — False when /proc/diskstats can't be read at
      all (macOS dev, container without /proc).
    """

    __slots__ = ("available", "rows")

    def __init__(
        self,
        *,
        rows: tuple[_DiskRow, ...],
        available: bool,
    ) -> None:
        self.rows = rows
        self.available = available


def _parse_diskstats(text: str) -> tuple[_DiskRow, ...]:
    """Parse /proc/diskstats.

    Format per line: ``major minor device <11+ counter fields>``.
    We only name the fields we care about; trailing fields (added
    in 4.18+ for discard / 5.5+ for flush) are accepted silently.
    Any line shorter than 12 fields is dropped — that's not a real
    diskstats line and we'd rather skip than guess.
    """
    rows: list[_DiskRow] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 12:
            continue
        try:
            major = int(parts[0])
            minor = int(parts[1])
            device = parts[2]
            reads_completed = int(parts[3])
            sectors_read = int(parts[5])
            writes_completed = int(parts[7])
            sectors_written = int(parts[9])
            in_flight = int(parts[11])
        except ValueError:
            # One bad line shouldn't disable the whole card.
            continue
        rows.append(
            _DiskRow(
                major=major,
                minor=minor,
                device=device,
                reads_completed=reads_completed,
                sectors_read=sectors_read,
                writes_completed=writes_completed,
                sectors_written=sectors_written,
                in_flight=in_flight,
            )
        )
    return tuple(rows)


def _capture(*, path: Path = _DISKSTATS_PATH) -> _DiskstatsSnapshot:
    """Read /proc/diskstats + build a snapshot.

    Keyword-only path parameter so tests can inject a tmp file —
    same hermetic-fixture pattern as every other diagnostic card.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _DiskstatsSnapshot(rows=(), available=False)
    return _DiskstatsSnapshot(rows=_parse_diskstats(text), available=True)


def _saturated_devices(snap: _DiskstatsSnapshot) -> tuple[str, ...]:
    """⚠ predicate (multi-value form). Returns the device names
    whose in_flight count exceeds the saturation threshold. Empty
    tuple = healthy. We return names rather than a bool so the
    render can highlight specifically which device is hot."""
    return tuple(row.device for row in snap.rows if row.in_flight > _INFLIGHT_WARN)


def _fmt_bytes(value: int) -> str:
    if value >= 1024 * 1024 * 1024:
        return f"{value / (1024**3):.2f} GiB"
    if value >= 1024 * 1024:
        return f"{value / (1024**2):.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value:,} B"


def _render(snap: _DiskstatsSnapshot) -> str:
    lines = ["💽 <b>Block I/O (/proc/diskstats)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/diskstats unavailable on this host — Linux-"
            "only card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        lines.append(
            "  <i>diskstats is empty — no block devices visible. "
            "Likely a private mount namespace or stripped container.</i>"
        )
        return "\n".join(lines)

    saturated = _saturated_devices(snap)
    if saturated:
        # Hoist above the table — a 30-row scroll would otherwise
        # bury the signal. Same ergonomic as /admin_mounts.
        lines.append(
            "  ⚠ <b>I/O queue saturation</b> on: "
            + ", ".join(f"<code>{d}</code>" for d in saturated)
            + " — in-flight ops over the "
            f"<code>{_INFLIGHT_WARN}</code> threshold."
        )
        lines.append("")

    shown = snap.rows[:_RENDER_ROW_CAP]
    hidden = len(snap.rows) - len(shown)

    for row in shown:
        marker = " ⚠" if row.in_flight > _INFLIGHT_WARN else ""
        read_bytes = row.sectors_read * _SECTOR_BYTES
        write_bytes = row.sectors_written * _SECTOR_BYTES
        lines.append(
            f"  • <code>{row.device}</code> "
            f"<i>({row.major}:{row.minor})</i> · "
            f"read=<code>{_fmt_bytes(read_bytes)}</code> "
            f"write=<code>{_fmt_bytes(write_bytes)}</code> "
            f"in-flight=<code>{row.in_flight}</code>{marker}"
        )

    if hidden > 0:
        lines.append("")
        lines.append(
            f"  <i>{hidden} additional device(s) hidden — Telegram "
            f"text cap. Total devices: {len(snap.rows)}.</i>"
        )

    lines.append("")
    lines.append(
        f"<i>⚠ markers: only on devices with in-flight I/O ops "
        f"over <code>{_INFLIGHT_WARN}</code> — a right-now "
        f"snapshot of queue depth, not historical weighted time. "
        f"read/write totals are cumulative since boot and informational. "
        f"See /admin_io for per-process I/O accounting.</i>"
    )
    return "\n".join(lines)


async def handle_admin_diskstats(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_diskstats; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        device_count=len(snap.rows),
        saturated=_saturated_devices(snap),
    ).info("/admin_diskstats rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.diskstats")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_diskstats(message, settings)

    router.message.register(_entry, Command("admin_diskstats", ignore_case=True))
    return router
