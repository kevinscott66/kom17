"""``/admin_cmdline`` — kernel boot parameters from /proc/cmdline.

The existing kernel-side surface explains *current state*:
/admin_kernel for build info, /admin_sysctl for runtime knobs,
/admin_meminfo for memory accounting. None of them surfaces the
**boot-time parameter list** — the arguments the bootloader
passed to the kernel, which determine fundamental behaviour the
running system can't change without a reboot.

Why this matters operationally:

* **CPU mitigations.** ``mitigations=off`` disables Spectre /
  Meltdown / MDS / Retbleed countermeasures for performance.
  An operator inheriting a host needs to know — security audits
  ask, and the host's actual security posture is materially
  different. ``nosmt`` disables HyperThreading, halving CPU
  count.  ``isolcpus=N`` reserves cores from the scheduler.
* **Memory layout.** ``hugepagesz=`` / ``hugepages=`` allocate
  huge pages at boot. ``mem=`` caps RAM. ``numa=off`` disables
  NUMA awareness entirely.
* **Debugging knobs.** ``debug``, ``loglevel=`` change dmesg
  verbosity. ``init=`` overrides /sbin/init.
* **IOMMU / virtualization.** ``intel_iommu=on`` /
  ``amd_iommu=on``, ``kvm-intel.nested=1``.

We don't try to *enumerate* every notable flag — the kernel docs
list hundreds. Instead we surface the raw cmdline verbatim
(operator reads it themselves) and *highlight* a curated set of
parameters that materially affect the host's security or
performance posture. The highlights are an explicit allowlist:
adding to it is intentional, missing items aren't a bug. Same
posture as /admin_envscan (highlight a known set, surface the
rest as-is).

Format is the simplest in /proc — single line, no newline
required, parameters are space-separated. Each token is either
``key=value`` or a bare flag. We tokenise and check membership
against the highlight set. Values with embedded spaces don't
exist in /proc/cmdline (the bootloader doesn't quote), so plain
``split()`` is right.

⚠ predicate: zero. ``mitigations=off`` is a legitimate
performance choice in many environments (sealed VM, isolated
hardware, build host). We don't presume — surface the value,
operator decides. Same posture as /admin_kernel.

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


log = logger.bind(component="handlers.admin.cmdline")


_CMDLINE_PATH = Path("/proc/cmdline")


# Parameters worth pulling into a "notable" section. Two shapes:
#
# - ``key=value`` form: the *key* is what we match; the value is
#   surfaced as-is. Operator reads the value and decides.
# - bare-flag form: the whole token is the match.
#
# Adding to this set is intentional — a curated subset that's
# materially security- or performance-relevant. Anything not here
# still shows up in the raw cmdline section below; the highlight
# is for readability, not gatekeeping.
_NOTABLE_KEYS = frozenset(
    {
        # CPU security / scheduling
        "mitigations",
        "spectre_v2",
        "spec_store_bypass_disable",
        "isolcpus",
        "nohz",
        "nohz_full",
        "rcu_nocbs",
        # Memory
        "hugepagesz",
        "hugepages",
        "mem",
        "numa",
        "transparent_hugepage",
        # IOMMU / virt
        "intel_iommu",
        "amd_iommu",
        "iommu",
        "kvm-intel.nested",
        "kvm-amd.nested",
        # Boot / init
        "init",
        "root",
        "ro",
        "rw",
        "quiet",
        "loglevel",
        "debug",
        # Networking-relevant
        "ipv6.disable",
    }
)


# Bare flags (no ``=``) that should also surface in the notable
# section. Subset of _NOTABLE_KEYS where the token has no value.
_NOTABLE_BARE = frozenset({"ro", "rw", "quiet", "debug", "nosmt"})


class _CmdlineSnapshot:
    """Captured /proc/cmdline.

    ``raw`` — verbatim text (one line, trailing newline stripped).
    ``params`` — parsed token list. Each entry is a ``(key,
    value)`` tuple where ``value`` is None for bare flags. We
    preserve order — kernel cmdline order is itself meaningful
    (later params can override earlier ones).
    ``available`` — False on macOS / non-procfs.
    """

    __slots__ = ("available", "params", "raw")

    def __init__(
        self,
        *,
        raw: str,
        params: tuple[tuple[str, str | None], ...],
        available: bool,
    ) -> None:
        self.raw = raw
        self.params = params
        self.available = available


def _parse_cmdline(text: str) -> tuple[tuple[str, str | None], ...]:
    """Parse /proc/cmdline into ``(key, value)`` tuples.

    Empty value (``key=``) is kept as ``(key, "")`` rather than
    collapsed to a bare flag — the kernel treats ``key=`` and
    ``key`` differently in a few places (transparent_hugepage=
    isn't the same as transparent_hugepage), and the operator
    deserves to see the distinction.

    Tokens with embedded ``=`` past the first are split on the
    first only, so ``BOOT_IMAGE=/boot/vmlinuz-6.5.0-26-generic``
    surfaces correctly as key=BOOT_IMAGE,
    value=/boot/vmlinuz-6.5.0-26-generic.
    """
    parts: list[tuple[str, str | None]] = []
    for token in text.split():
        if "=" in token:
            key, _, value = token.partition("=")
            parts.append((key, value))
        else:
            parts.append((token, None))
    return tuple(parts)


def _capture(*, path: Path = _CMDLINE_PATH) -> _CmdlineSnapshot:
    """Read /proc/cmdline + build snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _CmdlineSnapshot(raw="", params=(), available=False)
    raw = text.strip()
    return _CmdlineSnapshot(
        raw=raw,
        params=_parse_cmdline(raw),
        available=True,
    )


def _format_param(key: str, value: str | None) -> str:
    """Format a single ``(key, value)`` for the notable section.
    Bare flags render as just ``flag``; key=value pairs render as
    ``key=<code>value</code>``."""
    if value is None:
        return f"<code>{key}</code>"
    return f"<code>{key}={value}</code>"


def _render(snap: _CmdlineSnapshot) -> str:
    lines = ["🥾 <b>Kernel boot parameters (/proc/cmdline)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/cmdline unavailable — Linux-only surface "
            "(macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.raw:
        lines.append(
            "  <i>Empty cmdline — extremely unusual; the bootloader "
            "passed no arguments to the kernel.</i>"
        )
        return "\n".join(lines)

    # Notable section: parameters from the curated set, preserving
    # cmdline order.
    notable = [
        (k, v)
        for (k, v) in snap.params
        if (v is not None and k in _NOTABLE_KEYS) or (v is None and k in _NOTABLE_BARE)
    ]

    lines.append(
        f"  <b>Parameters parsed:</b> <code>{len(snap.params)}</code> "
        f"(<code>{len(notable)}</code> notable)"
    )
    lines.append("")

    if notable:
        lines.append(
            "  <b>Notable parameters</b> (curated subset — security / performance / boot posture):"
        )
        for key, value in notable:
            lines.append(f"    • {_format_param(key, value)}")
    else:
        lines.append(
            "  <i>No parameters from the curated notable set — host "
            "is using defaults for everything the card checks.</i>"
        )

    lines.append("")
    lines.append("  <b>Raw cmdline:</b>")
    lines.append(f"  <code>{snap.raw}</code>")

    lines.append("")
    lines.append(
        "<i>No warning markers — boot-time choices like "
        "<code>mitigations=off</code> are legitimate in many "
        "environments (sealed VMs, isolated hardware, build hosts). "
        "Operator policy decides whether the inventory matches the "
        "host's intended posture. Compare with /admin_kernel for "
        "build flags and /admin_sysctl for runtime knobs.</i>"
    )
    return "\n".join(lines)


async def handle_admin_cmdline(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_cmdline; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    notable_count = sum(
        1
        for (k, v) in snap.params
        if (v is not None and k in _NOTABLE_KEYS) or (v is None and k in _NOTABLE_BARE)
    )
    log.bind(
        user_id=user.id,
        available=snap.available,
        total_params=len(snap.params),
        notable_params=notable_count,
    ).info("/admin_cmdline rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.cmdline")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_cmdline(message, settings)

    router.message.register(_entry, Command("admin_cmdline", ignore_case=True))
    return router
