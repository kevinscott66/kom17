"""``/admin_hostinfo`` — host identity snapshot.

The blue/green deploy story for production ends with "switch
nginx upstream to the new container; verify the new one is serving;
drain the old one". The verification step has historically been
"compare logs", which is fine until log buffering hides the swap.
This card answers the question "which host / kernel / container am
I talking to?" in one Telegram message.

Why an operator wants this:

* Post-deploy verification. After flipping the upstream, the
  operator sends /admin_hostinfo to confirm the FQDN, kernel build,
  and process boot time match the freshly-deployed container, not
  the soon-to-be-drained one. Without this card the only
  comparable signal is /admin_uptime — and uptime alone can match
  on coincidence (two containers both restarted N minutes ago).
* Disaster-recovery: which DC is this? When the host moves
  (planned migration, or a failover the operator wasn't briefed
  on), the FQDN difference here is the only mechanically-visible
  signal — Telegram doesn't carry datacenter metadata into
  webhook updates.
* Sanity-check on /etc/hosts drift. If the host's ``/etc/hostname``
  disagrees with what ``hostname -f`` reports, the bot can be
  reaching the network with a different identity than the
  operator-facing one. The card surfaces ``platform.node`` and
  ``socket.getfqdn`` side by side so the disagreement is visible.

Reads :mod:`platform` (uname tuple, node, system, release,
machine) and :func:`socket.getfqdn`. Both are pure-Python, no
syscalls beyond ``uname`` and one DNS-or-/etc/hosts resolution.
We do NOT issue an outbound DNS query — ``getfqdn`` falls back to
``gethostname`` if the resolver is unreachable, which is what we
want during a network-partition incident.

Same posture as every other ``/admin_*``: silent-drop for non-devs
(existence must not leak dev IDs), private-only at the router
level (hostnames are not strictly secret but the rest of the
control plane is private, so keeping the posture symmetric
simplifies the audit).
"""

from __future__ import annotations

import html
import platform
import socket
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.hostinfo")


class _HostSnapshot:
    """One-shot host-identity bundle.

    Two kinds of names are surfaced because they answer different
    operator questions:

    * ``node`` (``platform.node``) — what the OS thinks its name
      is, typically ``/etc/hostname``.
    * ``fqdn`` (``socket.getfqdn``) — what the network thinks the
      name is, via the resolver.

    Disagreement between these two is the failure mode this card
    catches; on a healthy host they're either identical or the FQDN
    is the node with a domain suffix appended.

    ``system / release / machine`` are the uname trio that pin the
    kernel version — useful for the "did the container actually
    pick up the new image?" check after a deploy.
    """

    __slots__ = (
        "fqdn",
        "machine",
        "node",
        "processor",
        "release",
        "system",
        "version",
    )

    def __init__(
        self,
        *,
        node: str,
        fqdn: str,
        system: str,
        release: str,
        version: str,
        machine: str,
        processor: str,
    ) -> None:
        self.node = node
        self.fqdn = fqdn
        self.system = system
        self.release = release
        self.version = version
        self.machine = machine
        self.processor = processor


def _capture() -> _HostSnapshot:
    """Sample the load-bearing host-identity values once.

    Each call is microseconds + one resolver lookup. The lookup is
    cached by the OS for the process lifetime so repeated invocations
    don't hammer DNS — but even on a cold cache the operator-facing
    latency is below what a Telegram round-trip adds.
    """
    uname = platform.uname()
    # ``socket.getfqdn`` can return an empty string on a host with
    # no resolver configuration. Surface "<unresolved>" rather than
    # let an empty cell read like the rendering broke.
    fqdn = socket.getfqdn() or "<unresolved>"
    return _HostSnapshot(
        node=uname.node or "<unknown>",
        fqdn=fqdn,
        system=uname.system or "<unknown>",
        release=uname.release or "<unknown>",
        # ``uname.version`` is the kernel build string — long
        # ("#1 SMP PREEMPT_DYNAMIC Thu Jan ...") and load-bearing
        # for "is this the new kernel?" checks. Render whole.
        version=uname.version or "<unknown>",
        machine=uname.machine or "<unknown>",
        # ``processor`` is often empty on Linux (no /proc/cpuinfo
        # parse fallback baked in). Surface explicitly rather than
        # silently drop the row.
        processor=uname.processor or "<unset>",
    )


def _render(snap: _HostSnapshot) -> str:
    lines = ["🖥 <b>Host identity</b>", ""]
    lines.append(f"<b>node:</b> <code>{html.escape(snap.node)}</code>")
    # Render fqdn unconditionally so the operator can scan for
    # node/fqdn disagreement at a glance. Rendering it only on
    # mismatch would hide the healthy state and make absence
    # ambiguous.
    lines.append(f"<b>fqdn:</b> <code>{html.escape(snap.fqdn)}</code>")
    if snap.node != snap.fqdn and snap.fqdn != f"{snap.node}.localdomain":
        # Soft warning: a real prod disagreement is a config drift,
        # but the FQDN ending in ``.localdomain`` is what
        # ``getfqdn`` falls back to on a host with no domain set
        # and is genuinely benign. Don't cry wolf on the benign
        # case — operators will learn to ignore the marker.
        lines.append("  <i>⚠ node ≠ fqdn — /etc/hostname vs resolver drift</i>")
    lines.append("")
    lines.append(f"<b>system:</b> <code>{html.escape(snap.system)}</code>")
    lines.append(f"<b>release:</b> <code>{html.escape(snap.release)}</code>")
    lines.append(f"<b>version:</b> <code>{html.escape(snap.version)}</code>")
    lines.append(f"<b>machine:</b> <code>{html.escape(snap.machine)}</code>")
    lines.append(f"<b>processor:</b> <code>{html.escape(snap.processor)}</code>")
    return "\n".join(lines)


async def handle_admin_hostinfo(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_hostinfo; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_hostinfo rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.hostinfo")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_hostinfo(message, settings)

    router.message.register(_entry, Command("admin_hostinfo", ignore_case=True))
    return router
