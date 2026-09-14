"""``/admin_sysctl`` — curated read of /proc/sys kernel tunables.

Every other admin card reads a single /proc file with a fixed
schema. /proc/sys is the opposite — a tree of thousands of
individual tunables, each a one-line file at a path mirroring the
dotted sysctl name (``net.core.somaxconn`` → ``/proc/sys/net/core/somaxconn``).
We can't dump the whole tree — Telegram's 4096-char limit makes
that hopeless and most of the tree is uninteresting anyway — so
this card is opinionated: a hand-curated set of tunables that
genuinely move the needle for a network-bound bot.

Why these specific keys:

* ``net.core.somaxconn`` — listen() backlog cap. **The classic
  production trap.** Kernel default was 128 until Linux 5.4
  (now 4096), and many container base images still ship 128.
  For a webhook-receiving bot this caps SYN-ACK queue depth
  under burst load; once exceeded clients see connection refused
  and retry storms ensue. Single ⚠ trigger of this card.
* ``net.ipv4.tcp_max_syn_backlog`` — companion to somaxconn.
  Useful to see together; we don't ⚠ on it because it interacts
  with somaxconn in a way that's hard to reduce to a single
  threshold.
* ``net.ipv4.ip_local_port_range`` — ephemeral port range for
  outbound connections. Default 32768-60999 (~28k ports). A bot
  making many outbound API calls can exhaust this; surfacing the
  number lets the operator do the math.
* ``net.ipv4.tcp_fin_timeout`` — how long a socket stays in
  FIN_WAIT_2 after we close it. Default 60s. Combined with high
  request rates this is the main contributor to TIME_WAIT pileups.
* ``net.ipv4.tcp_keepalive_time`` — idle seconds before keepalive
  probes. Default 7200 (2 hours). Often tuned down for long-lived
  client connections to detect dead peers faster.
* ``fs.file-max`` — system-wide open-file-handle ceiling. RLIMIT
  caps per-process; this caps the whole system. Cross-check with
  /admin_fdlimit (per-process).
* ``kernel.pid_max`` — global PID ceiling. Bot writing children
  / fork-storms can hit it before NPROC; combined with last_pid
  from /admin_loadavg, gives operators a delta-sample headroom.
* ``vm.overcommit_memory`` — 0/1/2 mode for malloc accounting.
  Value 2 (strict accounting) means mallocs can fail when there's
  free RAM but commit limit is hit; surprises many bots. Not
  ⚠'d because the value is policy choice, not a fault.

Cry-wolf posture: a single ⚠ on ``net.core.somaxconn ≤ 128``.
Other curated keys are shown but not ⚠'d — the values vary by
workload and one operator's "broken" is another's "deliberately
tuned". The single ⚠ is reserved for the unambiguous case where
a container base image silently kept the pre-5.4 default and the
operator deploys a network-heavy bot on it.

Forward-compat: a key that doesn't exist in /proc/sys on this
kernel renders as "n/a" rather than skipping — operator should
see the absence (it might tell them the kernel is stripped or
they're in a sandbox without that namespace).

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


log = logger.bind(component="handlers.admin.sysctl")


_PROC_SYS = Path("/proc/sys")


# The legacy default that's kept tripping ops people since the
# 90s. Linux 5.4 (Dec 2019) bumped the default to 4096 but the
# /proc/sys/net/core/somaxconn value isn't auto-migrated on
# upgrade, and many container images still ship the old value.
# Pin the threshold at the legacy default — if somaxconn equals
# or is below it, the operator definitely needs to tune.
_SOMAXCONN_WARN_LE = 128


# (display label, /proc/sys path components, one-liner note).
# Curated to keep the card scannable; each note explains the
# *operator-facing meaning* rather than the kernel-internal
# definition.
_SYSCTL_KEYS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "net.core.somaxconn",
        ("net", "core", "somaxconn"),
        "listen() backlog cap — warned at the legacy 128 default",
    ),
    (
        "net.ipv4.tcp_max_syn_backlog",
        ("net", "ipv4", "tcp_max_syn_backlog"),
        "SYN-queue cap (companion to somaxconn)",
    ),
    (
        "net.ipv4.ip_local_port_range",
        ("net", "ipv4", "ip_local_port_range"),
        "ephemeral port range — outbound TCP ceiling",
    ),
    (
        "net.ipv4.tcp_fin_timeout",
        ("net", "ipv4", "tcp_fin_timeout"),
        "FIN_WAIT_2 idle seconds — TIME_WAIT pileup contributor",
    ),
    (
        "net.ipv4.tcp_keepalive_time",
        ("net", "ipv4", "tcp_keepalive_time"),
        "idle seconds before keepalive probes start",
    ),
    (
        "fs.file-max",
        ("fs", "file-max"),
        "system-wide open-fd ceiling (vs per-process RLIMIT_NOFILE)",
    ),
    (
        "kernel.pid_max",
        ("kernel", "pid_max"),
        "global PID ceiling — fork-storm headroom signal",
    ),
    (
        "vm.overcommit_memory",
        ("vm", "overcommit_memory"),
        "0=heuristic, 1=always, 2=strict — policy, not a fault",
    ),
)


class _SysctlRow:
    """One curated sysctl key + its current value.

    * ``value`` — the raw string contents of the /proc/sys file,
      with trailing whitespace stripped. We keep it as string
      because some keys (ip_local_port_range, kernel.printk) are
      tab/space-separated tuples, not a single integer. Predicates
      that need an int parse on demand.
    * ``available`` — False when the file doesn't exist or can't
      be read (sandboxed kernel namespace, stripped /proc).
    """

    __slots__ = ("available", "key", "note", "value")

    def __init__(
        self,
        *,
        key: str,
        note: str,
        value: str | None,
        available: bool,
    ) -> None:
        self.key = key
        self.note = note
        self.value = value
        self.available = available


class _SysctlSnapshot:
    """Captured curated sysctl readings.

    * ``rows`` — one per curated key, in declaration order.
    * ``proc_sys_available`` — False when /proc/sys itself isn't
      a directory (macOS dev). Distinct from per-key availability
      because the global absence wants a different message than
      "we have /proc/sys but this specific key is missing".
    """

    __slots__ = ("proc_sys_available", "rows")

    def __init__(
        self,
        *,
        rows: tuple[_SysctlRow, ...],
        proc_sys_available: bool,
    ) -> None:
        self.rows = rows
        self.proc_sys_available = proc_sys_available


def _read_one(base: Path, parts: tuple[str, ...]) -> str | None:
    """Read one /proc/sys file. Returns None on any failure
    (missing key, permission denied, unreadable). Caller decides
    how to render None — degrade-don't-crash."""
    p = base.joinpath(*parts)
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _capture(*, base: Path = _PROC_SYS) -> _SysctlSnapshot:
    """Read each curated key + build a snapshot.

    Keyword-only ``base`` parameter so tests can synthesise a
    /proc/sys tree under tmp_path — same hermetic-fixture pattern
    as every other diagnostic card.
    """
    if not base.is_dir():
        return _SysctlSnapshot(
            rows=tuple(
                _SysctlRow(key=key, note=note, value=None, available=False)
                for key, _, note in _SYSCTL_KEYS
            ),
            proc_sys_available=False,
        )
    rows: list[_SysctlRow] = []
    for key, parts, note in _SYSCTL_KEYS:
        value = _read_one(base, parts)
        rows.append(
            _SysctlRow(
                key=key,
                note=note,
                value=value,
                available=value is not None,
            )
        )
    return _SysctlSnapshot(rows=tuple(rows), proc_sys_available=True)


