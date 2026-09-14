"""Regression guard: a command in a photo caption keeps its arguments.

aiogram's ``Command`` filter matches on ``message.text or
message.caption`` (``aiogram/filters/command.py``), so ``/ban 7d спам``
typed under a screenshot routes to ``handle_ban`` exactly like the typed
form. Twenty-six functions then split ``message.text``, which a caption
message leaves ``None``, and parsed zero arguments — taking their
no-argument branch:

* ``handle_ban`` — no duration token means PERMANENT by design
  (documented in the FAQ), so the admin who asked for seven days got
  forever, and the audit row recorded an empty ``reason``. Attaching the
  offending screenshot is the ordinary way to ban someone, which makes
  this the common path rather than a corner case.
* ``handle_mute`` — fell back to the group's default length.
* ``handle_clear`` — deleted ``DEFAULT_CLEAR`` messages, not the number
  asked for.
* ``games._is_stake_roll`` and its three siblings are FILTERS, so a
  caption ``/roll 100 4`` stopped matching its own registration and
  routed to a different handler entirely.

Task #96 fixed the same blind spot one layer up (the rank gate). The fix
here is one shared narrowing,
:func:`~telegram_invite_bot.utils.aiogram.command_body`, and this file
keeps it in place from three angles: the helper's own behaviour, the
real routing filters fed a caption, and an AST scan so a new command
handler cannot reintroduce the bare ``message.text`` read.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from telegram_invite_bot.handlers.ai import _group_context
from telegram_invite_bot.handlers.games import (
    _is_stake_flip,
    _is_stake_roll,
    _is_vanity_flip,
    _is_vanity_roll,
)
from telegram_invite_bot.utils.aiogram import command_body

if TYPE_CHECKING:
    from aiogram.types import Message

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"
HANDLERS = SRC_ROOT / "handlers"


def _msg(*, text: str | None = None, caption: str | None = None) -> Message:
    return cast("Message", SimpleNamespace(text=text, caption=caption))


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


def test_command_body_prefers_the_text_body() -> None:
    assert command_body(_msg(text="/ban 7d")) == "/ban 7d"


def test_command_body_falls_back_to_the_caption() -> None:
    assert command_body(_msg(caption="/ban 7d")) == "/ban 7d"


def test_command_body_never_returns_none() -> None:
    """Every call site splits the result, so ``None`` would be a crash."""
    assert command_body(_msg()) == ""


# ---------------------------------------------------------------------------
# The routing filters — these decide WHICH handler a caption reaches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("predicate", "body"),
    [
        (_is_vanity_roll, "/roll 4"),
        (_is_vanity_flip, "/flip орёл"),
        (_is_stake_roll, "/roll 100 4"),
        (_is_stake_flip, "/flip 100 орёл"),
    ],
)
def test_game_routing_filters_read_a_caption(predicate: Any, body: str) -> None:
    """A caption form must match the same registration as the typed one.

    These four run as aiogram filters, so a miss here does not produce a
    usage message — the update quietly routes somewhere else (or
    nowhere), and the player's bet is simply ignored.
    """
    assert predicate(_msg(text=body)) is True, "typed form regressed"
    assert predicate(_msg(caption=body)) is True, "caption form does not route"


# ---------------------------------------------------------------------------
# The replied-to message is a caption source too
# ---------------------------------------------------------------------------


def _reply_msg(*, text: str | None = None, caption: str | None = None) -> Message:
    """A group message replying to one carrying ``text``/``caption``."""
    return cast(
        "Message",
        SimpleNamespace(
            chat=SimpleNamespace(type="supergroup", title="G"),
            reply_to_message=SimpleNamespace(text=text, caption=caption),
        ),
    )


@pytest.mark.parametrize("field", ["text", "caption"])
def test_group_context_reads_the_replied_body_either_way(field: str) -> None:
    """Asking the bot about a picture must not arrive as empty context.

    Replying to the photo someone posted and addressing the bot is the
    natural way to ask about it, and a photo keeps its words in
    ``caption`` — reading only ``text`` handed the model no context at
    all while the user believed they had given it plenty.
    """
    context = _group_context(_reply_msg(**{field: "смотрите какой график"}))

    assert context is not None
    assert context.reply_text == "смотрите какой график"


def test_group_context_has_no_reply_text_without_a_body() -> None:
    """A reply to a bare sticker or photo still yields a usable context."""
    context = _group_context(_reply_msg())

    assert context is not None
    assert context.reply_text is None


# ---------------------------------------------------------------------------
# AST scan: no command handler may read ``message.text`` on its own
# ---------------------------------------------------------------------------

#: Audited functions that legitimately read a ``.text`` attribute which
#: is NOT the command body. Each is allowed for a reason, not for
#: convenience: none of them parses the invoking message's arguments.
_ALLOWED: dict[str, str] = {
    # ``ai.py::_group_context`` used to sit here: it reads the *replied-to*
    # message rather than our own arguments, so the scanner's finding was
    # never about lost command arguments. It came off the list when the
    # read grew its ``or .caption`` fallback — which is what the scanner
    # looks for, so the entry became stale by being fixed.
    "support.py::handle_my_tickets": "reads ticket.text from the database",
    "support.py::handle_admin_tickets": "reads ticket.text from the database",
}


def _has_command_filter(node: ast.Call) -> bool:
    for arg in [*node.args, *(kw.value for kw in node.keywords)]:
        if isinstance(arg, ast.Call):
            fn = arg.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name == "Command":
                return True
    return False


def _command_entrypoints(tree: ast.AST) -> set[str]:
    """Names registered against a ``Command(...)`` filter in this module.

    Every positional ``Name`` of a ``register(...)`` call counts, not
    just the handler: the extra positional arguments are additional
    filters, and a *filter* that reads ``message.text`` decides which
    handler a caption command routes to.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "register"
            and _has_command_filter(node)
        ):
            names.update(a.id for a in node.args if isinstance(a, ast.Name))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and _has_command_filter(dec):
                    names.add(node.name)
    return names


