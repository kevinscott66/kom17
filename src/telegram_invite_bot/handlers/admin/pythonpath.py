"""``/admin_pythonpath`` — :data:`sys.path` import-resolution snapshot.

Complements /admin_python (interpreter identity) and /admin_modules
(installed package versions) with the **resolution order** the
interpreter is actually applying when it imports a module. Two
versions of the same package can live on disk simultaneously (an
egg-info dropped into the working directory + the real install in
site-packages); the one that wins is whichever ``sys.path`` entry
comes first. The card surfaces that order so an operator can see
exactly which copy ``import telegram_invite_bot`` is resolving.

Why an operator wants this:

* Shadowing audit. A leftover ``./telegram_invite_bot/`` directory
  in the working dir (e.g. from an aborted ``rsync``) silently
  takes precedence over the site-packages install. The bot runs,
  but with stale code. ``sys.path[0] == ''`` (the CPython default
  for interactive / ``-m`` invocations) is what makes the shadow
  win, and the card surfaces it so the operator can chase the
  empty-string entry rather than chase the mystery of "the
  deploy ran but the behaviour didn't update".
* Stray ``PYTHONPATH``. The container shell's profile might leak
  a developer's ``PYTHONPATH=/home/me/work`` into the bot's
  process. That entry won't show as a file on disk — but
  ``sys.path`` carries it. The card distinguishes "this entry
  exists on disk" from "this entry is in sys.path but doesn't
  resolve" so the operator can spot the rogue env var.
* "Where does the venv start?" — first ``site-packages`` entry
  pins the venv root. After ``apt install python3-libfoo`` an
  operator wants to know which site-packages — venv or system —
  picked up the new package; the per-entry kind tag answers that.

Reads :data:`sys.path` once. We tag each entry with a coarse
kind — "<cwd>" for empty string, "site-packages" for paths
containing that segment, "missing" for paths that don't exist on
disk, otherwise "dir". The tagging is the diagnostic; the path
list alone is noise without it.

Same posture as every other ``/admin_*``: silent-drop for non-devs,
private-only at the router level.
"""

from __future__ import annotations

import html
import os
import sys
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.pythonpath")


# Rendered-row cap. A healthy venv has 6-10 ``sys.path`` entries;
# something past 20 means the operator should be looking at the
# raw env (this card's value is the kind-tagging, not the path
# strings themselves) or chasing a leaked PYTHONPATH. Truncation
# tail keeps the card under Telegram's 4096-char limit even on a
# pathologically-long path.
_MAX_ENTRIES = 25


class _PathEntry:
    """One sys.path entry with operator-loaded interpretation.

    ``kind`` is the load-bearing tag — "<cwd>" / "site-packages"
    / "missing" / "dir" — that converts a raw path into the
    diagnostic signal. ``index`` preserves the resolution order
    because order is what makes shadowing happen; rendering in
    sorted form would erase the entire reason for the card.
    """

    __slots__ = ("index", "kind", "path")

    def __init__(self, *, index: int, path: str, kind: str) -> None:
        self.index = index
        self.path = path
        self.kind = kind


def _classify(path: str) -> str:
    """Coarse kind-tag for one sys.path entry.

    Empty-string is "<cwd>" — the implicit-cwd entry that makes
    accidental shadowing possible. CPython removes it under
    ``-I`` / ``-P`` but a normal ``python -m`` invocation keeps
    it. Surfacing it explicitly so an operator can spot a
    shadowed package as soon as they scan the list.

    "site-packages" is what the venv (and the system Python) writes
    its installs into. The first such entry pins the install root
    an operator is most likely to care about.

    "missing" is a real prod regression — a PYTHONPATH that points
    at a directory the container doesn't have. CPython silently
    skips missing entries on import, so the symptom is just "the
    package can't be found" with no hint that the path entry
    itself was the culprit.
    """
    if path == "":
        # Implicit current-working-directory entry. The shadowing
        # vector — render it loud so the operator can chase the
        # accidental-shadow case without learning a folklore rule
        # about CPython's path bootstrap.
        return "<cwd>"
    if "site-packages" in path:
        return "site-packages"
    if not os.path.exists(path):
        return "missing"
    return "dir"


def _capture() -> tuple[list[_PathEntry], str]:
    """Snapshot sys.path entries + raw PYTHONPATH env.

    Reading :data:`sys.path` directly (rather than via
    ``sys.path.copy()``) is fine — we never mutate, and the list
    is updated only on ``import`` statements, which can't happen
    while we're inside the synchronous handler body.

    ``PYTHONPATH`` is returned alongside so the operator can match
    rogue entries in ``sys.path`` back to the env var that put
    them there. A truthy difference between the two is the
    "leaked dev env var" diagnostic.
    """
    entries = [_PathEntry(index=i, path=p, kind=_classify(p)) for i, p in enumerate(sys.path)]
    return entries, os.environ.get("PYTHONPATH") or "<unset>"


def _render(entries: list[_PathEntry], pythonpath_env: str) -> str:
    lines = ["🐍 <b>sys.path</b>", ""]
    lines.append(f"<b>PYTHONPATH env:</b> <code>{html.escape(pythonpath_env)}</code>")
    lines.append(f"<b>entries:</b> <code>{len(entries)}</code>")
    lines.append("")
    if not entries:
        # ``sys.path == []`` is unreachable in practice (CPython
        # always populates it) but the empty branch keeps the
        # renderer total over its input rather than assuming a
        # nonempty invariant the caller has to maintain.
        lines.append("<i>(empty — should be unreachable)</i>")
        return "\n".join(lines)
    for entry in entries[:_MAX_ENTRIES]:
        # ``<cwd>`` and ``missing`` get markers — they're the
        # operationally-suspicious states. ``site-packages`` /
        # ``dir`` are healthy and render bare so the operator
        # can scan for the markers without the healthy rows
        # competing for attention.
        marker = ""
        if entry.kind == "<cwd>":
            marker = " ⚠ (shadowing vector)"
        elif entry.kind == "missing":
            marker = " ⚠ (silently skipped on import)"
        # Empty paths render as a literal ``""`` so the operator
        # sees them as a value rather than a missing cell.
        display = entry.path if entry.path else '""'
        lines.append(
            f"  <code>{entry.index}.</code> <code>{html.escape(display)}</code> "
            f"<i>[{html.escape(entry.kind)}]</i>{marker}"
        )
    if len(entries) > _MAX_ENTRIES:
        remaining = len(entries) - _MAX_ENTRIES
        lines.append(f"  <i>… and {remaining} more</i>")
    return "\n".join(lines)


async def handle_admin_pythonpath(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_pythonpath; silently dropped"
        )
        return
    entries, env = _capture()
    await message.answer(_render(entries, env))
    log.bind(user_id=user.id).info("/admin_pythonpath rendered")


def build_router(settings: Settings) -> Router:
    router = Router(name="admin.pythonpath")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_pythonpath(message, settings)

    router.message.register(_entry, Command("admin_pythonpath", ignore_case=True))
    return router
