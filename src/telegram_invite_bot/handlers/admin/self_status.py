"""``/admin_self_status`` — process security/runtime posture from /proc/self/status.

The existing process-side surface (``/admin_proc``, ``/admin_memory``,
``/admin_threads``, ``/admin_fds``, ``/admin_capabilities``) covers
resource accounting, file descriptors, and the capability-set
side of /proc/self/status. The fields it leaves uncovered are the
**security posture** ones — and those have unambiguous operator
signals that no other card surfaces:

* **TracerPid.** Non-zero means another process has attached via
  ``ptrace`` — strace, gdb, perf trace, BPF uprobe, lsof -p, an
  intrusion. In dev this is harmless; in prod it's the kind of
  thing an operator wants to know about immediately. This is one
  of the ⚠ predicates.
* **CoreDumping.** =1 means the kernel is currently writing a
  core dump of this process. We're alive enough to read /proc
  but about to be gone. The card may render once before the
  next request fails — but the rendered ⚠ is the operator's
  last warning. Second ⚠ predicate.
* **Seccomp.** 0 = no seccomp, 1 = strict mode, 2 = filter mode.
  Operationally relevant for a hardened daemon — but POLICY,
  not a universal warning (sealed VMs legitimately run with
  Seccomp=0). We surface the value, no ⚠.
* **NoNewPrivs.** 1 means setuid binaries can't elevate. Hardening
  flag — informational, surface but don't warn.
* **Speculation_Store_Bypass.** Per-thread CPU vuln mitigation
  state (``thread vulnerable`` / ``thread mitigated`` / ``not
  vulnerable``). Pairs with /admin_cmdline's mitigations= value
  but at the running-process granularity.

⚠ predicate count: two. ``TracerPid != 0`` OR ``CoreDumping == 1``.
Cry-wolf guard pins must-not-fire on the canonical-healthy
sample.

Parser shape — /proc/self/status is ``Key:\tValue`` per line.
Stable since 2.4. Most values are single integers; a few
(``Uid``, ``Gid``) are tab-separated tuples (real/effective/
saved/fs). We extract a curated subset; unknown keys ignored
(forward-compat).

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


log = logger.bind(component="handlers.admin.self_status")


_STATUS_PATH = Path("/proc/self/status")


# Curated keys we care about. Anything else /proc/self/status
# emits drops silently — forward-compat against future kernel
# additions, and dedup against /admin_memory + /admin_proc which
# already cover the resource accounting side.
_INTERESTING = frozenset(
    {
        "Name",
        "State",
        "Pid",
        "PPid",
        "TracerPid",
        "Uid",
        "Gid",
        "FDSize",
        "Threads",
        "NSpid",
        "Seccomp",
        "Seccomp_filters",
        "NoNewPrivs",
        "Speculation_Store_Bypass",
        "CoreDumping",
        "Cpus_allowed_list",
    }
)


_SECCOMP_LABELS = {
    "0": "disabled",
    "1": "strict",
    "2": "filter",
}


class _SelfStatusSnapshot:
    """Captured /proc/self/status (curated subset).

    Fields are precomputed booleans for the two ⚠ predicates
    plus the raw key:value dict for render. Surfacing the raw
    dict (filtered to ``_INTERESTING``) keeps the rendered card
    in lockstep with what we actually parsed — operator can
    cross-check.
    """

    __slots__ = ("available", "core_dumping", "fields", "tracer_pid")

    def __init__(
        self,
        *,
        fields: dict[str, str],
        available: bool,
    ) -> None:
        self.fields = fields
        self.available = available
        self.tracer_pid = self._int_or_zero(fields.get("TracerPid", "0"))
        self.core_dumping = self._int_or_zero(fields.get("CoreDumping", "0")) == 1

    @staticmethod
    def _int_or_zero(raw: str) -> int:
        """Defensive int parse — a future kernel emitting a
        non-numeric value shouldn't crash the card; treat it as
        zero (no warning) rather than synthesising one."""
        try:
            return int(raw.split()[0]) if raw.strip() else 0
        except ValueError:
            return 0

    @property
    def is_traced(self) -> bool:
        return self.tracer_pid != 0


def _parse_self_status(text: str) -> dict[str, str]:
    """Parse /proc/self/status into a filtered key:value dict.

    Only keys in ``_INTERESTING`` are kept. Lines without a
    colon (corrupt read, future format) drop defensively.
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key not in _INTERESTING:
            continue
        out[key] = value.strip()
    return out


