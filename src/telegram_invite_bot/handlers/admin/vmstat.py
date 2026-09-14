"""``/admin_vmstat`` — curated /proc/vmstat memory/swap activity counters.

/proc/vmstat has 100+ key-value lines. We curate a small subset
that answers operator-pressing memory questions no other card can
answer:

* /admin_meminfo — point-in-time MemAvailable.
* /admin_swaps — swap *configuration* (devices, sizes, priorities).
* /admin_smaps — per-process honest accounting.
* /admin_psi — windowed pressure averages.

What's still missing: did the kernel actually *act* on memory
pressure since boot? Did it swap pages in/out? Did major page
faults happen? Did the OOM killer fire? Those are cumulative
counters in /proc/vmstat — invisible to every other card.

⚠ predicate: ``oom_kill`` non-zero. When the kernel OOM-killer
fires, something on this host got SIGKILL'd because the system
ran out of memory; the operator needs to know. Counter is
cumulative since boot so a long-running host might be carrying
ancient hits — that's fine, OOM-kill is interesting at any age
and the operator can correlate with `dmesg`. Other counters are
informational decoration: pgmajfault is normal on any active host
(every fresh page-cache fill from disk is a fault), pswpin/out can
spike during legitimate memory-pressure recovery without being an
incident.

Forward-compat: /proc/vmstat is one key-value pair per line,
whitespace-separated. We parse all of them and store as a dict;
the curated render walks a fixed list but the full dict is kept
on the snapshot so a future card can extend without re-parsing.
Missing curated keys render as "n/a" rather than silent-drop so
an operator on an older kernel sees what's absent.

Same posture as every other admin card — silent-drop, private-only,
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


log = logger.bind(component="handlers.admin.vmstat")


_VMSTAT_PATH = Path("/proc/vmstat")


# Curated subset — order is operator priority. oom_kill first
# because it's the ⚠ field; then swap activity; then fault rate;
# then page-reclaim under pressure indicators.
_CURATED: tuple[tuple[str, str], ...] = (
    ("oom_kill", "OOM-killer fires since boot — non-zero means something got SIGKILL'd ⚠"),
    ("pswpin", "pages swapped IN from disk — swap actually used"),
    ("pswpout", "pages swapped OUT to disk — memory pressure reclaimed via swap"),
    ("pgmajfault", "major faults — page had to come from disk (page-cache miss)"),
    ("pgfault", "minor faults — page allocated or copied in RAM (decorative)"),
    ("pgsteal_kswapd", "pages reclaimed by kswapd (background reclaim)"),
    ("pgsteal_direct", "pages reclaimed by direct reclaim — synchronous, latency-impacting"),
    ("pgscan_kswapd", "pages scanned by kswapd — proxy for reclaim work"),
    ("pgscan_direct", "pages scanned by direct reclaim"),
    ("nr_free_pages", "current free pages (4KiB each on most archs) — point-in-time"),
)


# ⚠ trigger fields — narrow on purpose. Only oom_kill: it's the
# one /proc/vmstat counter where any non-zero value is unambiguous
# "kernel killed something for being too big". Swap and fault
# counters are normal on any active host and would be cry-wolf.
_WARN_FIELDS: frozenset[str] = frozenset({"oom_kill"})


class _VmstatSnapshot:
    """Captured /proc/vmstat.

    * ``values`` — every parsed key→int. We keep the full dict
      rather than only the curated subset so a future card can
      pivot without re-parsing.
    * ``available`` — False on macOS dev / non-procfs container.
    """

    __slots__ = ("available", "values")

    def __init__(
        self,
        *,
        values: dict[str, int],
        available: bool,
    ) -> None:
        self.values = values
        self.available = available


def _parse_vmstat(text: str) -> dict[str, int]:
    """Parse /proc/vmstat.

    Each line is ``<key> <value>``. Non-integer values are skipped
    per-line — kernel has had a few floats historically (e.g.
    ``balloon_inflate`` on Xen guests) and we want to keep parsing
    the rest of the file regardless.
    """
    values: dict[str, int] = {}
    for raw_line in text.splitlines():
        parts = raw_line.split()
        if len(parts) < 2:
            continue
        key = parts[0]
        try:
            values[key] = int(parts[1])
        except ValueError:
            continue
    return values


def _capture(*, path: Path = _VMSTAT_PATH) -> _VmstatSnapshot:
    """Read /proc/vmstat + build a snapshot."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _VmstatSnapshot(values={}, available=False)
    return _VmstatSnapshot(values=_parse_vmstat(text), available=True)


def _triggered_warns(snap: _VmstatSnapshot) -> tuple[str, ...]:
    """Names of warn-fields with non-zero values.

    Empty when snapshot unavailable — absence of data is NOT a
    warning (macOS dev parity)."""
    if not snap.available:
        return ()
    return tuple(name for name in _WARN_FIELDS if snap.values.get(name, 0) > 0)


def _fmt_value(value: int | None) -> str:
    return "n/a" if value is None else f"{value:,}"


def _render(snap: _VmstatSnapshot) -> str:
    lines = ["📊 <b>VM activity counters (/proc/vmstat)</b>", ""]

    if not snap.available:
        lines.append(
            "  <i>/proc/vmstat unavailable on this host — Linux-only "
            "card. macOS + non-procfs containers see this.</i>"
        )
        return "\n".join(lines)

    if not snap.values:
        lines.append(
            "  <i>/proc/vmstat is empty — extremely unusual; check "
            "kernel build for missing CONFIG_VM_EVENT_COUNTERS.</i>"
        )
        return "\n".join(lines)

    triggered = _triggered_warns(snap)
    if triggered:
        joined = ", ".join(f"<code>{w}</code>" for w in triggered)
        lines.append(
            f"  ⚠ <b>OOM-killer fired since boot:</b> {joined} non-zero "
            f"— kernel SIGKILL'd at least one process for being too "
            f"large for available memory. Correlate with dmesg for "
            f"victim identity + timestamp."
        )
        lines.append("")

    lines.append("  <b>Curated counters:</b>")
    for key, desc in _CURATED:
        value = snap.values.get(key)
        rendered_value = _fmt_value(value)
        lines.append(f"  • <code>{key}</code> = <code>{rendered_value}</code> — {desc}")

    lines.append("")
    lines.append(
        f"<i>Total /proc/vmstat keys parsed: {len(snap.values)}. "
        f"Counters are cumulative since boot — pswpin/pswpout non-zero "
        f"on a long-running host may be ancient swap activity from a "
        f"single past pressure event.</i>"
    )
    lines.append("")
    lines.append(
        "<i>⚠ markers: oom_kill non-zero only — the unambiguous "
        "&quot;kernel killed something&quot; signal. pgmajfault and "
        "pswpin/out are NOT marked: major faults are normal on any "
        "active host (every page-cache miss is a fault) and swap "
        "activity can be legitimate pressure recovery. See "
        "/admin_meminfo for point-in-time memory, /admin_swaps for "
        "swap configuration, /admin_psi for windowed pressure.</i>"
    )
    return "\n".join(lines)


async def handle_admin_vmstat(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_vmstat; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    triggered = _triggered_warns(snap)
    log.bind(
        user_id=user.id,
        available=snap.available,
        field_count=len(snap.values),
        oom_kill=snap.values.get("oom_kill"),
        pswpout=snap.values.get("pswpout"),
        warn_count=len(triggered),
    ).info("/admin_vmstat rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.vmstat")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_vmstat(message, settings)

    router.message.register(_entry, Command("admin_vmstat", ignore_case=True))
    return router
