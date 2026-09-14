"""``/admin_misc`` — misc char devices (major 10) from /proc/misc.

Linux assigns major number 10 to a grab-bag of unrelated tiny
character devices that don't justify their own major: ``kvm``,
``fuse``, ``hwrng``, ``loop-control``, ``net/tun``, ``watchdog``,
``cpu_dma_latency``, ``rfkill``, ``vhost-net``, etc. /proc/misc
is the registration table for this bucket — minor number plus
driver name per line.

Why this matters operationally — /admin_devices surfaces the
*major-number* registration table; misc-major (10) consolidates
~30+ drivers behind a single entry there. Without /admin_misc
the operator can't tell from inside Telegram whether ``kvm`` is
available (virtualisation host?), ``fuse`` is registered (any
sshfs / overlayfs-via-fuse?), ``net/tun`` is present (WireGuard
prerequisite — pairs with /admin_devices' ``tun`` entry), or
``hwrng`` is loaded (kernel entropy source, pairs with
/admin_random).

This card is the missing piece between /admin_devices (driver
*classes*) and /admin_mounts (filesystem *use*).

Format is the simplest in /proc — one line per registration,
``<minor> <name>`` with leading whitespace::

     60 cpu_dma_latency
    130 watchdog
    237 loop-control
    228 hwrng
    229 fuse
    232 kvm

Stable since 2.4. Order is registration order (most recent
last) — we preserve it; sorting by minor would imply a
significance the kernel doesn't intend.

⚠ predicate: zero. There is no universal "expected misc-driver
set" — a sealed VM legitimately lacks ``kvm``; a build host
legitimately lacks ``net/tun``. Same posture as /admin_devices
and /admin_cmdline: surface verbatim, operator decides. The
card's value is presence-confirmation by name, not a derived
warning.

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


log = logger.bind(component="handlers.admin.misc")


_MISC_PATH = Path("/proc/misc")


class _MiscEntry:
    """One misc-major (10) registration: (minor, name)."""

    __slots__ = ("minor", "name")

    def __init__(self, *, minor: int, name: str) -> None:
        self.minor = minor
        self.name = name


class _MiscSnapshot:
    """Captured /proc/misc.

    ``entries`` — parsed registrations in file order.
    ``available`` — False on macOS / non-procfs.
    """

    __slots__ = ("available", "entries")

    def __init__(self, *, entries: tuple[_MiscEntry, ...], available: bool) -> None:
        self.entries = entries
        self.available = available


def _parse_misc(text: str) -> tuple[_MiscEntry, ...]:
    """Parse /proc/misc.

    Lines that don't tokenise to ``<int> <name>`` are dropped
    defensively — silent acceptance of a malformed line could
    synthesise a minor=0 phantom and confuse presence checks.
    """
    entries: list[_MiscEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        minor_str, name = parts
        try:
            minor = int(minor_str)
        except ValueError:
            continue
        entries.append(_MiscEntry(minor=minor, name=name))
    return tuple(entries)


def _capture(*, path: Path = _MISC_PATH) -> _MiscSnapshot:
    """Read /proc/misc + build snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _MiscSnapshot(entries=(), available=False)
    return _MiscSnapshot(entries=_parse_misc(text), available=True)


def _render(snap: _MiscSnapshot) -> str:
    lines = ["🧮 <b>Misc char devices (/proc/misc, major 10)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/misc unavailable — Linux-only surface "
            "(macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.entries:
        lines.append(
            "  <i>No misc drivers registered — extremely stripped "
            "kernel. fuse / kvm / net/tun / watchdog all unavailable.</i>"
        )
        return "\n".join(lines)

    lines.append(f"  <b>Registered:</b> <code>{len(snap.entries)}</code>")
    lines.append("")
    # Sort by name for scannability — operator usually looks up
    # a specific driver by name (kvm? fuse? tun?), not by minor.
    for entry in sorted(snap.entries, key=lambda e: e.name):
        lines.append(f"    • <code>{entry.minor:>4}</code> <code>{entry.name}</code>")

    lines.append("")
    lines.append(
        "<i>No warnings — the expected misc-driver set varies "
        "wildly (sealed VM, build host, embedded). The card's "
        "value is presence-confirmation by name: <code>kvm</code> "
        "for virt hosts, <code>net/tun</code> as WireGuard / "
        "OpenVPN prerequisite, <code>fuse</code> for userspace FS, "
        "<code>hwrng</code> for hardware entropy (pairs with "
        "/admin_random), <code>kvm</code> + <code>vhost-net</code> "
        "for accelerated guests.</i>"
    )
    return "\n".join(lines)


async def handle_admin_misc(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_misc; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        entry_count=len(snap.entries),
    ).info("/admin_misc rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.misc")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_misc(message, settings)

    router.message.register(_entry, Command("admin_misc", ignore_case=True))
    return router
