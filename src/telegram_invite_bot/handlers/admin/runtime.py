"""``/admin_runtime`` — interpreter-tunable snapshot (recursionlimit,
switchinterval, int_max_str_digits, maxsize).

Complements /admin_flags (boot-time sys.flags) with the **runtime-
mutable** tunables — the ones a library or a stray ``sys.setrecursion-
limit(...)`` can change after start. /admin_flags answers "how was
this interpreter launched"; this one answers "what has someone done
to it since".

Why an operator wants this:

* ``sys.getrecursionlimit()`` — default 1000. We've seen libraries
  (notably some JSON-walking and template-engine code) bump this to
  10000+ to "fix" a RecursionError, which turns a tractable
  stack-overflow crash into an opaque C-stack segfault that
  ``faulthandler`` may or may not catch. Surfacing the integer lets
  the operator spot the bump without bisecting requirements.txt.
* ``sys.getswitchinterval()`` — default 0.005s (5ms). The GIL
  release cadence. Some performance-tuning posts on the internet
  suggest cranking this to 0.1s for CPU-bound workloads; for an
  IO-bound bot that's catastrophic — every coroutine that touches
  a thread pool waits up to 100ms before getting scheduled. The
  bot's latency budget evaporates and the cause is one ``sys.set-
  switchinterval`` call buried in some "optimisation" branch.
* ``sys.get_int_max_str_digits()`` — default 4300. The CVE-2020-10735
  / 2022-26488 mitigation: parsing ``int("1" * 100_000)`` is
  quadratic in the number of digits and can DoS the interpreter
  with user-controlled input. Setting this to ``0`` disables the
  limit. On a bot that takes user text — group nicknames, wallet
  amounts typed as strings — disabling this is a real DoS surface.
  Surface the integer; ⚠ when it's 0 (off) or unexpectedly high.
* ``sys.maxsize`` — confirms the build is 64-bit (expected
  ``9223372036854775807``). A 32-bit build sneaking into a deploy
  is the kind of "how did this even happen" failure mode that
  costs an hour of head-scratching when you don't have this card.

Read-once at message-time. Same posture as every other ``/admin_*``:
silent-drop for non-devs, private-only at the router level.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.runtime")


# CPython defaults at time of writing (3.12). Used by ``_is_concern-
# ing`` for the recursionlimit / switchinterval rows where the
# "departed from default" signal is the load-bearing one — an
# operator scanning the card needs to know which numbers are out
# of the ordinary, not just what the numbers are.
_DEFAULT_RECURSION_LIMIT = 1000
_DEFAULT_SWITCH_INTERVAL = 0.005
_DEFAULT_INT_MAX_STR_DIGITS = 4300
_EXPECTED_MAXSIZE_64BIT = 2**63 - 1


class _RuntimeSnapshot:
    """Captured interpreter tunables.

    Each field is a plain int / float so the renderer doesn't have
    to know per-field semantics. ``int_max_str_digits_available``
    flags the rare case CPython is built without the int-str
    conversion limit (Python < 3.11 or a custom build) — we still
    surface the row, just as "unavailable" instead of a number.
    """

    __slots__ = (
        "int_max_str_digits",
        "int_max_str_digits_available",
        "maxsize",
        "recursion_limit",
        "switch_interval",
    )

    def __init__(
        self,
        *,
        recursion_limit: int,
        switch_interval: float,
        int_max_str_digits: int,
        int_max_str_digits_available: bool,
        maxsize: int,
    ) -> None:
        self.recursion_limit = recursion_limit
        self.switch_interval = switch_interval
        self.int_max_str_digits = int_max_str_digits
        self.int_max_str_digits_available = int_max_str_digits_available
        self.maxsize = maxsize


def _capture() -> _RuntimeSnapshot:
    """Sample interpreter tunables. Defensive on int_max_str_digits.

    The int-str conversion limit was added in 3.11 (security
    backport, also landed in 3.10.7 / 3.9.14 / 3.8.14). On a build
    without it ``sys.get_int_max_str_digits`` simply doesn't exist
    — ``getattr`` guards the call so the diagnostic stays useful
    on older interpreters.
    """
    get_int_max = getattr(sys, "get_int_max_str_digits", None)
    if get_int_max is None:
        int_max = -1
        int_max_available = False
    else:
        try:
            int_max = int(get_int_max())
            int_max_available = True
        except (TypeError, ValueError, OSError):
            int_max = -1
            int_max_available = False

    return _RuntimeSnapshot(
        recursion_limit=sys.getrecursionlimit(),
        switch_interval=sys.getswitchinterval(),
        int_max_str_digits=int_max,
        int_max_str_digits_available=int_max_available,
        maxsize=sys.maxsize,
    )


def _recursion_concerning(value: int) -> bool:
    """``True`` if recursionlimit has been bumped well above default.

    A 10% wiggle (≤ 1100) is tolerated — some test frameworks bump
    to 1100/1500 transiently. The "stack-overflow → C segfault"
    failure mode the docstring documents only really bites when
    libraries bump to ~10000, so the threshold is generous.
    """
    return value > _DEFAULT_RECURSION_LIMIT * 2


def _switch_interval_concerning(value: float) -> bool:
    """``True`` if switchinterval has been pushed off the IO-bound default.

    Anything above 20ms (4× default) means coroutines wait noticeably
    longer for GIL handoffs — a real latency hit on an async bot.
    Below default is fine (more aggressive switching) but cranking
    it up is the "I read a blog post" failure mode."""
    return value > _DEFAULT_SWITCH_INTERVAL * 4


def _int_max_str_digits_concerning(value: int, available: bool) -> bool:
    """``True`` if the int-str conversion limit was disabled or bumped.

    ``0`` means OFF — the CVE-2020-10735 mitigation is disabled. Any
    value far above the default (e.g. 100_000) also defeats the
    point. Unavailable (older build) is informational, not concerning
    — we can't fault an interpreter for not having the feature."""
    if not available:
        return False
    if value == 0:
        return True
    return value > _DEFAULT_INT_MAX_STR_DIGITS * 4


