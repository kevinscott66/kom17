"""``/admin_mounts`` — filesystem mount table from /proc/self/mounts.

The mount table answers a class of questions that no other card
in this directory does:

* Is ``/tmp`` mounted ``noexec``? (kills tempfile-spawned binaries.)
* Is ``/var/log`` on a separate filesystem? (relevant for /admin_disk
  free-space readings — same path on the same fs is one number.)
* Did the rootfs get remounted ``ro`` after a disk error? (kernel
  failsafe, silent w.r.t. application logs — bot keeps running
  while writes silently fail.)
* What kind of filesystem backs the database directory? (tmpfs
  means data evaporates at reboot — surprisingly common in dev
  containers).

We render the full table rather than curating — operators reach
for ``/admin_mounts`` precisely when they don't already know which
mount they need to inspect, so trimming would defeat the purpose.
The table can be long (50–200 lines on a container host), but
Telegram's 4096-char limit is the natural cap; we truncate with an
explicit "N entries hidden" marker rather than silently dropping.

Cry-wolf posture: a single ⚠ on the *rootfs readonly* signal. A
healthy Linux system has ``/`` mounted rw; ``ro`` on rootfs means
either the kernel remounted it after an I/O error (panic-on-error
disabled) or the operator booted in single-user / rescue mode and
forgot to remount. Either way the bot will silently fail every
write — that's the unambiguous "something is very wrong" signal.

Per-mount ``ro`` is NOT ⚠'d — many legitimate mounts are
intentionally readonly (squashfs, configmap volumes in k8s,
overlay lowerdirs). Per-mount ``noexec``/``nosuid``/``nodev`` are
also not ⚠'d — they're security hardening, not faults.

Forward-compat: /proc/self/mounts format has been stable since
~Linux 2.4 (six space-separated columns), but we accept short
lines without raising — degrade-don't-crash. Mount options field
is comma-separated arbitrary tokens; we keep the raw string AND a
parsed flag-set, so future filesystems with novel option syntax
don't break our predicates.

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


log = logger.bind(component="handlers.admin.mounts")


_MOUNTS_PATH = Path("/proc/self/mounts")


# Cap on rendered rows. /proc/self/mounts on a container host can
# carry 100+ lines (overlay, cgroup v1 controllers, /run binds, …);
# Telegram caps text at 4096 chars. Pick a row budget that keeps
# the card under that limit with a comfortable margin.
_RENDER_ROW_CAP = 40


class _MountRow:
    """One /proc/self/mounts entry.

    /proc/self/mounts format (man proc(5)): ``device mountpoint
    fstype options dump pass``. We keep the first four — the last
    two are zero-only fields preserved from /etc/fstab parity and
    have no operator value.

    * ``options_raw`` — kept verbatim so the operator sees the same
      thing ``cat /proc/mounts`` would show.
    * ``options`` — parsed frozenset of comma-separated tokens
      (``ro``, ``rw``, ``noexec``, ``nosuid``, ``nodev``, …) for
      predicate use.
    """

    __slots__ = ("device", "fstype", "mountpoint", "options", "options_raw")

    def __init__(
        self,
        *,
        device: str,
        mountpoint: str,
        fstype: str,
        options_raw: str,
        options: frozenset[str],
    ) -> None:
        self.device = device
        self.mountpoint = mountpoint
        self.fstype = fstype
        self.options_raw = options_raw
        self.options = options


class _MountsSnapshot:
    """Captured mount table.

    * ``rows`` — every parsed mount entry, in /proc order (which is
      mount order — earliest-mounted first, so / is row 0 on every
      sane Linux).
    * ``available`` — False when /proc/self/mounts is unreadable
      (macOS dev, container without /proc, permission error).
      Render the absence rather than pretending we have data.
    """

    __slots__ = ("available", "rows")

    def __init__(
        self,
        *,
        rows: tuple[_MountRow, ...],
        available: bool,
    ) -> None:
        self.rows = rows
        self.available = available


def _parse_mounts(text: str) -> tuple[_MountRow, ...]:
    """Parse /proc/self/mounts.

    Fields are space-separated, but the kernel octal-escapes
    whitespace inside mountpoint / device names (a mountpoint with
    a literal space becomes ``\\040``). We deliberately do NOT
    decode those — the operator wants the exact bytes the kernel
    sees, and decoding could ambiguate against malicious names.
    The few cases where this matters (a usb stick named ``My
    Drive``) are obvious-on-sight.
    """
    rows: list[_MountRow] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 4:
            # Short line — degrade rather than crash. Should never
            # happen on a real kernel but we'd rather see partial
            # output than no card at all.
            continue
        device, mountpoint, fstype, options_raw = parts[0], parts[1], parts[2], parts[3]
        # Options can be ``rw,relatime,errors=remount-ro,data=ordered``;
        # we want the bare flag set, so keep the values too (the
        # ``errors=remount-ro`` token doesn't break anything by
        # being in the set — it just makes flag membership tests
        # exact-match-only).
        options = frozenset(opt for opt in options_raw.split(",") if opt)
        rows.append(
            _MountRow(
                device=device,
                mountpoint=mountpoint,
                fstype=fstype,
                options_raw=options_raw,
                options=options,
            )
        )
    return tuple(rows)


def _capture(*, path: Path = _MOUNTS_PATH) -> _MountsSnapshot:
    """Read /proc/self/mounts + build a snapshot.

    Keyword-only path parameter so tests can inject a tmp file —
    same hermetic-fixture pattern as every other diagnostic card.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _MountsSnapshot(rows=(), available=False)
    return _MountsSnapshot(rows=_parse_mounts(text), available=True)


