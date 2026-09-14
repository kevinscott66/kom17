"""``/admin_smaps`` — Proportional Set Size + shared/private accounting.

Reads ``/proc/self/smaps_rollup`` (kernel 4.14+: a pre-aggregated
sum of every mapping's smaps entry — much cheaper than parsing
the full ``/proc/self/smaps``). Surfaces the fields that
distinguish "memory cost to THIS process" from "memory cost to
the system" — the gap that VmRSS in /admin_memory hides.

Why an operator wants this:

* VmRSS double-counts shared libraries. A 2 GiB RSS reading
  where 1 GiB is libc + Python + numpy shared with three other
  Python processes on the same host means the bot is only
  responsible for ~1.25 GiB of host pressure (its private +
  its fair share of the shared). PSS is that fair-share number
  — the only honest "what does this bot actually cost?" metric.
* The Pss / Rss ratio reveals shared-vs-private balance. Close
  to 1.0 = nearly everything is private (anonymous heap, no
  meaningful library sharing on this host). Close to 0.5 or
  below = lots of sharing (typical for a co-tenant Python host
  where stdlib + site-packages are mmap'd from a common venv).
* Private_Clean vs Private_Dirty splits the bot's exclusive
  footprint into "can be reclaimed without writeback" (clean
  file-backed pages) and "must be swapped" (anonymous heap).
  High Private_Dirty on a swap-constrained host = an OOM is
  one allocation away. Complements /admin_oom + /admin_cgroup
  (which surface the policy) with the actual page distribution.
* Swap + SwapPss: how much of the bot is currently paged out.
  A persistent nonzero Swap on a long-running bot means the
  kernel decided some of our heap was cold — usually fine, but
  a sudden spike + matching wall-clock latency is the canonical
  "the host is overcommitted" signal.

Cry-wolf posture: no ⚠ on absolute sizes (a 4 GiB bot on a
32 GiB host is fine; an 800 MiB bot on a 1 GiB VM is not — we
can't tell from this card alone, /admin_cgroup is the place for
pressure). The single ⚠ is on a nonzero ``Swap`` — operator
awareness, not alarm — and only when the value is non-trivial
(over 1 MiB, same threshold idiom as /admin_io).

Linux 4.14+ only. Non-Linux + older kernels render an
informational note rather than a ⚠ — same posture as every other
Linux-specific admin card.
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


log = logger.bind(component="handlers.admin.smaps")


_PROC_SMAPS_ROLLUP = Path("/proc/self/smaps_rollup")


# Subset of smaps_rollup fields we render. The kernel emits ~20
# fields; we pick the ones that actually answer the operator's
# questions and leave the rest for forward-compat under an
# "additional fields" footer. Order is render-order — fields
# appear top-to-bottom in this layout.
_SMAPS_FIELDS: tuple[str, ...] = (
    "Rss",
    "Pss",
    "Pss_Anon",
    "Pss_File",
    "Pss_Shmem",
    "Shared_Clean",
    "Shared_Dirty",
    "Private_Clean",
    "Private_Dirty",
    "Referenced",
    "Anonymous",
    "Swap",
    "SwapPss",
)


# Per-field one-liner. The kernel docs (smaps under
# Documentation/filesystems/proc.rst) define the fields; we
# paraphrase to surface the operator-actionable angle (cost,
# shareability, reclaimability) rather than the kernel-internal
# definition.
_SMAPS_FIELD_NOTES: dict[str, str] = {
    "Rss": "resident set — what VmRSS shows; double-counts shared",
    "Pss": "proportional — RSS divided fairly across sharers",
    "Pss_Anon": "anonymous PSS share — heap not backed by a file",
    "Pss_File": "file-backed PSS — mmap'd libs / executables",
    "Pss_Shmem": "shmem PSS — tmpfs / shared segments",
    "Shared_Clean": "shareable + matches backing file — free to drop",
    "Shared_Dirty": "shareable but modified — must be written back",
    "Private_Clean": "exclusive to us + clean — reclaimable cheaply",
    "Private_Dirty": "exclusive to us + dirty — must swap to reclaim",
    "Referenced": "kernel-marked recently accessed (LRU survival)",
    "Anonymous": "total anonymous (heap + stack + private mmaps)",
    "Swap": "currently paged out to swap",
    "SwapPss": "swap fair-share across processes",
}


# Swap ⚠ floor. Same idiom as /admin_io's cancelled_write_bytes —
# tiny nonzero values are benign (kernel idle-page demotion), the
# threshold is conservative.
_SWAP_WARN_BYTES = 1 * 1024 * 1024


class _SmapsSnapshot:
    """Captured smaps_rollup readout.

    ``fields`` maps each smaps field name to its byte count, or
    None if the field was missing/unparseable. ``status_present``
    distinguishes non-Linux / pre-4.14 (no rollup file) from
    real-but-incomplete readings.
    """

    __slots__ = ("fields", "status_present")

    def __init__(
        self,
        *,
        fields: dict[str, int | None],
        status_present: bool,
    ) -> None:
        self.fields = fields
        self.status_present = status_present


def _parse_smaps_rollup(text: str) -> dict[str, int | None]:
    """Parse /proc/self/smaps_rollup.

    Kernel format per field: ``<Name>:<whitespace><value> kB``
    (always kB regardless of system page size — kernel
    convention). The first line is a header (``ADDR ADDR ...
    [rollup]``) which has no colon and is skipped.

    Returns a dict keyed by every name in ``_SMAPS_FIELDS``
    (missing → None) plus any kernel-side additions (forward-
    compat). Values are returned in BYTES, not kB — the render
    layer should not have to remember the kernel's unit choice.
    """
    parsed: dict[str, int | None] = dict.fromkeys(_SMAPS_FIELDS)
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep:
            # Header line ("ADDR-ADDR [rollup]") has no colon.
            continue
        name = name.strip()
        if not name:
            continue
        # Value form is "<int> kB". Strip the unit, parse the
        # number, convert to bytes. Anything else → None for
        # that field (forward-compat against a future unit
        # change rather than a crash).
        tokens = rest.strip().split()
        if not tokens:
            parsed[name] = None
            continue
        try:
            kb = int(tokens[0])
        except ValueError:
            parsed[name] = None
            continue
        parsed[name] = kb * 1024
    return parsed


def _capture(*, smaps_path: Path = _PROC_SMAPS_ROLLUP) -> _SmapsSnapshot:
    try:
        text = smaps_path.read_text()
    except OSError:
        return _SmapsSnapshot(
            fields=dict.fromkeys(_SMAPS_FIELDS),
            status_present=False,
        )
    return _SmapsSnapshot(
        fields=_parse_smaps_rollup(text),
        status_present=True,
    )


def _swap_concerning(swap: int | None) -> bool:
    """⚠ predicate. Single ⚠ trigger on this card — nonzero swap
    above the floor. Below the floor = idle-page demotion noise,
    not an issue."""
    return swap is not None and swap >= _SWAP_WARN_BYTES


def _fmt_bytes(n: int) -> str:
    """Compact byte formatting — same idiom as /admin_io,
    /admin_cgroup. Deliberately consistent so the operator's
    eye doesn't have to re-tune between cards."""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{n}B"  # pragma: no cover — loop covers all cases


