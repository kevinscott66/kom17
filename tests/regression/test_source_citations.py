"""Regression guard: a line citation into live source must point at code.

``src/`` is densely cross-referenced. Comments and docstrings send the
reader to a specific spot in a sibling module — ``promo_repo.py:154``,
``handlers/topup.py:706`` — because the reasoning usually only holds if
you have seen that spot. Those numbers rot silently. Every line inserted
above the target shifts it, nothing recompiles, no test notices, and the
next reader lands on a closing bracket or a blank line and quietly stops
believing the comment. Documentation that has to be disbelieved is worse
than none.

What this guard decides, and what it cannot.

It resolves every ``<path>.py:<line>[-<line>]`` citation naming a file
inside the package and fails when the cited range falls off the end of
that file, or contains nothing but blank lines and structural
punctuation — a closing bracket, a lone comma, a docstring fence. Nobody
cites those on purpose, so a hit is rot by construction: no judgement
call, no argument about intent.

What it cannot see on its own is a citation that drifted onto a
*different real line*. ``middlewares/base.py:131-132``, cited from nine
modules, landed in the middle of a comment twenty-odd lines above the
``session.commit()`` it meant; a machine reading only line numbers has
no way to know that is not what the author intended.

Two further tests close most of that half.

:data:`_ANCHORED` takes the expensive targets — the ones cited from many
places, where one insert rots every citing comment at once, as happened
to ``middlewares/base`` before #1994 — and pins each range to a literal
that must appear inside it. Shift the target and this fails by name. It
does not prove the nine sites still agree with the table; it guarantees
the table's range still means what the citations say it means.

The third test needs no table. Most citations sit next to the name of
the thing they point at — ``recent_chat_actions``
(``moderation_repo.py:439-466``) — and that name is machine-checkable:
the cited range has to overlap that symbol's own definition. A citation
naming the *call site* of a function is the deliberate exception
(``_gate_blocked`` at ``handlers/checks.py:421``, called at ``:540``),
so a cited line that mentions the name itself counts as agreement. The
first sweep of it found four citations pointing at unrelated live code,
the oldest of them four refactors deep, all four silently passing every
other guard in this file.

Citations into ``bot.py`` are out of scope on purpose: the legacy
monolith is frozen, so its line numbers cannot move, and it does not
live in the package tree this walks.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import telegram_invite_bot

#: ``some/dir/mod.py:12`` or ``mod.py:12-34``.
_CITATION = re.compile(r"([A-Za-z_][A-Za-z0-9_/]*\.py):(\d+)(?:-(\d+))?")

#: Lines that carry no information for a reader who followed a pointer.
#: Kept deliberately narrow — only blanks and pure structure — so a hit
#: is never a judgement call about what the author might have meant.
_STRUCTURAL = re.compile(r"^[\s)\]}(\[{,:'\"]*$")

_ROOT = Path(telegram_invite_bot.__file__).parent


def _resolve(cited: str) -> Path | None:
    """The package file a citation names, or ``None`` if it names none.

    Citations are written at whatever depth reads well in context —
    ``promo_repo.py``, ``repositories/promo_repo.py``,
    ``db/models/economy.py`` all appear — so a bare suffix match is what
    the corpus needs. An ambiguous suffix resolves to nothing rather
    than to a guess: a wrong file would make this guard lie in the
    other direction.
    """
    direct = _ROOT / cited
    if direct.is_file():
        return direct
    matches = [p for p in _ROOT.rglob(Path(cited).name) if p.as_posix().endswith("/" + cited)]
    return matches[0] if len(matches) == 1 else None


def test_every_in_package_line_citation_points_at_something() -> None:
    rotten: list[str] = []
    for source in sorted(_ROOT.rglob("*.py")):
        for lineno, line in enumerate(source.read_text().splitlines(), 1):
            for match in _CITATION.finditer(line):
                target = _resolve(match.group(1))
                if target is None:
                    continue
                start = int(match.group(2))
                end = int(match.group(3) or match.group(2))
                body = target.read_text().splitlines()
                where = f"{source.relative_to(_ROOT)}:{lineno} cites {match.group(0)}"
                if not 1 <= start <= end <= len(body):
                    rotten.append(f"{where} — out of range ({len(body)} lines)")
                    continue
                cited = body[start - 1 : end]
                if all(_STRUCTURAL.match(text) for text in cited):
                    rotten.append(
                        f"{where} — nothing there: {' / '.join(t.strip() for t in cited)}"
                    )

    assert not rotten, "line citations that have drifted off their target:\n" + "\n".join(rotten)


#: Ranges cited from several modules at once, each pinned to a literal
#: that has to live inside it. These are the expensive ones: a single
#: inserted line above any of them rots every citing comment
#: simultaneously, which is exactly what happened to
#: ``middlewares/base`` before #1994.
_ANCHORED: dict[str, str] = {
    "middlewares/base.py:157-158": "await session.commit()",
    "db/engines.py:204-210": 'cursor.execute("BEGIN IMMEDIATE")',
    "db/engines.py:207-208": "_NON_WRITE_HEADS",
    "db/engines.py:213": "expire_on_commit=False",
    "db/session.py:132-136": "expire_on_commit=False",
    "db/pragma.py:63": "busy_timeout",
}


def test_the_multiply_cited_ranges_still_contain_what_they_promise() -> None:
    drifted: list[str] = []
    for citation, anchor in _ANCHORED.items():
        path, _, span = citation.rpartition(":")
        target = _resolve(path)
        assert target is not None, f"{citation} names no file in the package"
        start, _, stop = span.partition("-")
        body = target.read_text().splitlines()
        cited = body[int(start) - 1 : int(stop or start)]
        if not any(anchor in text for text in cited):
            found = [i + 1 for i, text in enumerate(body) if anchor in text]
            drifted.append(f"{citation} no longer holds {anchor!r} — it now lives at {found}")

    assert not drifted, (
        "a range cited from several modules has moved; re-point every citing"
        " comment, then update _ANCHORED:\n" + "\n".join(drifted)
    )


#: How far back a citation's own prose is read for the name of the thing
#: it points at. Three lines is what a wrapped comment costs: the name
#: usually opens the sentence and the number closes it.
_PROSE_WINDOW = 3

#: Names as they are written in prose here — ``backticked``, in a Sphinx
#: role, or bare with call parens. Bare words need a length floor or
#: every ``the`` and ``and`` becomes a candidate.
_NAMED = re.compile(
    r"``([A-Za-z_][A-Za-z0-9_.]*)``"
    r"|:(?:func|meth|data|class|attr):`~?([A-Za-z_][A-Za-z0-9_.]*)`"
    r"|\b([a-z_][a-z0-9_]{3,})\(\)"
)


def _definitions(target: Path) -> dict[str, tuple[int, int]]:
    """``{name: (first line, last line)}`` for every def and class.

    The first definition of a name wins. A module that reuses one name
    for two things is rare enough here that guessing the earlier one is
    better than declining to check at all.
    """
    try:
        tree = ast.parse(target.read_text())
    except SyntaxError:  # pragma: no cover — the package parses
        return {}
    spans: dict[str, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            spans.setdefault(node.name, (node.lineno, node.end_lineno or node.lineno))
    return spans


def test_a_citation_lands_inside_the_symbol_its_prose_names() -> None:
    drifted: list[str] = []
    for source in sorted(_ROOT.rglob("*.py")):
        lines = source.read_text().splitlines()
        for index, line in enumerate(lines):
            for match in _CITATION.finditer(line):
                target = _resolve(match.group(1))
                if target is None:
                    continue
                start = int(match.group(2))
                end = int(match.group(3) or match.group(2))
                spans = _definitions(target)
                body = target.read_text().splitlines()
                if not 1 <= start <= end <= len(body):
                    continue  # the first test owns out-of-range citations
                prose = " ".join(lines[max(0, index - _PROSE_WINDOW + 1) : index + 1])
                named = {
                    (found[0] or found[1] or found[2]).rsplit(".", 1)[-1]
                    for found in _NAMED.findall(prose)
                }
                named &= spans.keys()
                if not named:
                    continue
                cited = body[start - 1 : end]
                agrees = any(
                    spans[name][0] <= end
                    and start <= spans[name][1]
                    # The call-site exception: pointing at where a
                    # function is USED is a citation about the caller,
                    # and the cited lines say the name themselves.
                    or any(name in text for text in cited)
                    for name in named
                )
                if not agrees:
                    lives = ", ".join(f"{n}={spans[n][0]}-{spans[n][1]}" for n in sorted(named))
                    drifted.append(
                        f"{source.relative_to(_ROOT)}:{index + 1} cites"
                        f" {match.group(0)}, but its prose names {lives}"
                    )

    assert not drifted, (
        "citations that name one thing and point at another — re-point them"
        " at the definition, or say in the prose what the cited lines"
        " actually are:\n" + "\n".join(drifted)
    )
