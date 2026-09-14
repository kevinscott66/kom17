"""``/admin_partitions`` — block-device inventory from /proc/partitions.

The existing storage surface looks at *activity* (diskstats:
reads/writes/queue depth) and *capacity used* (disk: per-mount df).
What neither surfaces is the **canonical device list the kernel
itself knows about** — every block major:minor it has registered,
the partition table on top of each, plus the loop/dm/zram virtual
devices.

Why this card is operationally distinct:

* /admin_diskstats names a device like ``dm-3`` or ``loop12``. The
  operator wants to know what that *is*. /proc/partitions lists
  every name the kernel recognizes with its major:minor and raw
  capacity in 1K-blocks. Cross-reference closes the loop.
* /admin_disk only knows about *mounted* filesystems. A volume
  that's attached but unmounted (a spare disk, a detached LVM
  member, a forgotten loop-mount of a backup image) is invisible
  there and shows up here.
* Total raw block-device capacity — a useful denominator when
  diskstats says "the system did 4 GiB of writes" and the
  operator wants the magnitude in context.

The format has been stable since 2.4 — first line is a column
header (``major minor  #blocks  name``), each subsequent row is
four whitespace-separated fields. The ``#blocks`` column is in
1-KiB units, not bytes — multiplying by 1024 gives byte-accurate
size. We compute that once at parse time and store both, because
a future drill-down (per-device usage history) would want both
the raw kernel field and the human number.

Curation: top _TOP_N devices by size, with whole-disk rows
flagged separately from partitions. A "whole disk" is a row whose
name is the parent device (sda, nvme0n1, vda); partition rows
share a prefix (sda1 of sda, nvme0n1p1 of nvme0n1). We don't try
to infer the hierarchy beyond marking which rows are whole disks
— the kernel doesn't surface parent links here and we'd be
guessing. The naming convention is itself the signal: sda + sda1
+ sda2 next to each other tells the operator the layout without
us computing it.

⚠ predicate: zero. Partition existence isn't a problem signal.
Operationally interesting cases (a disk failed and disappeared,
a loop mount that should be torn down, a forgotten zram device
holding swap) require *historical* comparison — we'd have to
remember the previous snapshot. Same posture as the rest of the
inventory cards (/admin_mounts, /admin_modules, /admin_swaps): we
surface state, operator compares mentally with what they expect.

Same posture as every other admin card — silent-drop, private-only,
pure stdlib, hermetic via keyword-only path injection.
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


log = logger.bind(component="handlers.admin.partitions")


_PARTITIONS_PATH = Path("/proc/partitions")

# Top-N rendered rows, sorted by descending size. Telegram message
# length is the constraint; 30 covers every realistic host —
# desktops have a dozen rows total, busy servers with LVM + loop
# mounts maybe forty.
_TOP_N = 30


class _Partition:
    """One row of /proc/partitions.

    ``blocks_1k`` is the raw kernel field (1-KiB units), preserved
    verbatim because the kernel exports it that way and any
    drill-down caller may want the unit-faithful value.
    ``bytes_total`` is the precomputed multiply — both the sort
    key and the human-render value, so we don't recompute on each
    sort comparison.
    """

    __slots__ = ("blocks_1k", "bytes_total", "major", "minor", "name")

    def __init__(
        self,
        *,
        major: int,
        minor: int,
        blocks_1k: int,
        name: str,
    ) -> None:
        self.major = major
        self.minor = minor
        self.blocks_1k = blocks_1k
        self.name = name
        self.bytes_total = blocks_1k * 1024

    @property
    def is_whole_disk(self) -> bool:
        """Heuristic: a name ending in a digit is *probably* a
        partition (sda1, nvme0n1p1). A name not ending in a digit
        (sda, nvme0n1, vda, dm-3) is *probably* a whole disk or a
        virtual device. The kernel doesn't expose the hierarchy
        directly in /proc/partitions, so this is the best we can
        do without parsing /sys/block — and good enough for the
        operator who just wants the rows grouped sensibly.

        Edge case: dm-N and md-N end in a digit but ARE
        whole-disk-like (device-mapper, mdraid). We accept the
        false-positive — operators reading the card know the
        convention, and the major number already disambiguates."""
        return not self.name[-1:].isdigit()


class _PartitionsSnapshot:
    """Captured /proc/partitions.

    ``rows`` — every parsed entry, in file order.
    ``available`` — False when /proc/partitions can't be read
    (macOS dev, non-procfs container, very rarely EACCES on
    hardened distros). Same handling as the rest of the procfs
    surface.
    """

    __slots__ = ("available", "rows")

    def __init__(self, *, rows: tuple[_Partition, ...], available: bool) -> None:
        self.rows = rows
        self.available = available


def _parse_partitions(text: str) -> tuple[_Partition, ...]:
    """Parse /proc/partitions. Returns the row tuple.

    First line is a column header (``major minor #blocks name``).
    We identify it by its first token being ``major`` rather than
    an integer and skip it. Each data row has exactly 4
    whitespace-separated tokens. Lines whose first three columns
    aren't integers are dropped (mid-update read or future format
    change).
    """
    rows: list[_Partition] = []
    for raw_line in text.splitlines():
        tokens = raw_line.split()
        if len(tokens) != 4:
            continue
        # Header sniff: first token isn't an integer.
        try:
            major = int(tokens[0])
            minor = int(tokens[1])
            blocks_1k = int(tokens[2])
        except ValueError:
            continue
        rows.append(
            _Partition(
                major=major,
                minor=minor,
                blocks_1k=blocks_1k,
                name=tokens[3],
            )
        )
    return tuple(rows)


def _capture(*, path: Path = _PARTITIONS_PATH) -> _PartitionsSnapshot:
    """Read /proc/partitions + build snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _PartitionsSnapshot(rows=(), available=False)
    return _PartitionsSnapshot(rows=_parse_partitions(text), available=True)


