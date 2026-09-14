"""``/admin_fdlimit`` — file-descriptor + memory limit snapshot.

Complements /admin_proc (RSS, CPU) and /admin_engines (pool checked-
out) by surfacing the OS-imposed ceilings the process runs against.
The diagnostic gap this closes is the "we hit a ceiling we didn't
know we had" failure mode: a SQLAlchemy pool that grows freely
against an unset application limit but then runs into a default
systemd ``LimitNOFILE=1024`` and starts throwing
``OSError: [Errno 24] Too many open files``. /admin_engines surfaces
"checked_out is climbing"; this card surfaces "and the ceiling is
1024 — you have N FDs of headroom".

Why an operator wants this:

* Predict an outage before it lands. RLIMIT_NOFILE soft against
  /admin_engines's checked_out tells you how many concurrent DB
  sessions you can grow into before the OS refuses. A 1024 soft
  with 800 already checked out is a sharper warning than any
  in-app metric.
* Verify a systemd unit's LimitNOFILE took. After editing
  ``LimitNOFILE=65536`` in the unit and restarting, the only
  Telegram-visible verification path is this card — otherwise
  the operator has to ``cat /proc/$(pidof bot)/limits`` from
  shell.
* Memory ceiling. RLIMIT_AS or RLIMIT_RSS, if set by the host
  (k8s cgroup, systemd unit), surfaces here too. An unset
  RLIMIT shows as :data:`resource.RLIM_INFINITY`; the renderer
  collapses that to "unlimited" so the operator sees the
  intent, not the sentinel value.

Pure stdlib (``resource``), no IO, no DB. Same posture as every
other ``/admin_*``: silent-drop for non-devs, private-only.
"""

from __future__ import annotations

import resource
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.fdlimit")


# The set of RLIMIT_* names we surface. Other rlimits exist
# (RLIMIT_CORE, RLIMIT_STACK) but none of them tie to a real bot
# failure mode worth dedicating a card line to. Keep this list
# tight so the operator scans three values, not eight.
#
# Tuple of (display_label, attribute_name). We resolve the
# attribute by getattr because RLIMIT_AS is missing on some
# platforms (macOS) — the card has to render gracefully there
# rather than crash on import.
_RLIMITS: tuple[tuple[str, str], ...] = (
    ("Open files (NOFILE)", "RLIMIT_NOFILE"),
    ("Address space (AS)", "RLIMIT_AS"),
    ("Resident set (RSS)", "RLIMIT_RSS"),
)


class _LimitRow:
    """One rlimit's (soft, hard) reading.

    ``available`` is False when the platform doesn't expose this
    rlimit (no ``RLIMIT_AS`` on macOS, no ``RLIMIT_RSS`` on Linux
    in current kernels). Render those explicitly as "n/a" rather
    than skip — the operator should see the absence, not be left
    wondering whether the read failed silently.
    """

    __slots__ = ("available", "hard", "label", "soft")

    def __init__(
        self,
        *,
        label: str,
        soft: int,
        hard: int,
        available: bool,
    ) -> None:
        self.label = label
        self.soft = soft
        self.hard = hard
        self.available = available


def _capture() -> list[_LimitRow]:
    """Read every entry in :data:`_RLIMITS` defensively.

    The defensive ``getattr`` is load-bearing for macOS where
    ``resource.RLIMIT_AS`` is undefined — a direct attribute
    access would AttributeError at import-time. Rendering the
    row as "n/a" is the right surface: the limit doesn't exist
    on this platform, but the operator looking for it shouldn't
    have to learn "oh right, that one's Linux-only".
    """
    rows: list[_LimitRow] = []
    for label, attr in _RLIMITS:
        rlim = getattr(resource, attr, None)
        if rlim is None:
            rows.append(_LimitRow(label=label, soft=0, hard=0, available=False))
            continue
        soft, hard = resource.getrlimit(rlim)
        rows.append(_LimitRow(label=label, soft=soft, hard=hard, available=True))
    return rows


def _fmt_limit(value: int) -> str:
    """Format a single rlimit value.

    :data:`resource.RLIM_INFINITY` is -1 (the platform sentinel);
    rendering it as ``-1`` would mislead an operator scanning for
    "is this bounded?". Collapse to the human word "unlimited".
    """
    if value == resource.RLIM_INFINITY:
        return "unlimited"
    return f"{value:,}"


def _render(rows: list[_LimitRow]) -> str:
    lines = ["📎 <b>Resource ceilings</b>", ""]
    for row in rows:
        if not row.available:
            # Platform doesn't expose this rlimit — surface
            # explicitly so the operator sees the absence rather
            # than a silently-skipped row.
            lines.append(f"• {row.label}: <code>n/a on this platform</code>")
            continue
        lines.append(
            f"• {row.label}: "
            f"soft <code>{_fmt_limit(row.soft)}</code>, "
            f"hard <code>{_fmt_limit(row.hard)}</code>"
        )
    lines.append("")
    lines.append(
        "<i>Soft is the active ceiling; the process can raise it "
        "up to hard via <code>setrlimit</code>. Cross-reference "
        "NOFILE-soft against /admin_engines checked_out + open "
        "sockets to predict EMFILE before it lands.</i>"
    )
    return "\n".join(lines)


async def handle_admin_fdlimit(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_fdlimit; silently dropped"
        )
        return
    rows = _capture()
    await message.answer(_render(rows))
    log.bind(user_id=user.id).info("/admin_fdlimit rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.fdlimit")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_fdlimit(message, settings)

    router.message.register(_entry, Command("admin_fdlimit", ignore_case=True))
    return router
