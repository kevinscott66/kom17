"""``/admin_consoles`` — registered kernel consoles from /proc/consoles.

The existing kernel/boot-side surface (``/admin_cmdline``,
``/admin_kernel``, ``/admin_loguru``) reports what the operator
configured. None of them surfaces **where kernel printk output is
actually going right now**. That matters because:

* **Silent log loss.** If no console has the ``E`` (enabled) flag,
  kernel printk has nowhere to go — panic traces, oops splats,
  driver warnings vanish. The host is one bug away from a
  diagnosis blackout. This is the single ⚠ predicate.
* **Serial-console diagnostics.** ``ttyS0`` (or virtio's
  ``hvc0``) being registered AND enabled is the difference
  between "we can grab the panic log from IPMI / hypervisor"
  and "we can't". Operator inheriting a host needs to confirm.
* **Boot-console lingering.** The ``B`` flag means a console
  registered very early in boot (earlycon, simple-framebuffer).
  These are normally auto-disabled when the real console takes
  over. A lingering ``B`` after full boot is unusual and worth
  surfacing — operator can decide.

Format is a small fixed-column line per registered console.
From kernel ``printk/printk.c`` ``console_show``::

    tty0                 -WU (EC p  )    4:1
    ttyS0                -W- (E  p a)    4:64

Columns: ``name+index``, three-char write/unused/blank field,
parenthesised flag set, ``major:minor``. Stable since the
modern console registration code landed (~2.6).

The parenthesised flags are what we care about. Kernel encoding:
``E`` enabled, ``C`` consdev (preferred), ``B`` boot (auto-off
after takeover), ``p`` printbuffer, ``a`` panic-write, ``b``
bracket, ``u`` unblanked. We surface them verbatim and decode the
operationally important ones (E, C, B) into a per-line tag.

⚠ predicate: single. Zero enabled consoles. Pinned with explicit
must-not-fire test on the canonical at-least-one-enabled sample —
cry-wolf guard because the whole point of the card is this single
signal.

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


log = logger.bind(component="handlers.admin.consoles")


_CONSOLES_PATH = Path("/proc/consoles")


class _Console:
    """One registered kernel console.

    ``flags_raw`` is the parenthesised token (without parens) as
    kernel emitted it — we surface it verbatim because future
    kernels may add letters and forward-compat matters more than
    a pre-decoded dict. The booleans below are precomputed for
    the three operationally-critical flags so render doesn't
    re-scan."""

    __slots__ = (
        "device",
        "enabled",
        "flags_raw",
        "is_boot",
        "is_preferred",
        "name",
    )

    def __init__(
        self,
        *,
        name: str,
        device: str,
        flags_raw: str,
    ) -> None:
        self.name = name
        self.device = device
        self.flags_raw = flags_raw
        self.enabled = "E" in flags_raw
        self.is_preferred = "C" in flags_raw
        self.is_boot = "B" in flags_raw


class _ConsolesSnapshot:
    """Captured /proc/consoles.

    ``consoles`` — every parsed registration.
    ``available`` — False on macOS / non-procfs.
    """

    __slots__ = ("available", "consoles")

    def __init__(self, *, consoles: tuple[_Console, ...], available: bool) -> None:
        self.consoles = consoles
        self.available = available

    @property
    def enabled_count(self) -> int:
        return sum(1 for c in self.consoles if c.enabled)


def _parse_consoles(text: str) -> tuple[_Console, ...]:
    """Parse /proc/consoles.

    Each non-empty line: ``name <wuB> (flags) major:minor``.
    We split on whitespace; the flags are the parenthesised
    token. Robust to extra whitespace and to lines we don't
    understand (we drop them — defensive against a future kernel
    column change rather than crashing render).
    """
    consoles: list[_Console] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Need: name, three-char field, (flags), major:minor.
        # We find the parenthesised token explicitly — the
        # 3-char field can contain '-' which would otherwise
        # confuse a positional split.
        lparen = line.find("(")
        rparen = line.find(")", lparen + 1)
        if lparen == -1 or rparen == -1:
            continue
        name_part = line[:lparen].split()
        tail = line[rparen + 1 :].split()
        if not name_part or not tail:
            continue
        flags_raw = line[lparen + 1 : rparen].replace(" ", "")
        consoles.append(
            _Console(
                name=name_part[0],
                device=tail[-1],
                flags_raw=flags_raw,
            )
        )
    return tuple(consoles)


def _capture(*, path: Path = _CONSOLES_PATH) -> _ConsolesSnapshot:
    """Read /proc/consoles + build snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _ConsolesSnapshot(consoles=(), available=False)
    return _ConsolesSnapshot(consoles=_parse_consoles(text), available=True)


def _tags(console: _Console) -> str:
    """Render the operationally-relevant flags as compact tags.
    Returns empty string if no notable flag is set — the verbatim
    flags_raw is still rendered separately so nothing is lost."""
    parts: list[str] = []
    if console.enabled:
        parts.append("enabled")
    else:
        parts.append("DISABLED")
    if console.is_preferred:
        parts.append("preferred")
    if console.is_boot:
        parts.append("boot")
    return ", ".join(parts)


def _render(snap: _ConsolesSnapshot) -> str:
    lines = ["📟 <b>Kernel consoles (/proc/consoles)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/consoles unavailable — Linux-only surface "
            "(macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.consoles:
        lines.append(
            "  <i>No consoles registered — kernel printk has nowhere "
            "to go. Panic traces / oops splats / driver warnings are "
            "lost.</i> ⚠"
        )
        return "\n".join(lines)

    enabled = snap.enabled_count
    warn_marker = " ⚠" if enabled == 0 else ""
    lines.append(
        f"  <b>Registered:</b> <code>{len(snap.consoles)}</code>  "
        f"<b>enabled:</b> <code>{enabled}</code>{warn_marker}"
    )
    lines.append("")

    for c in snap.consoles:
        lines.append(
            f"  • <code>{c.name}</code> "
            f"(<code>{c.device}</code>, flags=<code>{c.flags_raw}</code>) — "
            f"{_tags(c)}"
        )

    lines.append("")
    if enabled == 0:
        lines.append(
            "<i>⚠ Zero enabled consoles. Kernel printk output has no "
            "sink — panic traces, oops splats, driver warnings vanish "
            "silently. Most common cause: <code>quiet</code> + console "
            "misconfiguration on the bootloader. Compare with "
            "/admin_cmdline.</i>"
        )
    else:
        lines.append(
            "<i>No warnings — at least one console is enabled, so "
            "kernel printk output has a sink. The <code>B</code> "
            "(boot) tag on a console after full boot usually means "
            "earlycon hasn't been handed off; harmless but worth "
            "noting on long-lived hosts.</i>"
        )
    return "\n".join(lines)


async def handle_admin_consoles(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_consoles; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        total=len(snap.consoles),
        enabled=snap.enabled_count,
    ).info("/admin_consoles rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.consoles")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_consoles(message, settings)

    router.message.register(_entry, Command("admin_consoles", ignore_case=True))
    return router
