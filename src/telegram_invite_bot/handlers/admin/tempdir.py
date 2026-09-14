"""``/admin_tempdir`` — resolved tempdir + writability + free-space.

Complements /admin_disk (configured-directory headroom) by surfacing
the **other** directory the bot writes to without explicit config:
the tempdir picked by :func:`tempfile.gettempdir` according to the
TMPDIR / TEMP / TMP env-var precedence and the platform fallback.

Why an operator wants this:

* Image generation (Pillow's ``Image.save`` to a tempfile, PDF
  rendering, stats-card composites) all hit the tempdir. ``/admin_-
  disk`` covers ``database/`` and ``logs/`` but is blind to where
  ``tempfile.NamedTemporaryFile()`` actually lands — and on a host
  with a tmpfs ``/tmp`` (default systemd config), the tempdir lives
  in RAM and shares the page cache with everything else. The free-
  space number on the tempdir is therefore a different physical
  number than what /admin_disk reports for the project root.
* Some deploys put TMPDIR on a tiny /var partition; an "out of
  disk space" failure during image render with otherwise-healthy
  /admin_disk readings is exactly the surprise this card catches.
* Writability is not free either — some hardened systemd units set
  ``ReadOnlyPaths=/tmp`` or ``PrivateTmp=true`` (which gives the
  process a fresh tempdir under ``/tmp/systemd-private-…``).
  Whether the bot can actually write to its resolved tempdir is
  a one-touch check that doesn't require shelling in.
* Cross-validates the TMPDIR / TEMP / TMP env vars an operator
  might have set. /admin_locale surfaces LANG; this surfaces the
  tempdir-affecting env vars so an operator who tweaked them in
  the systemd unit can confirm the change actually took effect.

Silent-drop for non-devs, private-only at the router level. Same
posture as every other ``/admin_*``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.tempdir")


# Env vars that influence tempfile.gettempdir, in the precedence
# CPython documents (and that gettempdir actually uses). Rendering
# them lets an operator see which one is winning — a TMPDIR set
# in the systemd unit but a TEMP also set in the shell environment
# would otherwise be ambiguous from outside the process.
_TEMPDIR_ENV_VARS: tuple[str, ...] = ("TMPDIR", "TEMP", "TMP")


# Free-space threshold below which we mark ⚠. 100 MiB is generous —
# image rendering and PDF composition rarely need more than a few
# MiB of scratch space, but a 100 MiB cushion gives headroom for
# concurrent renders and any chunked log rotation that lands in
# the tempdir. A bot host hitting this is one bad day from EIO on
# the next render.
_FREE_BYTES_CONCERNING = 100 * 1024 * 1024


class _TempdirSnapshot:
    """Captured tempdir state.

    ``resolved`` is what :func:`tempfile.gettempdir` returned —
    we keep it as :class:`pathlib.Path` so the renderer can use
    ``.exists()`` / ``.is_dir()`` without re-converting. ``env_vars``
    is the dict of TMPDIR/TEMP/TMP → value-or-unset, evaluated at
    capture time so the render is a pure function of the snapshot.

    ``writable`` is the result of an actual write probe (create +
    delete a small tempfile) rather than ``os.access(W_OK)`` —
    ``os.access`` lies under SELinux / AppArmor / ACLs more often
    than it tells the truth, and a probe-write is the only reliable
    "can the bot actually write here" answer.
    """

    __slots__ = (
        "env_vars",
        "free_bytes",
        "resolved",
        "total_bytes",
        "usage_available",
        "writable",
        "writable_error",
    )

    def __init__(
        self,
        *,
        resolved: Path,
        env_vars: dict[str, str | None],
        writable: bool,
        writable_error: str | None,
        free_bytes: int,
        total_bytes: int,
        usage_available: bool,
    ) -> None:
        self.resolved = resolved
        self.env_vars = env_vars
        self.writable = writable
        self.writable_error = writable_error
        self.free_bytes = free_bytes
        self.total_bytes = total_bytes
        self.usage_available = usage_available


def _probe_writable(path: Path) -> tuple[bool, str | None]:
    """Create + delete a tempfile in ``path``. Returns (ok, err_msg).

    A real syscall is the only reliable answer — SELinux/AppArmor
    can refuse a write that ``os.access`` says is permitted, and a
    full filesystem can refuse one that the permission bits allow.
    The probe uses :func:`tempfile.NamedTemporaryFile` rather than
    a hand-rolled ``open(..., "w")`` so we exercise the same code
    path the actual image-rendering does.

    On error we capture the exception class name (not the message,
    which on some platforms includes the full path and is noisy).
    The class name is enough to route the diagnosis: PermissionError
    → AppArmor/perms, OSError → ENOSPC/EROFS, etc.
    """
    try:
        with tempfile.NamedTemporaryFile(dir=str(path), prefix="admin_tempdir_probe_"):
            pass
    except OSError as exc:
        return False, type(exc).__name__
    except Exception as exc:  # noqa: BLE001 - defensive; classify any failure
        return False, type(exc).__name__
    return True, None


def _capture(tempdir_path: str | None = None) -> _TempdirSnapshot:
    """Sample tempdir state. ``tempdir_path`` is for tests.

    At runtime the default ``None`` triggers a real
    :func:`tempfile.gettempdir` call; in tests we pass a tmp_path
    to exercise the snapshot/render pipeline without touching the
    real /tmp.

    Disk-usage failure (the tempdir is on a filesystem
    :func:`shutil.disk_usage` can't stat — extremely rare, but
    possible on certain FUSE mounts) flips ``usage_available=False``
    so the renderer doesn't print a misleading "0 bytes free".
    """
    resolved = Path(tempdir_path) if tempdir_path is not None else Path(tempfile.gettempdir())

    env_vars: dict[str, str | None] = {name: os.environ.get(name) for name in _TEMPDIR_ENV_VARS}

    writable, writable_error = _probe_writable(resolved)

    try:
        usage = shutil.disk_usage(str(resolved))
        free_bytes = usage.free
        total_bytes = usage.total
        usage_available = True
    except OSError:
        free_bytes = 0
        total_bytes = 0
        usage_available = False

    return _TempdirSnapshot(
        resolved=resolved,
        env_vars=env_vars,
        writable=writable,
        writable_error=writable_error,
        free_bytes=free_bytes,
        total_bytes=total_bytes,
        usage_available=usage_available,
    )


def _fmt_mib(b: int) -> str:
    return f"{b / (1024 * 1024):.1f} MiB"


def _free_concerning(snap: _TempdirSnapshot) -> bool:
    """``True`` if free space is below the threshold AND known.

    Missing usage info (usage_available=False) is informational —
    we can't claim "low free space" if we can't read the number.
    Mirrors the protective-flag posture from /admin_memory's
    missing-VmSwap branch.
    """
    if not snap.usage_available:
        return False
    return snap.free_bytes < _FREE_BYTES_CONCERNING


def _render(snap: _TempdirSnapshot) -> str:
    lines = ["📁 <b>tempfile.gettempdir()</b>", ""]
    lines.append(f"  • <b>resolved:</b> <code>{snap.resolved}</code>")

    write_marker = " ⚠" if not snap.writable else ""
    if snap.writable:
        lines.append("  • <b>writable:</b> <code>yes</code>")
    else:
        err = snap.writable_error or "unknown"
        lines.append(f"  • <b>writable:</b> <code>no</code> <i>({err})</i>{write_marker}")

    if snap.usage_available:
        free_marker = " ⚠" if _free_concerning(snap) else ""
        lines.append(
            f"  • <b>free:</b> <code>{_fmt_mib(snap.free_bytes)}</code> "
            f"of <code>{_fmt_mib(snap.total_bytes)}</code>"
            f"{free_marker}"
        )
    else:
        lines.append(
            "  • <b>free:</b> <code>unavailable</code> <i>(disk_usage failed — FUSE / sandbox?)</i>"
        )

    lines.append("")
    lines.append("  <b>env vars (precedence: TMPDIR → TEMP → TMP):</b>")
    for name in _TEMPDIR_ENV_VARS:
        value = snap.env_vars.get(name)
        if value is None:
            lines.append(f"    • <code>{name}</code>: <i>unset</i>")
        else:
            lines.append(f"    • <code>{name}</code>: <code>{value}</code>")

    lines.append("")
    lines.append(
        f"<i>⚠ markers: tempdir not writable (probe-write failed — "
        f"AppArmor/SELinux/ENOSPC routing via error-class name), "
        f"or free space below "
        f"{_FREE_BYTES_CONCERNING // (1024 * 1024)} MiB (image "
        f"render headroom — cross-check with /admin_disk for the "
        f"configured-dir headroom on the same filesystem).</i>"
    )
    return "\n".join(lines)


async def handle_admin_tempdir(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_tempdir; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_tempdir rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.tempdir")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_tempdir(message, settings)

    router.message.register(_entry, Command("admin_tempdir", ignore_case=True))
    return router
