"""A translated string must never be passed through ``html.escape``.

Every catalogue string in every language is already validated as
Telegram HTML by ``test_i18n_html_safety.py`` — it renders ``t(key,
lang)`` for the whole key set and refuses anything the Bot API's parser
would reject. So the defensive reading ("escape it in case a translator
writes a stray ``<``") describes a state the suite does not allow to
exist, and the escape buys nothing.

What it costs is real. The catalogue is full of deliberate markup —
``h_broadcast_preview`` carries ``<b>всем</b>``, and dozens of keys do
the same — so escaping a catalogue string turns intended bold into
literal ``&lt;b&gt;``. The two instances this guard was written for
were both no-ops only because the strings they escaped happened to hold
no special characters:

* ``handlers/top.py`` escaped the ``/top`` period label, three lines
  below a docstring that says in as many words that nothing there needs
  escaping;
* ``handlers/rp.py`` escaped ``rp18_enabled`` inside an f-string that
  wraps it in ``<b>`` anyway, so an intended tag would have been both
  literalised and nested.

Both were removed; this keeps them from coming back, and with them the
false signal — an ``html.escape`` is how this codebase marks a value as
attacker-controlled, and spending that mark on a constant makes the
genuine ones harder to find.

The check reads three shapes: ``html.escape(t(...))`` written out; the
one that actually shipped, where the ``t(...)`` result is bound to a
local first and the escape is applied to the name; and the ternary
``x = t(a) if cond else t(b)``, which is how ``/top`` picks its period
label and is still a catalogue string on either branch.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"


def _is_translate_call(node: ast.AST) -> bool:
    """``t(...)`` — the project's only translation entry point."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t"


def _yields_translation(node: ast.AST) -> bool:
    """True for an expression that can only evaluate to a catalogue string.

    A bare ``t(...)`` and a ternary between two of them are the same
    thing to this guard: whichever branch runs, the value came out of
    the catalogue. ``/top`` writes the second shape — one key for
    "today", another for "N days" — precisely because the choice is a
    phrase a locale reshapes, so treating it as unknown would miss the
    escape the guard was written for.
    """
    if _is_translate_call(node):
        return True
    return isinstance(node, ast.IfExp) and all(
        _yields_translation(branch) for branch in (node.body, node.orelse)
    )


def _is_escape_call(node: ast.AST) -> ast.Call | None:
    """``html.escape(...)`` with exactly one positional argument."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "escape"
        and isinstance(func.value, ast.Name)
        and func.value.id == "html"
        and len(node.args) == 1
    ):
        return node
    return None


def _translated_locals(scope: ast.AST) -> set[str]:
    """Names bound to a ``t(...)`` result anywhere in this scope.

    Deliberately flat rather than flow-sensitive: a name that ever holds
    a catalogue string is not a name to escape, whichever branch
    assigned it. Nested functions are walked too — their bindings are
    reported against the same file, which is all the failure message
    needs.
    """
    names: set[str] = set()
    for node in ast.walk(scope):
        value = getattr(node, "value", None)
        if value is None or not _yields_translation(value):
            continue
        if isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_no_source_file_escapes_a_translated_string() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bound = _translated_locals(tree)
        for node in ast.walk(tree):
            call = _is_escape_call(node)
            if call is None:
                continue
            arg = call.args[0]
            if _yields_translation(arg):
                what = "html.escape(t(...))"
            elif isinstance(arg, ast.Name) and arg.id in bound:
                what = f"html.escape({arg.id}), and {arg.id} holds a t(...) result"
            else:
                continue
            offenders.append(f"{path.relative_to(_SRC.parents[1])}:{call.lineno}: {what}")

    assert not offenders, (
        "a catalogue string was escaped — the catalogue is validated HTML and "
        "carries intended markup, so this literalises tags the translator meant:\n"
        + "\n".join(offenders)
    )
