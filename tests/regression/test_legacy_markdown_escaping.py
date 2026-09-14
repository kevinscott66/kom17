"""Regression guard: legacy Markdown cards never interpolate raw user text.

The legacy half (``bot.py``) sends almost everything with
``parse_mode="Markdown"``, and much of what it interpolates is text the
user picked: the nick from ``/nick``, the city from ``/city``, a
Telegram ``first_name``, a group title. Markdown has no notion of
"data" — a metacharacter in that text is markup, and the failure is
not cosmetic:

* ``/nick [Поддержка](https://evil.example)`` made the BOT post a live
  link into the group under its own name — a phishing seam wearing the
  bot's credibility.
* A single ``*`` or ``_`` in ONE member's nick unbalanced the whole
  message, so Telegram rejected the ``/top`` and ``/stata`` tables and
  **nobody in the chat** could see the leaderboard. One user could
  silently break a shared surface for everyone.

Three of these sites had *hand-rolled* escaping — a ``.replace()``
chain covering ``\\``, ``[`` and ``]`` — which reads like a defence,
passes review, and covers exactly half the metacharacters. That is the
specific failure mode this file exists to prevent: escaping that looks
present and isn't complete.

Three independent checks
========================

1. :func:`test_escape_helper_covers_every_markdown_metacharacter` — the
   helper itself is correct. Run for real (see below), not read.
2. :func:`test_user_text_in_markdown_fstrings_is_escaped` — every
   watch-listed user-text variable rendered into a Markdown f-string
   traces back to the helper inside its own function.
3. :func:`test_no_hand_rolled_markdown_escaping_survives` — nobody
   re-introduces a partial ``.replace()`` chain; escaping lives in one
   place or not at all.

Why a source scan rather than calling the handlers
--------------------------------------------------
``bot.py`` is a ~40k-line telebot monolith that builds a ``TeleBot``,
opens databases and starts threads at import time — importing it from a
test is not on the table. Check 1 works around that honestly: it lifts
the helpers' own AST subtrees out of the module, re-emits them as a
throwaway module and imports THAT, so the assertion exercises the
production definitions rather than a copy that could drift. Checks 2
and 3 are structural by necessity; both fail with a precise
``file:line`` list.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

LEGACY = Path(__file__).resolve().parents[2] / "bot.py"

#: The characters Telegram's legacy ("Markdown", not V2) parser treats
#: as markup inside ordinary text and inside ``[link text]``.
MARKDOWN_METACHARACTERS = ("\\", "_", "*", "`", "[", "]")

#: The sanctioned escape helpers — the only functions allowed to own a
#: character table. Anything else escaping by hand is a bug waiting to
#: be half-written (see check 3). ``escape_markdown`` is the stricter
#: MarkdownV2 variant; a value laundered through it is safe for the
#: legacy parser too, so check 2 accepts it as provenance as well.
ESCAPE_HELPERS = frozenset({"_escape_markdown_caption", "_escape_html_caption", "escape_markdown"})

#: Literal fragments that mark an f-string as carrying Markdown markup.
#: ``](`` catches the ``[text](url)`` form, where the metacharacters are
#: just as live as in a ``**bold**`` run.
MARKDOWN_MARKERS = ("**", "__", "`", "](")

#: Variables known to hold user-chosen text at a Markdown call site.
#: Deliberately a watch-list rather than "every name": the point is a
#: guard with zero false positives that fails loudly on the sites we
#: actually audited, not a heuristic somebody would soon start muting.
USER_TEXT_NAMES = frozenset(
    {
        "display_name",  # /start greeting + /profile "Имя:" row
        "city_display",  # /city value on the profile card
        "city_display_p",
        "city_display_o",
        "city_display_fb",
        "safe_nick",  # /nick value on the profile card
        "safe_name",  # member name in /top and /stata tables
        "name_for_display",
        "from_name",  # both sides of the transfer receipt
        "target_name",
    }
)

#: Audited ``.replace()`` calls on a Markdown metacharacter that are NOT
#: escaping, keyed by their unparsed source so the waiver survives the
#: line numbers moving. Each one is a deliberate non-escape:
#:
#: * the ``p2p_price_market_`` line parses callback data, not display text;
#: * the ``[:35]`` title lands in an ``InlineKeyboardButton`` label, where
#:   there is no parse_mode at all — escaping it would show the user a
#:   literal backslash;
#: * the backtick strip runs before the row reaches the escaped renderer,
#:   and drops the character rather than smuggling it through.
AUDITED_NON_ESCAPES = frozenset(
    {
        "call.data.replace('p2p_price_market_', '').replace('_', '.')",
        "(g_title or str(g_chat_id))[:35].replace('*', '·')",
        "(member.user.first_name or f'ID{user_id}').replace('`', '')",
    }
)


def _legacy_tree() -> ast.Module:
    return ast.parse(LEGACY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def legacy_escapers(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Import the legacy escape helpers without importing ``bot.py``.

    Lifts the two ``FunctionDef`` subtrees out of the parsed module and
    re-emits them as a standalone module. They depend on nothing but
    builtins, so what runs is the production definition — if someone
    trims the character table, check 1 fails on behaviour rather than on
    a pattern match.
    """
    wanted = {"_escape_markdown_caption", "_markdown_link_escape"}
    found = [
        node
        for node in _legacy_tree().body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    assert {node.name for node in found} == wanted, (
        f"expected {sorted(wanted)} at module level of bot.py, found "
        f"{sorted(node.name for node in found)}"
    )

    module_path = tmp_path_factory.mktemp("legacy_md") / "legacy_escapers.py"
    module_path.write_text(
        "\n\n".join(ast.unparse(node) for node in found) + "\n", encoding="utf-8"
    )
    spec = importlib.util.spec_from_file_location("legacy_escapers", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_escape_helper_covers_every_markdown_metacharacter(legacy_escapers: Any) -> None:
    """``_escape_markdown_caption`` must neutralise the FULL set.

    The bug this pins is a partial table. Every hand-rolled site that
    got replaced covered ``\\``, ``[`` and ``]`` while letting ``*``,
    ``_`` and ``` ` ``` through — precisely the half that unbalances a
    message. Assert per character so a trimmed table names the
    character it dropped.
    """
    escape: Callable[[str], str] = legacy_escapers._escape_markdown_caption
    link_escape: Callable[[str], str] = legacy_escapers._markdown_link_escape

    for char in MARKDOWN_METACHARACTERS:
        escaped = escape(f"ник{char}ник")
        assert escaped == f"ник\\{char}ник", f"{char!r} is not escaped: {escaped!r}"
        # The link-text helper feeds ``[…](url)``, where the same
        # characters are live. It must not be a weaker variant — that
        # is exactly how the rating board ended up unparseable.
        assert link_escape(f"группа{char}") == f"группа\\{char}"

    # The payload that made the bot post someone else's link.
    assert escape("[Поддержка](https://evil.example)") == ("\\[Поддержка\\](https://evil.example)")


def _escaped_locals(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names assigned from an escape helper inside this function."""
    clean: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        callee = node.value.func
        if isinstance(callee, ast.Name) and callee.id in ESCAPE_HELPERS:
            clean.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return clean


def test_user_text_in_markdown_fstrings_is_escaped() -> None:
    """No watch-listed user-text name reaches a Markdown f-string raw.

    Provenance is resolved per function: a name counts as safe only if
    *that* function assigned it from an escape helper. A neighbouring
    function escaping a same-named variable proves nothing, and the
    whole class of bug here is a copied block that kept the name and
    dropped the call.
    """
    offenders: list[str] = []
    for func in ast.walk(_legacy_tree()):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        clean = _escaped_locals(func)
        for node in ast.walk(func):
            if not isinstance(node, ast.JoinedStr):
                continue
            literal = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if not any(marker in literal for marker in MARKDOWN_MARKERS):
                continue
            for part in node.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                rendered = part.value
                if not isinstance(rendered, ast.Name):
                    continue
                if rendered.id in USER_TEXT_NAMES and rendered.id not in clean:
                    offenders.append(
                        f"bot.py:{part.lineno} in {func.name}(): "
                        f"{{{rendered.id}}} is interpolated into a Markdown "
                        f"string without _escape_markdown_caption"
                    )

    assert not offenders, "raw user text in Markdown output:\n" + "\n".join(offenders)


def test_no_hand_rolled_markdown_escaping_survives() -> None:
    """Escaping goes through the helper — never through ``.replace()``.

    Check 2 only sees the names it was told about, so on its own it
    ages badly. This one is the general net: any ``.replace()`` keyed on
    a Markdown metacharacter is either the audited non-escape it is
    declared to be, or a hand-rolled escape that will be incomplete the
    way all three of the originals were.
    """
    tree = _legacy_tree()
    # ``_escape_markdown_caption`` IS the character table, so its own
    # ``.replace()`` is the one legitimate occurrence. Skip it by
    # position rather than by source text, which would also mute a copy.
    helper_lines = {
        child.lineno
        for func in tree.body
        if isinstance(func, ast.FunctionDef) and func.name in ESCAPE_HELPERS
        for child in ast.walk(func)
        if hasattr(child, "lineno")
    }

    # Keyed by line: a chain like ``x.replace(a).replace(b).replace(c)``
    # is three nested Calls and would otherwise be reported three times,
    # each a truncated prefix of the last. Keep the longest — that is
    # the whole chain, which is what a reader needs to see.
    worst: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.lineno in helper_lines:
            continue
        callee = node.func
        if not isinstance(callee, ast.Attribute) or callee.attr != "replace":
            continue
        if len(node.args) != 2:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        if first.value not in MARKDOWN_METACHARACTERS:
            continue
        source = ast.unparse(node)
        if source in AUDITED_NON_ESCAPES:
            continue
        if len(source) > len(worst.get(node.lineno, "")):
            worst[node.lineno] = source

    offenders = [f"bot.py:{line}: {src}" for line, src in sorted(worst.items())]
    assert not offenders, (
        "hand-rolled Markdown escaping (use _escape_markdown_caption, or add an "
        "audited waiver to AUDITED_NON_ESCAPES):\n" + "\n".join(offenders)
    )