def _capture(*, path: Path = _STATUS_PATH) -> _SelfStatusSnapshot:
    """Read /proc/self/status + build snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _SelfStatusSnapshot(fields={}, available=False)
    return _SelfStatusSnapshot(fields=_parse_self_status(text), available=True)


def _format_seccomp(raw: str) -> str:
    """Decode the Seccomp integer into a human label, preserving
    the raw value alongside (the integer is what dmesg / audit
    logs reference)."""
    token = raw.strip().split()[0] if raw.strip() else "0"
    label = _SECCOMP_LABELS.get(token, "unknown")
    return f"{token} ({label})"


def _render(snap: _SelfStatusSnapshot) -> str:
    lines = ["🪪 <b>Process posture (/proc/self/status)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/self/status unavailable — Linux-only "
            "surface (macOS dev / non-procfs container sees this).</i>"
        )
        return "\n".join(lines)

    if not snap.fields:
        lines.append(
            "  <i>/proc/self/status returned no recognised keys — "
            "extremely unusual; format may have shifted.</i>"
        )
        return "\n".join(lines)

    warn_markers: list[str] = []
    if snap.is_traced:
        warn_markers.append("traced")
    if snap.core_dumping:
        warn_markers.append("coredump")
    warn_marker = f" ⚠ {','.join(warn_markers)}" if warn_markers else ""

    # Identity + tracing block.
    lines.append(f"  <b>Identity:</b>{warn_marker}")
    lines.append(
        f"    Name=<code>{snap.fields.get('Name', '?')}</code>  "
        f"State=<code>{snap.fields.get('State', '?')}</code>  "
        f"Pid=<code>{snap.fields.get('Pid', '?')}</code>  "
        f"PPid=<code>{snap.fields.get('PPid', '?')}</code>"
    )
    tracer = snap.fields.get("TracerPid", "0")
    tracer_marker = " ⚠" if snap.is_traced else ""
    lines.append(f"    TracerPid=<code>{tracer}</code>{tracer_marker}")

    # Security posture block.
    lines.append("")
    lines.append("  <b>Security posture:</b>")
    seccomp_raw = snap.fields.get("Seccomp", "0")
    lines.append(f"    Seccomp=<code>{_format_seccomp(seccomp_raw)}</code>")
    if "Seccomp_filters" in snap.fields:
        lines.append(f"    Seccomp_filters=<code>{snap.fields['Seccomp_filters']}</code>")
    if "NoNewPrivs" in snap.fields:
        lines.append(f"    NoNewPrivs=<code>{snap.fields['NoNewPrivs']}</code>")
    if "Speculation_Store_Bypass" in snap.fields:
        lines.append(
            f"    Speculation_Store_Bypass=<code>{snap.fields['Speculation_Store_Bypass']}</code>"
        )

    # Lifecycle block (CoreDumping is the second ⚠).
    if snap.core_dumping:
        lines.append("")
        lines.append("  <b>Lifecycle:</b>")
        lines.append("    CoreDumping=<code>1</code> ⚠")

    # Threading / namespace block (cross-reference for the
    # operator — small enough to surface, big enough to skip if
    # absent).
    lines.append("")
    lines.append("  <b>Threading / namespace:</b>")
    if "Threads" in snap.fields:
        lines.append(f"    Threads=<code>{snap.fields['Threads']}</code>")
    if "FDSize" in snap.fields:
        lines.append(f"    FDSize=<code>{snap.fields['FDSize']}</code>")
    if "NSpid" in snap.fields:
        lines.append(f"    NSpid=<code>{snap.fields['NSpid']}</code>")
    if "Cpus_allowed_list" in snap.fields:
        lines.append(f"    Cpus_allowed_list=<code>{snap.fields['Cpus_allowed_list']}</code>")

    lines.append("")
    if snap.is_traced and snap.core_dumping:
        lines.append(
            "<i>⚠ TracerPid AND CoreDumping non-zero. Process is being "
            "traced WHILE writing a core dump — almost always a debugger "
            "post-mortem of a fatal signal in progress.</i>"
        )
    elif snap.is_traced:
        lines.append(
            "<i>⚠ TracerPid is non-zero — another process is attached "
            "via ptrace (strace / gdb / perf trace / BPF uprobe / "
            "intrusion). In dev this is normal; in prod it warrants a "
            "look at <code>/proc/&lt;TracerPid&gt;/comm</code>.</i>"
        )
    elif snap.core_dumping:
        lines.append(
            "<i>⚠ CoreDumping=1 — the kernel is writing a core dump of "
            "this process right now. Next request to the bot will "
            "almost certainly fail. Inspect <code>/var/lib/systemd/"
            "coredump/</code> or <code>kernel.core_pattern</code> for "
            "the resulting file.</i>"
        )
    else:
        lines.append(
            "<i>No warnings — process is not being traced and is not "
            "writing a core dump. Seccomp value is informational only "
            "(0 is legitimate on most non-hardened daemons); compare "
            "with /admin_cmdline's <code>mitigations=</code> and "
            "/admin_capabilities for the privilege side.</i>"
        )
    return "\n".join(lines)


async def handle_admin_self_status(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_self_status; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        available=snap.available,
        tracer_pid=snap.tracer_pid,
        core_dumping=snap.core_dumping,
        fields=len(snap.fields),
    ).info("/admin_self_status rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.self_status")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_self_status(message, settings)

    router.message.register(_entry, Command("admin_self_status", ignore_case=True))
    return router