def _pss_rss_ratio(snap: _SmapsSnapshot) -> float | None:
    """PSS/RSS ratio: how much of the bot's resident memory is
    its OWN cost vs amortized across sharers. Returns None if
    either field is missing — render shows the ratio when we
    have it (educational); the card stays terse when we don't."""
    rss = snap.fields.get("Rss")
    pss = snap.fields.get("Pss")
    if rss is None or pss is None or rss <= 0:
        return None
    return pss / rss


def _render(snap: _SmapsSnapshot) -> str:
    lines = ["🧮 <b>Proportional Set Size (smaps_rollup)</b>", ""]

    if not snap.status_present:
        lines.append(
            "  <i>/proc/self/smaps_rollup not readable — non-Linux "
            "host, kernel &lt; 4.14, or restricted namespace. No "
            "actionable signal.</i>"
        )
        lines.append("")
        lines.append(
            "<i>⚠ markers: only emitted on nonzero <code>Swap</code> "
            "(over 1 MiB) — kernel paged some of our heap out. "
            "Absolute sizes are never ⚠'d; cross-reference "
            "/admin_cgroup for pressure context.</i>"
        )
        return "\n".join(lines)

    lines.append("  <b>memory accounting:</b>")
    for name in _SMAPS_FIELDS:
        value = snap.fields.get(name)
        note = _SMAPS_FIELD_NOTES.get(name, "")
        if value is None:
            lines.append(f"    • <code>{name}</code>: <i>unreadable</i> <i>— {note}</i>")
            continue
        warn = " ⚠" if name == "Swap" and _swap_concerning(value) else ""
        lines.append(
            f"    • <code>{name}</code>: <code>{_fmt_bytes(value)}</code> <i>({note})</i>{warn}"
        )

    ratio = _pss_rss_ratio(snap)
    if ratio is not None:
        # Editorial commentary on the ratio. Three buckets,
        # informational only — operator decides whether the
        # interpretation matches their host topology.
        if ratio >= 0.9:
            interpretation = "almost entirely private; little shared-library benefit"
        elif ratio >= 0.6:
            interpretation = "mixed; typical for a moderately-loaded Python host"
        else:
            interpretation = "heavy sharing; co-tenant Python processes amortise libs"
        lines.append("")
        lines.append(
            f"  <b>PSS / RSS ratio:</b> <code>{ratio:.2f}</code> <i>({interpretation})</i>"
        )

    extras = sorted(set(snap.fields) - set(_SMAPS_FIELDS))
    if extras:
        lines.append("")
        lines.append("  <b>additional fields (kernel-side additions):</b>")
        for name in extras:
            value = snap.fields[name]
            if value is None:
                lines.append(f"    • <code>{name}</code>: <i>unreadable</i>")
            else:
                lines.append(f"    • <code>{name}</code>: <code>{_fmt_bytes(value)}</code>")

    lines.append("")
    lines.append(
        "<i>⚠ markers: only emitted on nonzero <code>Swap</code> "
        "(over 1 MiB) — kernel paged some of our heap out. "
        "Absolute sizes are never ⚠'d; cross-reference "
        "/admin_cgroup for pressure context. PSS is the honest "
        "per-process memory cost — divide RSS fairly across "
        "shared-library sharers.</i>"
    )
    return "\n".join(lines)


async def handle_admin_smaps(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_smaps; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        status_present=snap.status_present,
        rss=snap.fields.get("Rss"),
        pss=snap.fields.get("Pss"),
        swap=snap.fields.get("Swap"),
    ).info("/admin_smaps rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.smaps")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_smaps(message, settings)

    router.message.register(_entry, Command("admin_smaps", ignore_case=True))
    return router
