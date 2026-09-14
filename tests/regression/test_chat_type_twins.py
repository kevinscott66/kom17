"""Regression guard: a chat-type gate must never be the whole answer (#122).

A command registered with ``F.chat.type.in_(GROUP_TYPES)`` and nothing
else does not *refuse* a private invocation — it never matches one.
aiogram walks the routers, finds no handler, drops the update, and the
user gets nothing at all: no error, no hint, no clock. Several handlers
carried a comment saying private use "falls through to legacy, where
it'll get the same refusal", and that was true while the telebot
process still ran beside this one. It doesn't, so the fallthrough
landed on the floor. Silence reads as "the bot is broken" or "I typed
it wrong", and both send the user to support instead of to the chat
where the command works.

So: every command word registered under a per-registration chat-type
gate must ALSO be registered under the opposite gate, which in practice
means :func:`~telegram_invite_bot.handlers.group_only.handle_group_only`
(or an equivalent that speaks). ``handlers/report.py`` is the shape —
two chat-type-disjoint registrations of the same word, each carrying
its own filter, no router-level chat filter to shadow either.

**Scope, stated honestly.** This scan reads ``*.message.register(...)``
calls and the chat-type filter passed to each. It does NOT see
router-level ``router.message.filter(F.chat.type == ...)``, which
several modules use (``promo``, ``checks``, every ``admin/*`` module,
and the ``group_filter`` local in ``moderation``). Those needed a
different fix — a router-wide filter can't be paired per registration,
it needs a sibling router — which #123 built as
``handlers/chat_scope.with_chat_type_refusal``. Its guard is
``test_chat_scope_coverage``, which resolves the assembled tree instead
of reading source and therefore sees inherited filters, starred alias
tuples and cross-module overlaps that no static scan can. This guard
keeps the per-registration half, where reading the source says
something the tree cannot: that the twin is *written down* next to the
registration it answers.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"

GROUPS = "GROUPS"
PRIVATE = "PRIVATE"


def _chat_type_gate(node: ast.expr) -> str | None:
    """Classify a filter argument as a private/group chat-type gate.

    Deliberately textual: the call sites spell the same predicate three
    ways (``ChatType`` enum members, bare ``"group"`` strings, the
    ``GROUP_TYPES``/``GROUP_TYPE_NAMES`` constants), and
    ``core/chat_types.py`` asks each site to keep whichever style it
    already uses. Anything that mentions ``chat.type`` but doesn't
    reduce to one of the two sides returns ``None`` and is ignored —
    this guard is about the private/group split, not about every
    conceivable predicate.
    """
    src = ast.unparse(node)
    if "chat.type" not in src:
        return None
    if ".in_(" in src and "PRIVATE" not in src:
        return GROUPS
    if "==" in src and "PRIVATE" in src:
        return PRIVATE
    return None


def _command_words(node: ast.expr) -> list[str]:
    """Alias strings out of a ``Command("duel", "дуэль", ...)`` argument."""
    if not isinstance(node, ast.Call) or not ast.unparse(node.func).endswith("Command"):
        return []
    return [
        arg.value
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]


def _gates_by_command() -> dict[tuple[str, str], set[str]]:
    """Map ``(module, command word)`` → the chat-type gates it registers under."""
    gates: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = str(path.relative_to(SRC_ROOT))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not ast.unparse(node.func).endswith("message.register"):
                continue
            words: list[str] = []
            gate: str | None = None
            for arg in node.args:
                words.extend(_command_words(arg))
                gate = _chat_type_gate(arg) or gate
            for word in words:
                gates[(rel, word)].add(gate or "ANY")
    return gates


def test_scan_finds_the_known_group_only_families() -> None:
    """Guard the guard: a scan that silently matches nothing passes
    vacuously forever. These four families are the ones #122 fixed.
    """
    gates = _gates_by_command()
    for module, word in [
        ("handlers/duel.py", "duel"),
        ("handlers/challenge_commands.py", "accept"),
        ("handlers/couple_activities.py", "activities"),
        ("handlers/relations.py", "marriages"),
    ]:
        assert gates[(module, word)] == {GROUPS, PRIVATE}, (module, word)


def test_no_command_is_gated_to_one_chat_type_without_a_twin() -> None:
    """The rule itself. A word gated at the registration level must have
    a registration for the other side too — the one that speaks.
    """
    offenders = sorted(
        f"{module}:/{word} registered only under {sorted(found)}"
        for (module, word), found in _gates_by_command().items()
        if "ANY" not in found and found != {GROUPS, PRIVATE}
    )
    assert offenders == [], (
        "these commands match nothing (and answer nothing) in the other "
        "chat type — register handle_group_only, or an equivalent that "
        "replies, on the same words:\n  " + "\n  ".join(offenders)
    )
