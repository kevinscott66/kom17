"""``/admin_netconns`` — TCP socket-state census from /proc/net/tcp{,6}.

Complements /admin_dns (resolver-side reachability), /admin_certfp
(handshake-side fingerprint), /admin_dbprobe (per-engine liveness)
and /admin_telegram_api (live API call) by surfacing the **kernel
socket-table** view: how many sockets does THIS host hold in each
TCP state right now.

Why an operator wants this:

* Port-exhaustion early warning. A high TIME_WAIT count (several
  thousand) means the bot is churning short-lived outbound
  connections — typically a runaway aiohttp client without
  connection reuse, or a misconfigured connection pool. The host
  has 28k–60k ephemeral ports; once they fill, every new outbound
  connection blocks. TIME_WAIT > 1000 surfaces this before the
  user-visible "timed out" symptoms appear.
* Backlog visibility. ESTABLISHED count climbing while the bot
  isn't busier is the connection-leak signature — an outbound
  socket that never gets ``close()``d (typical cause: a writer
  hung on an awaited future). Pair with /admin_fds for the
  process-side view; this card is the kernel-side view.
* LISTEN sanity. The bot itself doesn't LISTEN (webhook is via
  the front nginx); but in dev / polling-mode it shouldn't either,
  so a non-zero LISTEN count is a misconfiguration hint.
* IPv6 parity. /proc/net/tcp covers v4, /proc/net/tcp6 covers v6
  — modern hosts have both; missing v6 is informational, missing
  v4 is structurally impossible (the netstack always exposes v4
  even when no v4 interface is configured).

Posture: silent-drop for non-devs, private-only at the router
level. Linux-only — non-Linux hosts render an informational
section rather than ⚠ (the file genuinely cannot exist).

Cost: two ``open`` + ``read`` of small text files; on a host with
2000 sockets, /proc/net/tcp is ~300 KB. Parsing is line-iteration
+ a single hex int conversion per row. Single-digit-ms.
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


log = logger.bind(component="handlers.admin.netconns")


# TCP state code → human label. The kernel emits the state as a
# 2-char hex byte in column 4 of /proc/net/tcp; this is the
# decoding table from ``include/net/tcp_states.h`` (stable since
# Linux 2.6, ABI commitment).
_TCP_STATES: dict[str, str] = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
}


# TIME_WAIT > 1000 is the canonical port-exhaustion early warning
# on a host running a single Python process. A web server fronting
# many short connections can legitimately sit at 5-10k; for THIS
# workload (one bot + one DB) anything over 1k is anomalous.
_TIME_WAIT_CONCERNING = 1000


# /proc paths exposed as constants so the test suite can swap them
# for fixture files. Production always reads the kernel-real ones.
_PROC_TCP = Path("/proc/net/tcp")
_PROC_TCP6 = Path("/proc/net/tcp6")


class _NetSnapshot:
    """Captured TCP-state census.

    ``v4_states`` / ``v6_states`` map state-label → count. Missing
    file (non-Linux) leaves the corresponding dict empty AND sets
    the ``*_present`` flag to False so the render branches on
    "intentionally absent" vs "present but empty".
    """

    __slots__ = ("v4_present", "v4_states", "v6_present", "v6_states")

    def __init__(
        self,
        *,
        v4_states: dict[str, int],
        v4_present: bool,
        v6_states: dict[str, int],
        v6_present: bool,
    ) -> None:
        self.v4_states = v4_states
        self.v4_present = v4_present
        self.v6_states = v6_states
        self.v6_present = v6_present


def _parse_proc_net_tcp(text: str) -> dict[str, int]:
    """Parse a /proc/net/tcp{,6} text blob into state-label → count.

    Format (per ``include/net/tcp_states.h`` + ``net/ipv4/tcp_ipv4.c``):

        sl local_addr:port rem_addr:port st tx:rx tr:tm rt uid timeout inode

    The header line is skipped (starts with whitespace + ``sl``).
    Column 3 (zero-indexed) is the 2-hex-char state code. Unknown
    state codes are bucketed under ``UNKNOWN_<hex>`` so a future
    kernel addition surfaces visibly rather than disappearing.
    """
    counts: dict[str, int] = {}
    for line_no, raw in enumerate(text.splitlines()):
        if line_no == 0:
            continue  # header
        parts = raw.split()
        if len(parts) < 4:
            continue
        state_hex = parts[3].upper()
        label = _TCP_STATES.get(state_hex, f"UNKNOWN_{state_hex}")
        counts[label] = counts.get(label, 0) + 1
    return counts


def _capture(*, tcp_path: Path = _PROC_TCP, tcp6_path: Path = _PROC_TCP6) -> _NetSnapshot:
    """Read both proc files (if present) and parse them.

    Missing files render as ``*_present=False`` — the non-Linux
    signal. Unreadable files (permission denied, in a restrictive
    namespace) treated the same as missing for rendering purposes,
    with a log line so the operator sees the difference in the
    journal if they look.
    """
    v4: dict[str, int] = {}
    v4_present = tcp_path.exists()
    if v4_present:
        try:
            v4 = _parse_proc_net_tcp(tcp_path.read_text())
        except OSError as exc:
            log.bind(path=str(tcp_path), error=type(exc).__name__).warning(
                "failed to read /proc/net/tcp"
            )
            v4_present = False
    v6: dict[str, int] = {}
    v6_present = tcp6_path.exists()
    if v6_present:
        try:
            v6 = _parse_proc_net_tcp(tcp6_path.read_text())
        except OSError as exc:
            log.bind(path=str(tcp6_path), error=type(exc).__name__).warning(
                "failed to read /proc/net/tcp6"
            )
            v6_present = False
    return _NetSnapshot(
        v4_states=v4,
        v4_present=v4_present,
        v6_states=v6,
        v6_present=v6_present,
    )


def _total_time_wait(snap: _NetSnapshot) -> int:
    """Sum TIME_WAIT across v4 + v6 — port exhaustion is a host-wide
    constraint, the kernel doesn't track ephemeral ports per family
    independently."""
    return snap.v4_states.get("TIME_WAIT", 0) + snap.v6_states.get("TIME_WAIT", 0)


def _render_family(label: str, present: bool, states: dict[str, int]) -> list[str]:
    """Render one family (v4 / v6) as a list of section lines.

    Empty states dict with present=True (host has the file but no
    sockets) renders as "0 sockets" rather than the empty section
    we'd get from naive iteration. Missing-file branch is rendered
    by the caller because it's an informational note, not a per-
    state line.
    """
    if not present:
        return [f"  <b>{label}:</b> <i>file not present (non-Linux host)</i>"]
    total = sum(states.values())
    lines = [f"  <b>{label}:</b> <code>{total}</code> sockets"]
    if total == 0:
        return lines
    # Sort by canonical state order (the table-of-states order, not
    # alphabetical) — operators read top-down from ESTABLISHED and
    # expect that order.
    for hex_code in _TCP_STATES:
        state_label = _TCP_STATES[hex_code]
        count = states.get(state_label)
        if count:
            warn = " ⚠" if state_label == "TIME_WAIT" and count > _TIME_WAIT_CONCERNING else ""
            lines.append(f"    • <code>{state_label}:</code> {count}{warn}")
    # Surface any UNKNOWN_<hex> buckets we accumulated — these are
    # state codes the kernel added since this card was written.
    for key in sorted(states):
        if key.startswith("UNKNOWN_"):
            lines.append(f"    • <code>{key}:</code> {states[key]} ⚠")
    return lines


def _render(snap: _NetSnapshot) -> str:
    lines = ["🔌 <b>TCP socket census</b>", ""]
    lines.extend(_render_family("ipv4", snap.v4_present, snap.v4_states))
    lines.append("")
    lines.extend(_render_family("ipv6", snap.v6_present, snap.v6_states))

    tw_total = _total_time_wait(snap)
    if tw_total > _TIME_WAIT_CONCERNING:
        lines.append("")
        lines.append(
            f"  <b>combined TIME_WAIT:</b> <code>{tw_total}</code> ⚠ "
            f"<i>(port-exhaustion risk; typical cause is an aiohttp "
            f"client without connection-pool reuse — cross-check "
            f"/admin_fds for per-process open sockets)</i>"
        )

    lines.append("")
    lines.append(
        f"<i>⚠ markers: TIME_WAIT &gt; {_TIME_WAIT_CONCERNING} (combined "
        f"v4+v6, port-exhaustion early warning), or an unknown state "
        f"code (kernel added a new TCP state since this card was "
        f"written — refresh the decoding table).</i>"
    )
    return "\n".join(lines)


async def handle_admin_netconns(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_netconns; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(
        user_id=user.id,
        v4_total=sum(snap.v4_states.values()),
        v6_total=sum(snap.v6_states.values()),
        time_wait=_total_time_wait(snap),
    ).info("/admin_netconns rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.netconns")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_netconns(message, settings)

    router.message.register(_entry, Command("admin_netconns", ignore_case=True))
    return router