def _fmt_bytes(n: int) -> str:
    """Compact human bytes. Same shape as the slabinfo formatter —
    intentional duplication: every card stays self-contained so a
    refactor can move them independently."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MiB"
    if n < 1024 * 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024 * 1024):.2f} GiB"
    return f"{n / (1024 * 1024 * 1024 * 1024):.2f} TiB"


def _render(snap: _PartitionsSnapshot) -> str:
    lines = ["💽 <b>Block-device inventory (/proc/partitions)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/partitions unavailable — Linux-only surface "
            "(macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        lines.append(
            "  <i>No block devices reported — extremely unusual; the "
            "kernel was built without block-device support.</i>"
        )
        return "\n".join(lines)

    total_bytes = sum(r.bytes_total for r in snap.rows)
    whole_disk_count = sum(1 for r in snap.rows if r.is_whole_disk)
    partition_count = len(snap.rows) - whole_disk_count

    lines.append(
        f"  <b>Devices reported:</b> <code>{len(snap.rows)}</code> "
        f"(<code>{whole_disk_count}</code> whole-disk-like, "
        f"<code>{partition_count}</code> partition-like)"
    )
    lines.append(f"  <b>Total block-device capacity:</b> <code>{_fmt_bytes(total_bytes)}</code>")
    lines.append("")
    lines.append(f"  <b>Top <code>{_TOP_N}</code> devices by raw size:</b>")

    top = sorted(snap.rows, key=lambda r: r.bytes_total, reverse=True)[:_TOP_N]
    for row in top:
        kind = "disk" if row.is_whole_disk else "part"
        lines.append(
            f"  • <code>{row.name}</code> "
            f"<i>{kind}</i> "
            f"({row.major}:{row.minor}) "
            f"<code>{_fmt_bytes(row.bytes_total)}</code>"
        )

    truncated = len(snap.rows) - len(top)
    if truncated > 0:
        lines.append(
            f"  <i>… {truncated} smaller devices not shown (cap <code>{_TOP_N}</code>).</i>"
        )

    lines.append("")
    lines.append(
        "<i>No warning markers by design — partition existence isn't "
        "a problem signal. Unfamiliar names cross-reference: "
        "<code>dm-N</code> = device-mapper (LVM / dmcrypt), "
        "<code>loop-N</code> = file-backed loop mount, "
        "<code>zram-N</code> = compressed RAM block device. Compare "
        "with /admin_diskstats for activity, /admin_disk for "
        "filesystem usage, /admin_mounts for the mount table.</i>"
    )
    return "\n".join(lines)


async def handle_admin_partitions(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_partitions; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        device_count=len(snap.rows),
        whole_disks=sum(1 for r in snap.rows if r.is_whole_disk),
        total_bytes=sum(r.bytes_total for r in snap.rows),
    ).info("/admin_partitions rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.partitions")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_partitions(message, settings)

    router.message.register(_entry, Command("admin_partitions", ignore_case=True))
    return router
