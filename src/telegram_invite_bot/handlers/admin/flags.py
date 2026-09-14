"""``/admin_flags`` — :data:`sys.flags` interpreter-mode snapshot.

Complements /admin_python (interpreter identity), /admin_loop (event-
loop config), and /admin_warnings (filter list) with the last
mode-flipping surface that controls how the running interpreter
behaves: the immutable ``sys.flags`` namedtuple set at process start.

Why an operator wants this:

* "Why is the bot 30 % slower today?" — a stray ``PYTHONDEVMODE=1``
  in the systemd unit (or worse, ``-X dev`` baked into a Dockerfile
  ENTRYPOINT) turns on a basketful of debug-only behaviours: the
  default warning filter goes from ``default`` to ``default::``, the
  asyncio loop runs in debug mode, ``ResourceWarning`` is enabled,
  and ``faulthandler`` is wired up. Each one has a measurable cost;
  together they're the difference between "comfortable" and "the
  CPU graph is climbing".
* "Did this build go out with assertions stripped?" — the
  ``optimize`` flag is 0 / 1 / 2 corresponding to ``python``,
  ``python -O``, ``python -OO``. A prod build accidentally shipped
  with ``-O`` silently disables every ``assert`` in our codepath
  including the contract checks in the DB safety listener; the
  card surfaces the integer so the operator can verify.
* ``no_user_site`` — a security-relevant flag. If a deployment
  pipeline left ``-s`` off, the bot's interpreter will read
  ``~/.local/lib/...`` site-packages, which on a shared host is
  a real shadowing vector for whoever owns the home directory.
* ``hash_randomization`` (PYTHONHASHSEED) — should be on in prod.
  A misconfigured systemd unit can pin the seed for reproducible
  bug-hunt builds and forget to flip it back; the flag surfaces
  the state so the operator catches the omission.

Reads :data:`sys.flags` once. The namedtuple is immutable for the
process lifetime — once we sample it, the values can't drift between
sample and render. Same posture as every other ``/admin_*``: silent-
drop for non-devs, private-only at the router level.
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


log = logger.bind(component="handlers.admin.flags")


# Flags we explicitly surface, in operational-relevance order.
# The full ``sys.flags`` namedtuple carries ~20 entries; rendering
# all of them would bury the load-bearing ones (debug, optimize,
# dev_mode, no_user_site, hash_randomization) under noise like
# ``inspect`` and ``bytes_warning`` that no operator triages on.
# Truncation is documentation: a future change that needs a new
# flag should add it to this tuple, not the renderer.
_FLAGS_OF_INTEREST: tuple[str, ...] = (
    # Performance / behaviour switches.
    "debug",
    "optimize",
    "dev_mode",
    "verbose",
    # Security / isolation.
    "no_user_site",
    "no_site",
    "isolated",
    "ignore_environment",
    "safe_path",
    # Hashing.
    "hash_randomization",
)


class _FlagRow:
    """One sys.flags entry with operator-loaded interpretation.

    ``raw_value`` is the integer the interpreter reports (most flags
    are 0/1, ``optimize`` is 0/1/2). ``concerning`` is computed at
    capture time so the renderer doesn't have to know per-flag
    semantics — pushing the "is this value worth a warning?"
    decision into _capture keeps _render trivial and unit-testable.
    """

    __slots__ = ("concerning", "name", "raw_value")

    def __init__(self, *, name: str, raw_value: int, concerning: bool) -> None:
        self.name = name
        self.raw_value = raw_value
        self.concerning = concerning


def _is_concerning(name: str, value: int) -> bool:
    """Per-flag "operationally suspicious" rule.

    The defaults are flipped from the obvious "non-zero is bad" because
    several flags are *protective* when set:

    * ``no_user_site`` on (1) is the secure state — user site-packages
      get loaded if the flag is 0. We do NOT want it 0 in prod.
    * ``hash_randomization`` on (1) is the secure state. 0 means
      a pinned PYTHONHASHSEED, which is fine in dev but a DoS
      vector in prod (predictable hash collisions on user-controlled
      strings).

    The other flags follow the obvious "non-zero is suspicious":
    ``debug`` / ``dev_mode`` / ``verbose`` / ``optimize`` are
    development modes that have measurable runtime costs or behaviour
    changes; ``ignore_environment`` set in a process that depends on
    env vars (we do — everything routes through pydantic-settings) is
    a config-loading bug.

    ``no_site`` / ``isolated`` / ``safe_path`` are "the deployer made
    a choice"; we surface them but don't flag them either way. An
    operator can read the value and decide.
    """
    if name in {"no_user_site", "hash_randomization"}:
        # Protective flags: 0 (off) is the concerning state.
        return value == 0
    if name in {"debug", "dev_mode", "verbose", "optimize"}:
        # Development modes: any non-zero is concerning in prod.
        return value != 0
    if name == "ignore_environment":
        # ``-E`` flag. We rely on env vars; any non-zero breaks us.
        return value != 0
    # ``no_site`` / ``isolated`` / ``safe_path`` — informational only.
    return False


def _capture() -> list[_FlagRow]:
    """Sample :data:`sys.flags` once.

    Defensive :func:`getattr` per flag: a future CPython that drops a
    flag we surface would otherwise crash this diagnostic. ``-1`` as
    the sentinel (rather than ``None``) keeps the row's ``raw_value``
    a real int for the formatter without a separate "unavailable"
    code path.
    """
    rows: list[_FlagRow] = []
    for name in _FLAGS_OF_INTEREST:
        value = getattr(sys.flags, name, -1)
        # The value can be a bool on some flags; coerce to int so the
        # renderer doesn't have to handle both. ``int(True) == 1``,
        # ``int(False) == 0``.
        try:
            value_int = int(value)
        except (TypeError, ValueError):
            # Truly unknown — surface as -1 so the row still renders.
            value_int = -1
        rows.append(
            _FlagRow(
                name=name,
                raw_value=value_int,
                concerning=_is_concerning(name, value_int),
            )
        )
    return rows


def _render(rows: list[_FlagRow]) -> str:
    lines = ["🚩 <b>sys.flags</b>", ""]
    for row in rows:
        marker = " ⚠" if row.concerning else ""
        lines.append(f"  • <code>{row.name}</code> = <code>{row.raw_value}</code>{marker}")
    lines.append("")
    lines.append(
        "<i>⚠ flags by intent: development-mode switches set in "
        "prod (debug/dev_mode/verbose/optimize), env-isolation "
        "breakers (ignore_environment), and protective flags left "
        "off (no_user_site=0, hash_randomization=0).</i>"
    )
    return "\n".join(lines)


async def handle_admin_flags(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_flags; silently dropped"
        )
        return
    rows = _capture()
    await message.answer(_render(rows))
    log.bind(user_id=user.id).info("/admin_flags rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.flags")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_flags(message, settings)

    router.message.register(_entry, Command("admin_flags", ignore_case=True))
    return router
