"""``/admin_kernel`` — kernel version + boot-cmdline snapshot.

Complements /admin_hostinfo (node identity), /admin_cpu (CPU
capacity) and /admin_signals (POSIX-handler state) with the
**kernel-side** view: what kernel version is actually running and
what command-line parameters it was booted with. The two questions
operators actually have:

* "Are we on a vulnerable kernel?" — ``/proc/version`` is the
  authoritative answer (``uname -r`` is the same string from
  userspace). When a CVE drops naming kernel ranges (every other
  month at this point), the operator wants to confirm the
  production box is or isn't in the vulnerable window WITHOUT
  shell access. ``platform.release()`` is the same value but the
  full /proc/version line also surfaces the build host, build
  timestamp and gcc version, which are useful for cross-deploy
  consistency checks.
* "What kernel options were set?" — ``/proc/cmdline`` is the exact
  string the bootloader passed. Operationally-relevant tokens that
  silently change behaviour:
    - ``isolcpus=`` / ``nohz_full=`` — cores carved out of the
      scheduler. Pairs with /admin_cpu's affinity narrowed ⚠.
    - ``transparent_hugepage=never`` — kills THP overhead but
      also kills any benefit for large allocations (Python's
      arena allocator can benefit).
    - ``swapaccount=0`` — disables per-cgroup swap accounting.
      A bot that's been migrated between hosts with and without
      this set behaves differently under memory pressure.
    - ``mitigations=off`` — disables Spectre/Meltdown/L1TF mitigations.
      Big throughput win on trusted-tenant hosts, security disaster
      on multi-tenant. ⚠ if present.

Linux-only at the read layer. macOS / Windows take the unavailable
branch and the card surfaces "unavailable" with a pointer to
platform-specific tooling (``sw_vers`` on Darwin, ``systeminfo`` on
Windows).
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


log = logger.bind(component="handlers.admin.kernel")


# Cmdline tokens we treat as security-relevant. Surfaced with ⚠
# when present so the operator notices them without parsing the
# whole cmdline string. The set is deliberately small: a giant
# list would lose triage signal. Each entry is the EXACT token
# string an operator would grep for.
_CONCERNING_CMDLINE_TOKENS: tuple[str, ...] = (
    "mitigations=off",
    "nosmt",  # may be benign on hardened hosts, may be a tuning hack
)


class _KernelSnapshot:
    """Captured /proc/version + /proc/cmdline.

    Both fields are raw text — the renderer truncates long cmdlines
    rather than the capturer, because the operator may still want
    to see a long string if they invoke the card; capturing the
    full text and letting render decide is the same posture we use
    on /admin_pythonpath.
    """

    __slots__ = ("available", "cmdline", "version")

    def __init__(self, *, available: bool, version: str = "", cmdline: str = "") -> None:
        self.available = available
        self.version = version
        self.cmdline = cmdline


# Telegram message limit is 4096 chars. We cap the cmdline at 600
# chars: a normal Linux cmdline is well under that, but some
# initramfs / hardened-server configs can push past 2 kB and would
# blow the card on their own.
_MAX_CMDLINE_LEN = 600


def _capture(
    version_path: Path = Path("/proc/version"),
    cmdline_path: Path = Path("/proc/cmdline"),
) -> _KernelSnapshot:
    """Read /proc/version and /proc/cmdline.

    Parameterised for test fixtures. The two reads are independent;
    one succeeding while the other fails is unusual but possible
    (e.g. hardened kernel that hides cmdline). We take the
    "available" path only if both reads succeed — a half-card is
    misleading; if we can't trust /proc, we surface that uniformly.
    """
    try:
        version = version_path.read_text().strip()
        cmdline = cmdline_path.read_text().strip()
    except OSError:
        return _KernelSnapshot(available=False)
    return _KernelSnapshot(available=True, version=version, cmdline=cmdline)


def _cmdline_concerns(cmdline: str) -> list[str]:
    """Return tokens from ``_CONCERNING_CMDLINE_TOKENS`` present in ``cmdline``.

    The check is whole-token (split on whitespace) rather than
    substring — ``nosmt`` matches the kernel param but not a hypothetical
    ``nosmtp_enabled=1`` token in a future cmdline. Same posture as the
    ⚠-bearing tokens in /admin_warnings.
    """
    tokens = set(cmdline.split())
    return [t for t in _CONCERNING_CMDLINE_TOKENS if t in tokens]


def _render(snap: _KernelSnapshot) -> str:
    lines = ["🐧 <b>Kernel</b>", ""]
    if not snap.available:
        lines.append(
            "  <code>unavailable</code> "
            "<i>(/proc/version or /proc/cmdline not readable — non-Linux host?)</i>"
        )
        lines.append("")
        lines.append(
            "<i>Linux-only diagnostic. On macOS use sw_vers + sysctl "
            "kern.bootargs; on Windows use systeminfo / bcdedit /enum.</i>"
        )
        return "\n".join(lines)

    lines.append(f"  • <b>version:</b> <code>{snap.version}</code>")

    cmdline_concerns = _cmdline_concerns(snap.cmdline)
    cmdline_display = snap.cmdline
    if len(cmdline_display) > _MAX_CMDLINE_LEN:
        cmdline_display = cmdline_display[:_MAX_CMDLINE_LEN] + "… <i>(truncated)</i>"
    lines.append(f"  • <b>cmdline:</b> <code>{cmdline_display}</code>")

    if cmdline_concerns:
        lines.append("")
        lines.append("  <b>cmdline ⚠:</b>")
        for token in cmdline_concerns:
            lines.append(f"    • <code>{token}</code>")

    lines.append("")
    lines.append(
        "<i>⚠ tokens flag boot params that change runtime behaviour "
        "silently — mitigations=off (Spectre/Meltdown disabled), "
        "nosmt (hyperthreading off). Cross-check /admin_cpu's "
        "affinity and load average for the runtime consequences.</i>"
    )
    return "\n".join(lines)


async def handle_admin_kernel(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_kernel; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_kernel rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.kernel")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_kernel(message, settings)

    router.message.register(_entry, Command("admin_kernel", ignore_case=True))
    return router