def _somaxconn_too_low(snap: _SysctlSnapshot) -> bool:
    """⚠ predicate — somaxconn at-or-below the legacy default.

    Returns False if the field is unreadable or unparseable —
    absence of data is NOT a warning, same cry-wolf-prevention
    posture as every other card.
    """
    for row in snap.rows:
        if row.key != "net.core.somaxconn":
            continue
        if row.value is None:
            return False
        try:
            return int(row.value) <= _SOMAXCONN_WARN_LE
        except ValueError:
            return False
    return False


def _render(snap: _SysctlSnapshot) -> str:
    lines = ["🎛 <b>Kernel tunables (/proc/sys)</b>", ""]

    if not snap.proc_sys_available:
        lines.append(
            "  <i>/proc/sys not available on this host — Linux-only "
            "card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    somaxconn_warn = _somaxconn_too_low(snap)
    for row in snap.rows:
        if not row.available or row.value is None:
            lines.append(f"  • <b>{row.key}</b>: <i>n/a</i>")
        else:
            marker = " ⚠" if row.key == "net.core.somaxconn" and somaxconn_warn else ""
            lines.append(f"  • <b>{row.key}</b>: <code>{row.value}</code>{marker}")
        lines.append(f"      <i>{row.note}</i>")

    lines.append("")
    lines.append(
        "<i>⚠ markers: only when net.core.somaxconn is at-or-below "
        "the legacy 128 default — that's the canonical &quot;the "
        "container image kept the pre-5.4 kernel default&quot; "
        "trap. Other keys are operator policy, not faults. See "
        "/admin_netconns for live socket-state census, "
        "/admin_fdlimit for per-process fd headroom.</i>"
    )
    return "\n".join(lines)


async def handle_admin_sysctl(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_sysctl; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        proc_sys_available=snap.proc_sys_available,
        somaxconn_warn=_somaxconn_too_low(snap),
    ).info("/admin_sysctl rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.sysctl")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_sysctl(message, settings)

    router.message.register(_entry, Command("admin_sysctl", ignore_case=True))
    return router
