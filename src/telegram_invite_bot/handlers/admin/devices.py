"""``/admin_devices`` — registered char/block device drivers from /proc/devices.

The existing device-side surface explains *runtime usage*:
``/admin_disk`` for free space, ``/admin_diskstats`` for I/O,
``/admin_mounts`` for mount table, ``/admin_partitions`` for the
block-device inventory at the partition level. None of them
surfaces the **driver registration table** — which major numbers
the kernel has accepted via ``register_chrdev`` /
``register_blkdev``. That matters because:

* **Driver presence is the precondition for everything else.**
  Before a /dev node can be opened, the kernel has to have the
  corresponding driver registered. Operator inheriting a host
  needs to confirm — e.g. ``tun`` driver for WireGuard /
  OpenVPN, ``nvme`` driver for NVMe SSDs, ``loop`` for
  containerised storage, ``device-mapper`` for LVM / dm-crypt.
* **The split between char and block matters.** Same major
  number can be used in both spaces. We surface the two
  sections separately because the operator's mental model
  separates them too.
* **Drift between built-in vs module-loaded drivers.** /proc/devices
  shows what's CURRENTLY registered — a driver that should be
  built-in but only appears after modprobe is a configuration
  smell. Pairs with /admin_modules for the kernel-module side.

We don't pre-decode "what each major means" — the kernel /
distro mapping is documented in /usr/include/linux/major.h
and `Documentation/admin-guide/devices.txt`, and an operator
who needs it has those references. We surface the verbatim list
sorted by major number per section, plus the headline counts.

Format is two labelled sections with a blank line between them::

    Character devices:
      1 mem
      4 tty
      4 ttyS
      ...

    Block devices:
    259 blkext
      7 loop
      ...

Stable since pre-2.4. Each line is ``<major> <name>`` with
leading whitespace. We tokenise on whitespace, take the first
two tokens. Section is determined by the most recent header.

⚠ predicate: zero. There's no portable "expected driver set"
across kernels (a hardened embedded build legitimately lacks
``tun``; a desktop legitimately has dozens of drivers a server
doesn't). Same posture as /admin_cmdline — surface verbatim,
operator decides.

Same wiring as every other admin card — silent-drop, private-only,
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


log = logger.bind(component="handlers.admin.devices")


_DEVICES_PATH = Path("/proc/devices")


class _Device:
    """One registered driver: (major, name) inside a section."""

    __slots__ = ("major", "name")

    def __init__(self, *, major: int, name: str) -> None:
        self.major = major
        self.name = name


class _DevicesSnapshot:
    """Captured /proc/devices.

    ``char`` — registered character drivers (in file order, which
    is major-ascending in practice).
    ``block`` — registered block drivers.
    ``available`` — False on macOS / non-procfs.
    """

    __slots__ = ("available", "block", "char")

    def __init__(
        self,
        *,
        char: tuple[_Device, ...],
        block: tuple[_Device, ...],
        available: bool,
    ) -> None:
        self.char = char
        self.block = block
        self.available = available


def _parse_devices(text: str) -> tuple[tuple[_Device, ...], tuple[_Device, ...]]:
    """Parse the two-section /proc/devices format.

    Section header determines which list a line lands in. Lines
    that aren't valid ``<int> <name>`` are dropped defensively —
    a future kernel adding an extra section header would
    otherwise become a phantom driver.
    """
    char: list[_Device] = []
    block: list[_Device] = []
    target: list[_Device] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("character devices"):
            target = char
            continue
        if lower.startswith("block devices"):
            target = block
            continue
        if target is None:
            # Data before any section header — skip defensively.
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        major_str, name = parts
        try:
            major = int(major_str)
        except ValueError:
            continue
        target.append(_Device(major=major, name=name))

    return tuple(char), tuple(block)


def _capture(*, path: Path = _DEVICES_PATH) -> _DevicesSnapshot:
    """Read /proc/devices + build snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _DevicesSnapshot(char=(), block=(), available=False)
    char, block = _parse_devices(text)
    return _DevicesSnapshot(char=char, block=block, available=True)


def _render_section(label: str, devices: tuple[_Device, ...]) -> list[str]:
    """Render one section. We cap at 40 entries per section —
    a modern desktop host registers 50+ char drivers; surfacing
    every one buries the operator. The cap is generous enough
    that a server's full list fits."""
    out: list[str] = []
    out.append(f"  <b>{label}</b> (<code>{len(devices)}</code> registered):")
    if not devices:
        out.append("    <i>(none)</i>")
        return out
    cap = 40
    for d in devices[:cap]:
        out.append(f"    • <code>{d.major:>3}</code> <code>{d.name}</code>")
    if len(devices) > cap:
        out.append(f"    <i>… {len(devices) - cap} more.</i>")
    return out


def _render(snap: _DevicesSnapshot) -> str:
    lines = ["🧷 <b>Registered device drivers (/proc/devices)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/devices unavailable — Linux-only surface "
            "(macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.char and not snap.block:
        lines.append(
            "  <i>No drivers registered — extremely stripped kernel "
            "or parser saw a format we don't recognise.</i>"
        )
        return "\n".join(lines)

    lines.append(
        f"  <b>Character drivers:</b> <code>{len(snap.char)}</code>  "
        f"<b>Block drivers:</b> <code>{len(snap.block)}</code>"
    )
    lines.append("")
    lines.extend(_render_section("Character devices", snap.char))
    lines.append("")
    lines.extend(_render_section("Block devices", snap.block))

    lines.append("")
    lines.append(
        "<i>No warnings — the kernel's notion of an 'expected' driver "
        "set varies wildly across distros, embedded builds and "
        "container hosts. Operator confirms a specific driver is "
        "present by name (e.g. <code>tun</code> for WireGuard, "
        "<code>nvme</code> for NVMe, <code>device-mapper</code> for "
        "LVM / dm-crypt). Pair with /admin_modules for the "
        "kernel-module side.</i>"
    )
    return "\n".join(lines)


async def handle_admin_devices(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_devices; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        char_count=len(snap.char),
        block_count=len(snap.block),
    ).info("/admin_devices rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.devices")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_devices(message, settings)

    router.message.register(_entry, Command("admin_devices", ignore_case=True))
    return router
