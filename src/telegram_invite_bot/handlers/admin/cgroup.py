"""``/admin_cgroup`` — cgroup membership + resource caps for this process.

Where the bot ACTUALLY lives on the host's resource hierarchy, and
what limits the kernel will enforce. Complements /admin_memory
(process-side counters) by surfacing the **cap that triggers the
kill**: a 2GiB RSS reading is benign on a 16GiB host but one
unlucky GC pause away from an OOM inside a 2.1GiB ``memory.max``
container.

Why an operator wants this:

* "Why does the bot die at 2GiB RSS when the host has 16GiB?"
  Almost always: a forgotten ``MemoryMax=`` in the systemd unit,
  or a Docker ``--memory`` cap inherited from a base compose file.
  Without this card the operator has to ``cat /proc/self/cgroup``
  by hand and chase symlinks through ``/sys/fs/cgroup`` — the
  card does the chase once.
* cgroup v1 vs v2: subtly different limit semantics (v1
  ``memory.limit_in_bytes`` includes cache; v2 ``memory.max``
  excludes reclaimable). Knowing the regime is half the
  diagnostic — render it explicitly rather than guessing.
* ``memory.current / memory.max`` is the **only** signal the
  operator gets BEFORE the OOM kill. /admin_memory shows VmRSS
  from /proc/self/status — useful, but uncapped. The pressure %
  here is the kill-clock countdown.
* ``pids.current / pids.max`` catches the rarer fork-bomb /
  thread-leak scenario where the bot runs out of task structs
  before it runs out of memory. Container defaults are
  surprisingly tight (often 4096) and a chatty worker pool can
  approach the cap.

⚠ markers are conservative — we only cry on real pressure, not
on the existence of a limit (a limit is operator intent). The
absence of any cgroup membership (``/proc/self/cgroup`` empty or
unreadable) is informational, not ⚠ — that's the non-Linux /
restricted-namespace signal.
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


log = logger.bind(component="handlers.admin.cgroup")


# /proc + /sys paths are constants so tests can swap them for
# fixture files. The kernel-real paths are used in production.
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")
_SYS_FS_CGROUP = Path("/sys/fs/cgroup")


# Pressure above this fraction earns a ⚠. 0.9 is conservative: a
# bot bursting briefly to 80% under load is normal; 90%+ sustained
# means an OOM is one allocation away. Below this, surface the
# numbers but stay quiet — cry-wolf prevention is the recurring
# theme across the admin index.
_PRESSURE_WARN_FRACTION = 0.9


class _CgroupSnapshot:
    """Captured cgroup membership and key resource caps.

    Tri-state fields: integer when the matching ``/sys/fs/cgroup``
    entry was read + parsed; the sentinel string ``"max"`` for
    unlimited (the kernel writes literally ``max`` in cgroup v2);
    ``None`` when the file was missing or unparseable. Render
    branches on each.
    """

    __slots__ = (
        "controllers",
        "cpu_max",
        "memory_current",
        "memory_max",
        "mode",
        "path",
        "pids_current",
        "pids_max",
    )

    def __init__(
        self,
        *,
        mode: str,
        path: str | None,
        controllers: tuple[str, ...],
        memory_max: int | str | None,
        memory_current: int | None,
        cpu_max: str | None,
        pids_max: int | str | None,
        pids_current: int | None,
    ) -> None:
        # mode ∈ {"v2", "v1", "hybrid", "none"} — keep it a plain
        # string rather than an Enum because the render branches
        # are flat and an Enum would just add ceremony.
        self.mode = mode
        self.path = path
        self.controllers = controllers
        self.memory_max = memory_max
        self.memory_current = memory_current
        self.cpu_max = cpu_max
        self.pids_max = pids_max
        self.pids_current = pids_current


def _parse_proc_cgroup(text: str) -> tuple[str, str | None, tuple[str, ...]]:
    """Parse ``/proc/self/cgroup``.

    cgroup v2 emits exactly one line of the form ``0::<path>``.
    cgroup v1 emits one line per controller: ``N:<controller>:<path>``.
    Hybrid hosts (v1 + v2 simultaneously) emit both — the v2 line
    has hierarchy id 0 and empty controller field.

    Returns ``(mode, path, controllers)`` where ``mode`` is one of
    ``"v2"``, ``"v1"``, ``"hybrid"``, ``"none"``; ``path`` is the
    v2 path if present else the first v1 path; ``controllers`` is
    the tuple of v1 controller names (empty for pure v2).
    """
    v2_path: str | None = None
    v1_path: str | None = None
    v1_controllers: list[str] = []
    for line in text.splitlines():
        # Each line is `hid:controller:path`. We split on the FIRST
        # two colons only because the path itself may contain
        # colons (rare but legal in slice names).
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hid, controller, path = parts
        if hid == "0" and controller == "":
            v2_path = path
        else:
            v1_path = v1_path or path
            if controller:
                # Split comma-separated controller lists ("cpu,cpuacct")
                # so each shows individually — matches the layout
                # operators see in /sys/fs/cgroup.
                v1_controllers.extend(controller.split(","))
    if v2_path is not None and v1_controllers:
        return "hybrid", v2_path, tuple(v1_controllers)
    if v2_path is not None:
        return "v2", v2_path, ()
    if v1_controllers:
        return "v1", v1_path, tuple(v1_controllers)
    return "none", None, ()


def _read_text(path: Path) -> str | None:
    """Read a /sys/fs/cgroup file. Returns None on any failure
    mode — missing file (controller not enabled), OSError
    (permission, ENOTDIR), or unreadable. Render branches on None
    to distinguish "no limit configured" from "we can't tell"."""
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _parse_max_or_int(value: str | None) -> int | str | None:
    """cgroup v2 limits are either ``max`` (unlimited) or a
    plain integer. Pre-parse so render doesn't have to repeat
    the dance. Unparseable yields None (forward-compat: if a
    future kernel adds a new sentinel, we degrade rather than
    crash)."""
    if value is None:
        return None
    if value == "max":
        return "max"
    try:
        return int(value)
    except ValueError:
        return None


def _capture(
    *,
    proc_cgroup_path: Path = _PROC_SELF_CGROUP,
    sys_fs_cgroup_root: Path = _SYS_FS_CGROUP,
) -> _CgroupSnapshot:
    """Read /proc/self/cgroup, then read the matching v2 limit
    files. cgroup v1 limit files exist under each controller's
    directory rather than the unified hierarchy; we don't follow
    them — v1 is on the deprecation path upstream and the
    diagnostic value is much higher for v2 (which is what every
    modern systemd / Docker deploy ships).
    """
    try:
        text = proc_cgroup_path.read_text()
    except OSError:
        return _CgroupSnapshot(
            mode="none",
            path=None,
            controllers=(),
            memory_max=None,
            memory_current=None,
            cpu_max=None,
            pids_max=None,
            pids_current=None,
        )
    mode, path, controllers = _parse_proc_cgroup(text)

    # v2-style limit lookup. For "hybrid" we still read the v2
    # files because that's where systemd writes the limits on a
    # hybrid host. For "v1" we leave the limit fields None — the
    # render explicitly says so.
    memory_max: int | str | None = None
    memory_current: int | None = None
    cpu_max: str | None = None
    pids_max: int | str | None = None
    pids_current: int | None = None
    if mode in ("v2", "hybrid") and path is not None:
        # Strip the leading slash so it joins with the cgroup
        # root cleanly (Path("/sys/fs/cgroup") / "/foo" yields
        # Path("/foo"), which would escape the root).
        rel = path.lstrip("/")
        cg_dir = sys_fs_cgroup_root / rel
        memory_max = _parse_max_or_int(_read_text(cg_dir / "memory.max"))
        mem_cur_raw = _read_text(cg_dir / "memory.current")
        if mem_cur_raw is not None:
            try:
                memory_current = int(mem_cur_raw)
            except ValueError:
                memory_current = None
        cpu_max = _read_text(cg_dir / "cpu.max")
        pids_max = _parse_max_or_int(_read_text(cg_dir / "pids.max"))
        pids_cur_raw = _read_text(cg_dir / "pids.current")
        if pids_cur_raw is not None:
            try:
                pids_current = int(pids_cur_raw)
            except ValueError:
                pids_current = None

    return _CgroupSnapshot(
        mode=mode,
        path=path,
        controllers=controllers,
        memory_max=memory_max,
        memory_current=memory_current,
        cpu_max=cpu_max,
        pids_max=pids_max,
        pids_current=pids_current,
    )


def _fmt_bytes(n: int) -> str:
    """Compact byte formatting: 1.5GiB, 256MiB, etc. The cgroup
    numbers we surface are always in bytes; raw integers are
    unreadable past ~1MB."""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{n}B"  # pragma: no cover — loop covers all cases


def _pressure_fraction(current: int | None, cap: int | str | None) -> float | None:
    """Return current/cap as a fraction, or None if we can't
    compute it (no cap configured, no current reading, or cap is
    the ``max`` sentinel). The None case is what suppresses ⚠ on
    uncapped processes — operator intent, not a problem."""
    if current is None or cap is None or isinstance(cap, str) or cap <= 0:
        return None
    return current / cap


def _is_pressured(current: int | None, cap: int | str | None) -> bool:
    """⚠ predicate. Conservative: only fires above
    ``_PRESSURE_WARN_FRACTION`` — bursting to 80% under load is
    normal Python heap behaviour."""
    frac = _pressure_fraction(current, cap)
    return frac is not None and frac >= _PRESSURE_WARN_FRACTION


def _fmt_limit(limit: int | str | None) -> str:
    """Render a limit field uniformly: ``unlimited`` for the
    ``max`` sentinel, byte-formatted for ints, ``not readable``
    for None. The render reuses this for both memory and pids."""
    if limit is None:
        return "<i>not readable</i>"
    if isinstance(limit, str):
        return "<code>max</code> <i>(unlimited)</i>"
    return f"<code>{_fmt_bytes(limit)}</code>"


def _render(snap: _CgroupSnapshot) -> str:
    lines = ["🧺 <b>cgroup posture</b>", ""]

    if snap.mode == "none":
        # No cgroup membership = non-Linux host, or running in a
        # restricted namespace where /proc/self/cgroup is hidden.
        # Informational, NOT ⚠ — same posture as every other
        # admin card that surfaces a missing Linux-only file.
        lines.append(
            "  <i>no cgroup membership detected — non-Linux host "
            "or restricted namespace. No actionable signal.</i>"
        )
        lines.append("")
        lines.append(
            "<i>⚠ markers: only emitted on real pressure "
            "(<code>memory.current</code> or <code>pids.current</code> "
            "above 90% of cap). The mere existence of a limit is "
            "operator intent, not a problem.</i>"
        )
        return "\n".join(lines)

    lines.append(f"  <b>mode:</b> <code>{snap.mode}</code>")
    if snap.path is not None:
        lines.append(f"  <b>path:</b> <code>{snap.path}</code>")
    if snap.controllers:
        # v1 controllers shown sorted for stable diffing across
        # runs; the kernel emits them in mount order which jitters.
        lines.append(
            "  <b>v1 controllers:</b> "
            + ", ".join(f"<code>{c}</code>" for c in sorted(snap.controllers))
        )

    if snap.mode == "v1":
        # We don't chase v1 controller dirs — render the disclaimer
        # so the operator knows why the limits section is empty.
        lines.append("")
        lines.append(
            "  <i>cgroup v1 limit files live under each controller "
            "dir rather than the unified hierarchy. This card surfaces "
            "v2 limits only; upgrade the host to unified cgroups for "
            "full coverage.</i>"
        )
    else:
        lines.append("")
        lines.append("  <b>memory:</b>")
        lines.append(f"    • max:     {_fmt_limit(snap.memory_max)}")
        if snap.memory_current is None:
            lines.append("    • current: <i>not readable</i>")
        else:
            frac = _pressure_fraction(snap.memory_current, snap.memory_max)
            pct = f" ({frac:.0%} of cap)" if frac is not None else ""
            warn = " ⚠" if _is_pressured(snap.memory_current, snap.memory_max) else ""
            lines.append(
                f"    • current: <code>{_fmt_bytes(snap.memory_current)}</code>{pct}{warn}"
            )

        lines.append("")
        lines.append("  <b>cpu:</b>")
        if snap.cpu_max is None:
            lines.append("    • max: <i>not readable</i>")
        else:
            # cpu.max is "quota period" e.g. "max 100000" (unlimited)
            # or "50000 100000" (half a core). Keep the raw form —
            # the operator's mental model matches the kernel ABI.
            lines.append(
                f"    • max: <code>{snap.cpu_max}</code> "
                f"<i>(quota period; &quot;max&quot; = unlimited)</i>"
            )

        lines.append("")
        lines.append("  <b>pids:</b>")
        lines.append(f"    • max:     {_fmt_limit(snap.pids_max)}")
        if snap.pids_current is None:
            lines.append("    • current: <i>not readable</i>")
        else:
            frac = _pressure_fraction(snap.pids_current, snap.pids_max)
            pct = f" ({frac:.0%} of cap)" if frac is not None else ""
            warn = " ⚠" if _is_pressured(snap.pids_current, snap.pids_max) else ""
            lines.append(f"    • current: <code>{snap.pids_current}</code>{pct}{warn}")

    lines.append("")
    lines.append(
        "<i>⚠ markers: only emitted on real pressure "
        "(<code>memory.current</code> or <code>pids.current</code> "
        "above 90% of cap). The mere existence of a limit is "
        "operator intent, not a problem. Tune via "
        "<code>systemctl set-property telegram-bot.service "
        "MemoryMax=…</code> or the unit file.</i>"
    )
    return "\n".join(lines)


async def handle_admin_cgroup(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_cgroup; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        mode=snap.mode,
        path=snap.path,
        memory_max=snap.memory_max,
        memory_current=snap.memory_current,
        pids_max=snap.pids_max,
        pids_current=snap.pids_current,
    ).info("/admin_cgroup rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.cgroup")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_cgroup(message, settings)

    router.message.register(_entry, Command("admin_cgroup", ignore_case=True))
    return router