def _defs(tree: ast.AST) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            out.setdefault(node.name, node)
    return out


def _callees(fn: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _reachable(tree: ast.AST, roots: set[str], *, depth: int = 4) -> set[str]:
    """Same-module functions reachable from ``roots``.

    Most handlers are registered as a thin closure that forwards to a
    module-level ``handle_*``, and the argument parsing lives one or two
    hops down from there — a scan that stopped at the registered name
    would see nothing at all.
    """
    defs = _defs(tree)
    seen = {r for r in roots if r in defs}
    frontier = set(seen)
    for _ in range(depth):
        nxt = {
            callee
            for name in frontier
            for callee in _callees(defs[name])
            if callee in defs and callee not in seen
        }
        if not nxt:
            break
        seen |= nxt
        frontier = nxt
    return seen


def _attr_reads(fn: ast.AST, attr: str) -> bool:
    return any(
        isinstance(node, ast.Attribute) and node.attr == attr and isinstance(node.ctx, ast.Load)
        for node in ast.walk(fn)
    )


def _scan() -> tuple[list[str], int]:
    """Return (offenders, number of command registrations walked)."""
    offenders: list[str] = []
    registrations = 0
    for path in sorted(HANDLERS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots = _command_entrypoints(tree)
        registrations += len(roots)
        if not roots:
            continue
        defs = _defs(tree)
        for name in sorted(_reachable(tree, roots)):
            fn = defs[name]
            if not _attr_reads(fn, "text") or _attr_reads(fn, "caption"):
                continue
            offenders.append(f"{path.name}::{name}")
    return offenders, registrations


def test_no_command_handler_parses_message_text_alone() -> None:
    offenders, registrations = _scan()
    # Non-vacuity: the walk has to have found the command surface. The
    # bot registers a few hundred commands; a number near zero means the
    # registration shape changed and the scan is looking at nothing.
    assert registrations > 100, (
        f"the scan reached only {registrations} command registrations — "
        f"the walk is broken, not the code"
    )

    unexplained = [o for o in offenders if o not in _ALLOWED]
    assert not unexplained, (
        "these command handlers read message.text without a caption "
        "fallback, so a command typed under a photo loses its arguments; "
        "route the read through utils.aiogram.command_body, or add the "
        f"function to _ALLOWED with a reason: {unexplained}"
    )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlisted site that no longer reads ``.text`` is dead weight
    hiding the next real one."""
    offenders, _ = _scan()
    stale = set(_ALLOWED) - set(offenders)
    assert not stale, (
        f"_ALLOWED lists {sorted(stale)}, but the scan no longer finds a "
        f"bare .text read there — drop the entry"
    )


# ---------------------------------------------------------------------------
# Guard the guard
# ---------------------------------------------------------------------------

_SYNTHETIC = """
async def handle_ban(message, bot):
    return (message.text or "").split()


async def handle_kick(message, bot):
    return (message.text or message.caption or "").split()


async def _ban(message, bot):
    await handle_ban(message, bot)


async def _kick(message, bot):
    await handle_kick(message, bot)


def _is_stake(message):
    return len((message.text or "").split()) > 2


async def _not_a_command(message, bot):
    return message.text


def setup(router):
    router.message.register(_ban, Command("ban"), group_filter)
    router.message.register(_kick, Command("kick"), group_filter, _is_stake)
    router.message.register(_not_a_command, F.text.startswith("!"))
"""


def test_the_scan_discriminates() -> None:
    """The matcher must see a real offender, clear a real fix, follow the
    thin-closure delegation, treat a routing filter as in scope, and not
    drag in text handlers that were never registered as commands."""
    tree = ast.parse(_SYNTHETIC)
    roots = _command_entrypoints(tree)
    assert {"_ban", "_kick", "_is_stake"} <= roots

    reach = _reachable(tree, roots)
    assert "handle_ban" in reach, "delegation through the closure is not followed"
    assert "_is_stake" in reach, "a routing filter is not treated as in scope"
    assert "_not_a_command" not in reach, "a non-command text handler leaked in"
    # ``group_filter`` is a Name but not a function — it must be dropped
    # rather than crash the lookup.
    assert "group_filter" not in reach

    defs = _defs(tree)
    assert _attr_reads(defs["handle_ban"], "text")
    assert not _attr_reads(defs["handle_ban"], "caption")
    assert _attr_reads(defs["handle_kick"], "caption"), "a real fix reads as unfixed"
