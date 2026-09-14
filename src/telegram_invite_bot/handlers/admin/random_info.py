"""``/admin_random`` — entropy / CSPRNG-source posture.

Complements /admin_hashlib (which primitives are wired) and
/admin_ssl (which TLS-version flags + trust store) by surfacing
the **randomness side** of the runtime: which CSPRNG sources are
available, do they actually produce output, and (on Linux) is
the kernel's entropy pool initialized.

Why an operator wants this:

* Webhook secret rotation, FSM session tokens, marriage-proposal
  codes — every "random-looking ID" the bot mints flows through
  ``secrets.token_bytes`` / ``os.urandom``. If those silently
  return predictable bytes (a misconfigured chroot without
  ``/dev/urandom``, a CI container without ``getrandom``), the
  bot is shipping guessable tokens without raising any error —
  the worst class of security regression because it's silent.
* Boot-time entropy starvation. On a freshly-booted VM or
  container, ``getrandom(2)`` can block briefly until the kernel
  RNG is seeded. Modern (post-5.18) kernels block this at the
  syscall boundary; older ones return weak bytes. The
  ``entropy_avail`` reading from ``/proc/sys/kernel/random``
  gives the operator a yes-no read.
* Sanity probe. We actually CALL ``secrets.token_bytes(32)`` and
  ``os.urandom(32)`` and confirm the returned bytes (a) are the
  right length, (b) are not all-zero. A trivial probe but it
  catches the case where the system call exists but returns junk
  (some hardened-kernel modes do exactly this).

Posture: silent-drop for non-devs, private-only at the router
level. The probe consumes ~64 bytes of entropy per call; that's
a rounding error against the kernel's continuous reseed loop.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.random_info")


# /proc/sys/kernel/random/entropy_avail floor below which we ⚠.
# Modern kernels (5.18+) gate getrandom on a 256-bit seed and then
# report ``entropy_avail`` as a near-fixed value (~256 on Linux);
# truly low readings (< 64) typically mean a non-Linux host where
# the file doesn't exist or a freshly-booted VM. We treat anything
# explicitly < 64 as a ⚠ — generous enough that the post-init
# steady-state never fires, but tight enough to catch a stuck pool.
_ENTROPY_CONCERNING_FLOOR = 64

# /dev/urandom path. Used for explicit-presence + readability check.
# os.urandom() on CPython routes through getrandom(2) which doesn't
# need the device node, but pre-3.6 fallback paths and some
# subprocess libraries DO read /dev/urandom directly — a missing
# node is a deferred-bug risk we want to surface.
_URANDOM_DEVICE = Path("/dev/urandom")

# Linux entropy file. Non-Linux hosts will not have this; we treat
# absent-on-non-Linux as informational, absent-on-Linux as ⚠.
_ENTROPY_AVAIL_FILE = Path("/proc/sys/kernel/random/entropy_avail")

# Number of bytes we sample from each source. 32 is conventional
# CSPRNG output size (256 bits) and matches what ``secrets`` uses
# internally for token defaults.
_SAMPLE_BYTES = 32


class _RandomSnapshot:
    """Captured CSPRNG-source posture.

    ``urandom_ok`` / ``secrets_ok`` are tri-state: ``True`` means
    the call returned the expected length AND non-trivial bytes;
    ``False`` means the call raised; ``None`` means we couldn't
    even attempt it (won't happen on CPython but kept for shape
    parity with other admin cards).
    """

    __slots__ = (
        "device_present",
        "device_readable",
        "entropy_avail",
        "entropy_file_present",
        "os_urandom_error",
        "os_urandom_ok",
        "secrets_error",
        "secrets_ok",
    )

    def __init__(
        self,
        *,
        os_urandom_ok: bool,
        os_urandom_error: str | None,
        secrets_ok: bool,
        secrets_error: str | None,
        device_present: bool,
        device_readable: bool,
        entropy_file_present: bool,
        entropy_avail: int | None,
    ) -> None:
        self.os_urandom_ok = os_urandom_ok
        self.os_urandom_error = os_urandom_error
        self.secrets_ok = secrets_ok
        self.secrets_error = secrets_error
        self.device_present = device_present
        self.device_readable = device_readable
        self.entropy_file_present = entropy_file_present
        self.entropy_avail = entropy_avail


def _looks_like_csprng_output(b: bytes) -> bool:
    """Cheap sanity: right length and not all-zero.

    A full statistical test is out of scope — we're catching the
    "system call exists but returns junk" failure mode, not
    auditing the CSPRNG itself. All-zero bytes from a 32-byte read
    is a vanishingly improbable real outcome (2^-256) so any actual
    occurrence is a smoking-gun bug.
    """
    return len(b) == _SAMPLE_BYTES and any(byte != 0 for byte in b)


def _probe_os_urandom() -> tuple[bool, str | None]:
    try:
        sample = os.urandom(_SAMPLE_BYTES)
    except OSError as exc:
        return False, type(exc).__name__
    if not _looks_like_csprng_output(sample):
        return False, "non-CSPRNG-shaped output"
    return True, None


def _probe_secrets() -> tuple[bool, str | None]:
    try:
        sample = secrets.token_bytes(_SAMPLE_BYTES)
    except OSError as exc:
        return False, type(exc).__name__
    if not _looks_like_csprng_output(sample):
        return False, "non-CSPRNG-shaped output"
    return True, None


def _read_entropy_avail(
    path: Path = _ENTROPY_AVAIL_FILE,
) -> tuple[bool, int | None]:
    """Return ``(file_present, value_or_None)``.

    Missing file is normal on non-Linux; render branches on
    ``file_present``. Parse errors on Linux yield ``(True, None)``
    so the operator sees the file exists but couldn't be read —
    different signal than "not on Linux".
    """
    if not path.exists():
        return False, None
    try:
        return True, int(path.read_text().strip())
    except (OSError, ValueError):
        return True, None


def _capture(
    *,
    urandom_device: Path = _URANDOM_DEVICE,
    entropy_file: Path = _ENTROPY_AVAIL_FILE,
) -> _RandomSnapshot:
    os_ok, os_err = _probe_os_urandom()
    sec_ok, sec_err = _probe_secrets()
    dev_present = urandom_device.exists()
    # Defensive: ``os.access(R_OK)`` returns False for missing files
    # already, but we split the signals for the rendered card so the
    # operator sees the precise failure shape.
    dev_readable = dev_present and os.access(urandom_device, os.R_OK)
    file_present, entropy = _read_entropy_avail(entropy_file)
    return _RandomSnapshot(
        os_urandom_ok=os_ok,
        os_urandom_error=os_err,
        secrets_ok=sec_ok,
        secrets_error=sec_err,
        device_present=dev_present,
        device_readable=dev_readable,
        entropy_file_present=file_present,
        entropy_avail=entropy,
    )


def _entropy_concerning(snap: _RandomSnapshot) -> bool:
    """⚠ only when the file is present (i.e. we're on Linux and
    SHOULD have a real reading) AND the value is explicitly below
    the floor. Missing-on-non-Linux is informational, not a ⚠ —
    same cry-wolf posture as every other admin card."""
    if not snap.entropy_file_present or snap.entropy_avail is None:
        return False
    return snap.entropy_avail < _ENTROPY_CONCERNING_FLOOR


def _render(snap: _RandomSnapshot) -> str:
    lines = ["🎲 <b>CSPRNG posture</b>", ""]

    # Source probes — these are the load-bearing signals.
    lines.append("  <b>source probes:</b>")
    if snap.os_urandom_ok:
        lines.append(f"    • <code>os.urandom({_SAMPLE_BYTES})</code> <i>ok</i>")
    else:
        lines.append(
            f"    • <code>os.urandom({_SAMPLE_BYTES})</code> "
            f"<i>failed ({snap.os_urandom_error})</i> ⚠"
        )
    if snap.secrets_ok:
        lines.append(f"    • <code>secrets.token_bytes({_SAMPLE_BYTES})</code> <i>ok</i>")
    else:
        lines.append(
            f"    • <code>secrets.token_bytes({_SAMPLE_BYTES})</code> "
            f"<i>failed ({snap.secrets_error})</i> ⚠"
        )

    lines.append("")
    lines.append("  <b>/dev/urandom:</b>")
    if not snap.device_present:
        lines.append("    • <i>not present</i> ⚠")
    elif not snap.device_readable:
        lines.append("    • <i>present but not readable by this process</i> ⚠")
    else:
        lines.append("    • <i>present + readable</i>")

    lines.append("")
    lines.append("  <b>kernel entropy:</b>")
    if not snap.entropy_file_present:
        lines.append(
            "    • <i>/proc/sys/kernel/random/entropy_avail not present "
            "(non-Linux host — informational only)</i>"
        )
    elif snap.entropy_avail is None:
        lines.append("    • <i>file present but unreadable / unparseable</i> ⚠")
    else:
        warn = " ⚠" if _entropy_concerning(snap) else ""
        lines.append(f"    • <code>entropy_avail = {snap.entropy_avail}</code>{warn}")

    lines.append("")
    lines.append(
        f"<i>⚠ markers: a CSPRNG source failed or returned non-CSPRNG-"
        f"shaped output (this is the silent-bad-tokens failure mode — "
        f"cross-check /admin_hashlib + /admin_ssl), /dev/urandom is "
        f"missing or unreadable (chroot / locked-down container), or "
        f"<code>entropy_avail &lt; {_ENTROPY_CONCERNING_FLOOR}</code> "
        f"(freshly-booted VM, stuck pool). Modern Linux kernels (5.18+) "
        f"gate <code>getrandom(2)</code> on a real seed, so steady-state "
        f"readings of ~256 are normal.</i>"
    )
    return "\n".join(lines)


async def handle_admin_random(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_random; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        os_urandom_ok=snap.os_urandom_ok,
        secrets_ok=snap.secrets_ok,
        entropy_avail=snap.entropy_avail,
    ).info("/admin_random rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.random")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_random(message, settings)

    router.message.register(_entry, Command("admin_random", ignore_case=True))
    return router