def _rootfs_readonly(snap: _MountsSnapshot) -> bool:
    """⚠ predicate — rootfs mount is readonly.

    Healthy Linux has ``/`` mounted rw. ``ro`` on rootfs means
    either the kernel auto-remounted after an I/O error (common
    failure mode on dying disks) or the operator booted into
    single-user mode and forgot to remount. Either way every write
    silently fails — exactly the kind of state operators stare at
    logs for hours not understanding.

    Returns False if no rootfs row is present (degraded snapshot
    or non-Linux). Absence of data is NOT a warning — same
    cry-wolf-prevention posture as every other card.
    """
    for row in snap.rows:
        if row.mountpoint == "/":
            return "ro" in row.options
    return False


def _render(snap: _MountsSnapshot) -> str:
    lines = ["💾 <b>Mount table (/proc/self/mounts)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/self/mounts unavailable on this host — "
            "Linux-only card. macOS + non-procfs containers will "
            "see this.</i>"
        )
        return "\n".join(lines)

    if not snap.rows:
        # /proc/self/mounts is readable but empty — extremely
        # unusual but possible inside a chroot with private mount
        # namespace. Render explicitly so the absence doesn't look
        # like a parser bug.
        lines.append("  <i>mount table is empty — likely a private mount namespace or chroot.</i>")
        return "\n".join(lines)

    rootfs_ro = _rootfs_readonly(snap)
    if rootfs_ro:
        # Hoist the warning ABOVE the table — operator's eye lands
        # at the top, and a 40-row scroll could otherwise bury it.
        lines.append(
            "  ⚠ <b>rootfs is mounted readonly</b> — every write "
            "will silently fail. Likely causes: kernel remount "
            "after I/O error, rescue/single-user boot. Investigate "
            "before the operator next deploys."
        )
        lines.append("")

    shown = snap.rows[:_RENDER_ROW_CAP]
    hidden = len(snap.rows) - len(shown)

    for row in shown:
        # Keep each entry to two lines max — mountpoint+fstype on
        # the headline, options on the second. Longer mountpoints
        # (overlay upperdir paths) wrap rather than truncating;
        # truncation would hide exactly the field the operator
        # came here to read.
        flags = []
        if "ro" in row.options:
            flags.append("ro")
        if "rw" in row.options:
            flags.append("rw")
        if "noexec" in row.options:
            flags.append("noexec")
        if "nosuid" in row.options:
            flags.append("nosuid")
        if "nodev" in row.options:
            flags.append("nodev")
        flags_str = " ".join(flags) if flags else "(no flags)"
        lines.append(
            f"  • <code>{row.mountpoint}</code> <i>({row.fstype})</i> — <code>{flags_str}</code>"
        )

    if hidden > 0:
        lines.append("")
        lines.append(
            f"  <i>{hidden} additional mount(s) hidden — Telegram "
            f"text cap. Total mounts: {len(snap.rows)}.</i>"
        )

    lines.append("")
    lines.append(
        "<i>⚠ markers: only on rootfs (mountpoint &quot;/&quot;) "
        "mounted readonly. Per-mount ro/noexec/nosuid/nodev are "
        "shown as flags but NOT ⚠'d — many legitimate mounts are "
        "intentionally readonly (squashfs, configmaps, overlay "
        "lowerdirs) and the security flags are hardening, not "
        "faults.</i>"
    )
    return "\n".join(lines)


async def handle_admin_mounts(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_mounts; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        mount_count=len(snap.rows),
        rootfs_readonly=_rootfs_readonly(snap),
    ).info("/admin_mounts rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.mounts")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_mounts(message, settings)

    router.message.register(_entry, Command("admin_mounts", ignore_case=True))
    return router
