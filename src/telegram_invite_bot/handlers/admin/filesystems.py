"""``/admin_filesystems`` — kernel-registered FS types from /proc/filesystems.

The mount/storage surface tells the operator what is *currently
mounted* (/admin_mounts), what block devices exist
(/admin_partitions), and how busy each mount is (/admin_disk,
/admin_diskstats). None of them answers a different question:
**what filesystem drivers does this kernel know about**, i.e.
what *could* be mounted right now without modprobing anything.

Operationally distinct uses:

* **Container hosts.** Containers need overlayfs (overlay) for
  layered images. If a host is missing it (older kernel, custom
  build), every container build silently falls back to a much
  slower copy-driver — visible only via this card.
* **Debug-mount diagnosis.** A failed ``mount -t fuse`` /
  ``mount -t cifs`` on the operator's shell is almost always a
  missing FS driver. /proc/filesystems shows whether the module
  is loaded; /admin_modules names what's available to be loaded.
* **Forensics.** "Was tmpfs available on this kernel?" — for
  reasoning about /tmp / /run behaviour during an incident.

Format is one of the simplest in /proc: each line is exactly two
whitespace-separated columns. First is either the literal
``nodev`` (pseudo-filesystem — no block device required, e.g.
sysfs, tmpfs, proc, cgroup2) or absent / empty (block-backed FS
like ext4, xfs, btrfs). Second is the FS name. Lines with the
wrong shape are dropped (mid-update read or future format
change). The format has been stable since 2.0.x — there's no
header line and no version preamble.

The card splits the rendered list into two groups (pseudo vs.
block-backed) because that's the question the operator usually
has: "is overlay there?" → block-backed list. "is cgroup2
there?" → pseudo list. We don't sort within group — kernel file
order tracks registration order, which is itself a weak forensic
signal (early-registered drivers are baseline, late ones tend to
be modules loaded by udev / systemd).

⚠ predicate: zero by design. Same posture as /admin_modules and
the rest of the inventory surface — driver presence is operator-
policy, not a problem signal.

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


log = logger.bind(component="handlers.admin.filesystems")


_FILESYSTEMS_PATH = Path("/proc/filesystems")


class _FsEntry:
    """One row of /proc/filesystems.

    ``name`` is the FS name (ext4, tmpfs, overlay, …).
    ``requires_device`` flips: True for block-backed FS that need
    a real device (ext4 / xfs / btrfs / vfat), False for
    pseudo-FS that don't (tmpfs / sysfs / proc / cgroup2). The
    kernel encodes that by *omission* of ``nodev`` — block-backed
    rows start with whitespace, pseudo rows start with the
    literal token ``nodev``. We lift it to a predicate at parse
    time so the render path is straightforward.
    """

    __slots__ = ("name", "requires_device")

    def __init__(self, *, name: str, requires_device: bool) -> None:
        self.name = name
        self.requires_device = requires_device


class _FilesystemsSnapshot:
    """Captured /proc/filesystems.

    ``rows`` — every parsed entry, in file order (kernel
    registration order; weak forensic signal worth preserving).
    ``available`` — False on macOS / non-procfs.
    """

    __slots__ = ("available", "rows")

    def __init__(self, *, rows: tuple[_FsEntry, ...], available: bool) -> None:
        self.rows = rows
        self.available = available

    @property
    def block_backed_count(self) -> int:
        return sum(1 for r in self.rows if r.requires_device)

    @property
    def pseudo_count(self) -> int:
        return sum(1 for r in self.rows if not r.requires_device)


def _parse_filesystems(text: str) -> tuple[_FsEntry, ...]:
    """Parse /proc/filesystems. Returns the row tuple.

    The kernel emits exactly two whitespace-separated columns per
    line — leading column either ``nodev`` (pseudo) or absent
    (block-backed), trailing column is the FS name. We sniff via
    column count after split: 1 token = block-backed (the
    leading whitespace stripped), 2 tokens with first == ``nodev``
    = pseudo. Any other shape is dropped (defensive).
    """
    rows: list[_FsEntry] = []
    for raw_line in text.splitlines():
        tokens = raw_line.split()
        if len(tokens) == 1:
            # Block-backed FS: line was "<tab>ext4" → split() ate
            # the leading whitespace and we're left with just the
            # name.
            rows.append(_FsEntry(name=tokens[0], requires_device=True))
        elif len(tokens) == 2 and tokens[0] == "nodev":
            rows.append(_FsEntry(name=tokens[1], requires_device=False))
        # Anything else: drop. /proc/filesystems doesn't emit
        # other shapes today; a future format change shouldn't
        # crash the card.
    return tuple(rows)


def _capture(*, path: Path = _FILESYSTEMS_PATH) -> _FilesystemsSnapshot:
    """Read /proc/filesystems + build snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _FilesystemsSnapshot(rows=(), available=False)
    return _FilesystemsSnapshot(rows=_parse_filesystems(text), available=True)


def _render(snap: _FilesystemsSnapshot) -> str:
    lines = ["🗂️ <b>Kernel filesystem drivers (/proc/filesystems)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/filesystems unavailable — Linux-only surface "
            "(macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        lines.append(
            "  <i>No filesystem drivers reported — extremely unusual; "
            "the kernel would not be able to mount anything.</i>"
        )
        return "\n".join(lines)

    block_backed = [r for r in snap.rows if r.requires_device]
    pseudo = [r for r in snap.rows if not r.requires_device]

    lines.append(
        f"  <b>Drivers registered:</b> <code>{len(snap.rows)}</code> "
        f"(<code>{len(block_backed)}</code> block-backed, "
        f"<code>{len(pseudo)}</code> pseudo)"
    )
    lines.append("")

    lines.append("  <b>Block-backed</b> (need a device, e.g. ext4 on /dev/sdaN):")
    if block_backed:
        # Two columns per render line for compactness — these are
        # short tokens (≤8 chars typically) and grouping them keeps
        # the card readable on mobile.
        for entry in block_backed:
            lines.append(f"    • <code>{entry.name}</code>")
    else:
        lines.append(
            "    <i>(none — the kernel has no block-FS drivers, "
            "unusual on a general-purpose host)</i>"
        )
    lines.append("")

    lines.append("  <b>Pseudo</b> (no device, e.g. tmpfs / proc / cgroup2):")
    for entry in pseudo:
        lines.append(f"    • <code>{entry.name}</code>")

    lines.append("")
    lines.append(
        "<i>No warning markers — driver presence is operator-policy, "
        "not a problem signal. Container hosts want <code>overlay</code> "
        "in the block-backed list; FUSE mounts want <code>fuse</code> "
        "in pseudo. Compare with /admin_modules to see if a missing "
        "driver is loadable as a kernel module, /admin_mounts for "
        "what's currently mounted, /admin_partitions for the device "
        "side.</i>"
    )
    return "\n".join(lines)


async def handle_admin_filesystems(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_filesystems; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        total=len(snap.rows),
        block_backed=snap.block_backed_count,
        pseudo=snap.pseudo_count,
    ).info("/admin_filesystems rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.filesystems")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_filesystems(message, settings)

    router.message.register(_entry, Command("admin_filesystems", ignore_case=True))
    return router
