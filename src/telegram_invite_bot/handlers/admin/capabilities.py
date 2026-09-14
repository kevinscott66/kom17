"""``/admin_capabilities`` — Linux capability bitmap of this process.

Complements /admin_oom (kernel-side kill priority), /admin_signals
(signal disposition) and /admin_envscan (credential env-var
audit) by surfacing the **Linux capabilities posture**: which
privileged operations is this process actually allowed to perform.

Why an operator wants this:

* Privilege-audit. A correctly-configured bot needs almost no
  capabilities — typically zero, possibly CAP_NET_BIND_SERVICE
  if it listens on a low port (it doesn't; nginx fronts the
  webhook). Running with extra caps is a deferred-bug risk: any
  future code path that decides to use them will silently
  succeed in a way that a non-root, no-cap process would have
  caught at the syscall boundary.
* "Is the bot running as root?" — a full capability set
  (0x1ffffffffff or similar) is the unambiguous root-or-equivalent
  signal. Operators sometimes set up the systemd unit with
  ``User=root`` for convenience during initial deploy and forget
  to switch back. ⚠'d so it's visible.
* CAP_SYS_ADMIN is the catch-all "I can do anything" cap; any
  process holding it has effectively bypassed every other
  permission check. Always ⚠'d separately because the symptom
  ("permission errors keep going away when I add caps") tends to
  drift toward this cap as the path of least resistance.
* Effective vs Permitted vs Inheritable vs Bounding vs Ambient —
  Linux has five sets and the distinctions matter (Effective is
  "what I can do right now", Permitted is "what I can re-acquire
  if I drop and re-add", Bounding is "the ceiling — what
  execve(2) can ever grant"). The card surfaces all five so the
  operator's mental model matches the kernel's.

Posture: silent-drop for non-devs, private-only at the router
level. Linux-only — non-Linux hosts render informational rather
than ⚠. One small file read (/proc/self/status), parses the
Cap{Inh,Prm,Eff,Bnd,Amb} hex lines.
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


log = logger.bind(component="handlers.admin.capabilities")


_PROC_STATUS = Path("/proc/self/status")


# Linux capability name table, indexed by bit position. The list
# is the union of capabilities defined in include/uapi/linux/
# capability.h through Linux 6.x; gaps are filled with ``UNKNOWN_<n>``
# so a future kernel addition surfaces visibly rather than as a
# bare bit position. Order matches the kernel's CAP_* enum.
_CAPABILITY_NAMES: tuple[str, ...] = (
    "CAP_CHOWN",
    "CAP_DAC_OVERRIDE",
    "CAP_DAC_READ_SEARCH",
    "CAP_FOWNER",
    "CAP_FSETID",
    "CAP_KILL",
    "CAP_SETGID",
    "CAP_SETUID",
    "CAP_SETPCAP",
    "CAP_LINUX_IMMUTABLE",
    "CAP_NET_BIND_SERVICE",
    "CAP_NET_BROADCAST",
    "CAP_NET_ADMIN",
    "CAP_NET_RAW",
    "CAP_IPC_LOCK",
    "CAP_IPC_OWNER",
    "CAP_SYS_MODULE",
    "CAP_SYS_RAWIO",
    "CAP_SYS_CHROOT",
    "CAP_SYS_PTRACE",
    "CAP_SYS_PACCT",
    "CAP_SYS_ADMIN",
    "CAP_SYS_BOOT",
    "CAP_SYS_NICE",
    "CAP_SYS_RESOURCE",
    "CAP_SYS_TIME",
    "CAP_SYS_TTY_CONFIG",
    "CAP_MKNOD",
    "CAP_LEASE",
    "CAP_AUDIT_WRITE",
    "CAP_AUDIT_CONTROL",
    "CAP_SETFCAP",
    "CAP_MAC_OVERRIDE",
    "CAP_MAC_ADMIN",
    "CAP_SYSLOG",
    "CAP_WAKE_ALARM",
    "CAP_BLOCK_SUSPEND",
    "CAP_AUDIT_READ",
    "CAP_PERFMON",
    "CAP_BPF",
    "CAP_CHECKPOINT_RESTORE",
)


# /proc/self/status lines we extract. The names match the kernel
# labels exactly so the parsing stays trivial; the rendered card
# uses friendlier labels.
_CAP_FIELDS: tuple[str, ...] = (
    "CapInh",
    "CapPrm",
    "CapEff",
    "CapBnd",
    "CapAmb",
)


# CAP_SYS_ADMIN bit position (per the table above) — the catch-all
# cap that effectively bypasses every other permission check.
# Surfaced as its own ⚠ row when set in CapEff.
_CAP_SYS_ADMIN_BIT = _CAPABILITY_NAMES.index("CAP_SYS_ADMIN")


# Full root-equivalent bitmap on a recent kernel. The exact value
# depends on the kernel version (each new capability shifts this
# up by one bit), so we compute it dynamically from the name table
# rather than hard-coding 0x1ffffffffff. A CapEff equal to or
# greater than this means the process has every named capability.
_FULL_CAPABILITY_BITMAP = (1 << len(_CAPABILITY_NAMES)) - 1


class _CapsSnapshot:
    """Captured capability posture.

    ``sets`` maps field-name (CapInh, …) to its integer bitmap.
    Missing fields render as None — typically only when /proc/
    self/status doesn't exist (non-Linux) or one of the fields
    isn't in the file (very old kernel without ambient caps).
    """

    __slots__ = ("sets", "status_present")

    def __init__(self, *, sets: dict[str, int | None], status_present: bool) -> None:
        self.sets = sets
        self.status_present = status_present


def _parse_status(text: str) -> dict[str, int | None]:
    """Parse the Cap{Inh,Prm,Eff,Bnd,Amb} lines from
    /proc/self/status. Each is rendered by the kernel as a fixed-
    width 16-char hex string after the field name + colon.

    Defensive on the parse: unparseable hex yields None for that
    field rather than raising, so a future kernel format change
    degrades gracefully.
    """
    result: dict[str, int | None] = {name: None for name in _CAP_FIELDS}
    for line in text.splitlines():
        for name in _CAP_FIELDS:
            prefix = f"{name}:"
            if line.startswith(prefix):
                hex_part = line[len(prefix) :].strip()
                try:
                    result[name] = int(hex_part, 16)
                except ValueError:
                    result[name] = None
                break
    return result


def _capture(*, status_path: Path = _PROC_STATUS) -> _CapsSnapshot:
    if not status_path.exists():
        return _CapsSnapshot(
            sets={name: None for name in _CAP_FIELDS},
            status_present=False,
        )
    try:
        text = status_path.read_text()
    except OSError as exc:
        log.bind(path=str(status_path), error=type(exc).__name__).warning(
            "failed to read /proc/self/status"
        )
        return _CapsSnapshot(
            sets={name: None for name in _CAP_FIELDS},
            status_present=False,
        )
    return _CapsSnapshot(sets=_parse_status(text), status_present=True)


def _decode_bitmap(bitmap: int) -> list[str]:
    """Return the human-readable capability names set in ``bitmap``.

    Bits beyond the name table are bucketed under ``UNKNOWN_<n>``
    so a kernel addition surfaces as a visible row rather than
    being silently dropped. Output is sorted by bit position
    (i.e. kernel-canonical order, not alphabetical) for stable
    diffing across snapshots.
    """
    names: list[str] = []
    bit = 0
    remaining = bitmap
    while remaining:
        if remaining & 1:
            if bit < len(_CAPABILITY_NAMES):
                names.append(_CAPABILITY_NAMES[bit])
            else:
                names.append(f"UNKNOWN_{bit}")
        remaining >>= 1
        bit += 1
    return names


def _is_root_equivalent(bitmap: int) -> bool:
    """Mask CapEff with the full-cap bitmap; if every named cap is
    set, the process is root-equivalent."""
    return (bitmap & _FULL_CAPABILITY_BITMAP) == _FULL_CAPABILITY_BITMAP


def _has_sys_admin(bitmap: int) -> bool:
    """CAP_SYS_ADMIN is the catch-all cap. Separately ⚠'d because
    its presence is the path-of-least-resistance security failure
    mode (operator added it to fix one specific error, never
    removed it)."""
    return bool(bitmap & (1 << _CAP_SYS_ADMIN_BIT))


def _format_set(label: str, bitmap: int | None) -> list[str]:
    """Render one capability set as 1-3 lines.

    Empty bitmap renders as a single "none" line — the healthy
    case and we want it visually distinct from "lots". Non-empty
    renders the hex + the decoded names; the names are the actual
    operational signal.
    """
    if bitmap is None:
        return [f"    • <b>{label}:</b> <i>not in status file</i>"]
    if bitmap == 0:
        return [f"    • <b>{label}:</b> <code>0</code> <i>(none)</i>"]
    names = _decode_bitmap(bitmap)
    lines = [f"    • <b>{label}:</b> <code>0x{bitmap:016x}</code> <i>({len(names)} caps)</i>"]
    # Cap rendered name count to keep the card under 4096 chars.
    # A full root bitmap is ~40 names; even rendered verbosely
    # that's manageable, so we don't truncate by default — only
    # if something pathological appears.
    if len(names) > 20:
        lines.append(f"      <i>{', '.join(names[:20])} … and {len(names) - 20} more</i>")
    else:
        lines.append(f"      <i>{', '.join(names)}</i>")
    return lines


def _render(snap: _CapsSnapshot) -> str:
    lines = ["🔐 <b>Linux capabilities</b>", ""]

    if not snap.status_present:
        lines.append(
            "  <i>/proc/self/status not present — non-Linux host or "
            "restricted namespace. Capability posture is unavailable.</i>"
        )
        lines.append("")
        lines.append(
            "<i>Card has no actionable signal on this host; the kernel "
            "capability model is Linux-specific.</i>"
        )
        return "\n".join(lines)

    # CapEff is the load-bearing field — it's what's actually
    # effective right now. Compute the global ⚠s on it before
    # rendering each set.
    cap_eff = snap.sets.get("CapEff")
    root_equivalent = cap_eff is not None and _is_root_equivalent(cap_eff)
    sys_admin = (
        cap_eff is not None
        and not root_equivalent  # don't double-mark
        and _has_sys_admin(cap_eff)
    )

    if root_equivalent:
        lines.append(
            "  <b>posture:</b> <i>root-equivalent — every named capability is in CapEff</i> ⚠"
        )
    elif sys_admin:
        lines.append(
            "  <b>posture:</b> <i>CAP_SYS_ADMIN is set in CapEff — "
            "the catch-all cap effectively bypasses every other "
            "permission check</i> ⚠"
        )
    elif cap_eff == 0:
        lines.append("  <b>posture:</b> <i>no effective capabilities</i>")
    else:
        eff_count = len(_decode_bitmap(cap_eff)) if cap_eff is not None else 0
        lines.append(f"  <b>posture:</b> <i>{eff_count} effective capabilities (review below)</i>")

    lines.append("")
    lines.append("  <b>sets:</b>")
    # Pretty labels. The kernel's column names are terse; we
    # spell them out so the operator doesn't have to remember
    # which is which.
    pretty: dict[str, str] = {
        "CapInh": "Inheritable (CapInh)",
        "CapPrm": "Permitted (CapPrm)",
        "CapEff": "Effective (CapEff)",
        "CapBnd": "Bounding (CapBnd)",
        "CapAmb": "Ambient (CapAmb)",
    }
    for field in _CAP_FIELDS:
        lines.extend(_format_set(pretty[field], snap.sets.get(field)))

    lines.append("")
    lines.append(
        "<i>⚠ markers: CapEff == full bitmap (root-equivalent — "
        "operator probably set <code>User=root</code> in the systemd "
        "unit for convenience and forgot to switch back), or CAP_SYS_"
        "ADMIN is set in isolation (catch-all bypass; almost always a "
        "stale fix for a permission error that should have been "
        "addressed with a narrower cap). The healthy posture for THIS "
        "bot is <code>CapEff=0</code> — it doesn't bind low ports "
        "(nginx fronts the webhook) or touch privileged kernel "
        "operations.</i>"
    )
    return "\n".join(lines)


async def handle_admin_capabilities(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_capabilities; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        cap_eff=snap.sets.get("CapEff"),
        status_present=snap.status_present,
    ).info("/admin_capabilities rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.capabilities")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_capabilities(message, settings)

    router.message.register(_entry, Command("admin_capabilities", ignore_case=True))
    return router