def _maxsize_concerning(value: int) -> bool:
    """``True`` if this looks like a 32-bit build (maxsize ~2^31)."""
    return value < _EXPECTED_MAXSIZE_64BIT


def _render(snap: _RuntimeSnapshot) -> str:
    lines = ["⚙ <b>Interpreter runtime tunables</b>", ""]

    rec_marker = " ⚠" if _recursion_concerning(snap.recursion_limit) else ""
    lines.append(
        f"  • <b>recursionlimit:</b> "
        f"<code>{snap.recursion_limit}</code> "
        f"<i>(default {_DEFAULT_RECURSION_LIMIT})</i>{rec_marker}"
    )

    sw_marker = " ⚠" if _switch_interval_concerning(snap.switch_interval) else ""
    lines.append(
        f"  • <b>switchinterval:</b> "
        f"<code>{snap.switch_interval * 1000:.3f}ms</code> "
        f"<i>(default {_DEFAULT_SWITCH_INTERVAL * 1000:.3f}ms)</i>"
        f"{sw_marker}"
    )

    if not snap.int_max_str_digits_available:
        lines.append(
            "  • <b>int_max_str_digits:</b> "
            "<code>unavailable</code> "
            "<i>(interpreter lacks CVE-2020-10735 limit)</i>"
        )
    else:
        ims_marker = (
            " ⚠"
            if _int_max_str_digits_concerning(
                snap.int_max_str_digits,
                snap.int_max_str_digits_available,
            )
            else ""
        )
        # Render 0 as "disabled" so an operator doesn't have to
        # remember that 0-means-off; the number alone is ambiguous
        # (could be misread as "no digits allowed").
        if snap.int_max_str_digits == 0:
            value_repr = "<code>0</code> <i>(disabled)</i>"
        else:
            value_repr = f"<code>{snap.int_max_str_digits}</code>"
        lines.append(
            f"  • <b>int_max_str_digits:</b> {value_repr} "
            f"<i>(default {_DEFAULT_INT_MAX_STR_DIGITS})</i>"
            f"{ims_marker}"
        )

    ms_marker = " ⚠" if _maxsize_concerning(snap.maxsize) else ""
    lines.append(
        f"  • <b>maxsize:</b> <code>{snap.maxsize}</code> "
        f"<i>(64-bit expected {_EXPECTED_MAXSIZE_64BIT})</i>"
        f"{ms_marker}"
    )

    lines.append("")
    lines.append(
        "<i>⚠ markers: recursionlimit raised &gt; 2× default "
        "(C-stack overflow risk), switchinterval &gt; 4× default "
        "(GIL handoff latency hit), int_max_str_digits disabled or "
        "raised (CVE-2020-10735 DoS surface), maxsize below 64-bit "
        "expectation (32-bit build slipped through).</i>"
    )
    return "\n".join(lines)


async def handle_admin_runtime(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_runtime; silently dropped"
        )
        return
    snap = _capture()
    await message.answer(_render(snap))
    log.bind(user_id=user.id).info("/admin_runtime rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.runtime")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_runtime(message, settings)

    router.message.register(_entry, Command("admin_runtime", ignore_case=True))
    return router
