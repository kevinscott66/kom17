"""#2008: a model may not deny a writer this package actually ships.

#2007 fixed three docstrings in ``db/models/economy.py`` by hand. All
three said the donation tables had no writer — no writer *in the port*,
the reader was meant to infer — and all three had been wrong since RR-2
#14 gave them one. Nobody noticed for two reasons. The claim sits in the
one file a maintainer reads to learn what a table *is*, so it is trusted
by default; and it is the kind of sentence that only becomes false
somewhere else, in a repository or a service, by an edit that never
opens the model file at all.

That is the shape this guard is for. It is a tripwire, not a style
checker: it says nothing about how a docstring should be written, and it
never fires on a table with no writer. It fires only on the coincidence
that produced #2007 — this package writes the table, *and* the model's
docstring contains a phrase that reads as "it doesn't".

Both halves are deliberately crude. The writer scan is syntactic and
over-eager on purpose (see :func:`_writers`), because a writer it misses
is a tripwire that silently stops working, while one it invents costs a
sentence in :data:`_ACKNOWLEDGED`. The phrase list is short and dumb for
the opposite reason: a clever matcher would need to tell "``/achievements``
is read-only" (a card, true) from "read-only in Stage 19" (a table,
false), and no regular expression can. So it flags both and makes a
human decide — which is the whole point, since the four stale claims
this file found on its first run had each been read past for months.

:data:`_ACKNOWLEDGED` is where that decision is recorded. An entry is
not an exemption from being right; it is a note that someone checked
this pairing and the prose is genuinely about something narrower than
the table — a single column, a closed interval in the past, one repo's
surface. Adding one takes an edit and a reason. Adding one to silence a
failure instead of reading the docstring re-creates #2007 exactly.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "telegram_invite_bot"
MODELS = SRC / "db" / "models"

#: Call names that mean "this statement changes rows".
_WRITE_CALL = re.compile(r"(?:^|_)(?:insert|update|delete|add|add_all|merge|upsert)$")

#: Raw DML against a literal table name, inside any string constant.
_RAW_DML = re.compile(
    r"\b(?:INSERT\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)

#: Phrasings a reader takes as "nothing in this package writes here".
#:
#: ``append-only`` is NOT one of them and must never be added: it means
#: "inserts, no updates", which is a description of a live write path,
#: not the absence of one. Seven models say it truthfully.
_DENIALS = (
    re.compile(r"\bread[- ]only\b", re.IGNORECASE),
    re.compile(r"\bfrozen\b", re.IGNORECASE),
    re.compile(r"\bno writer\b", re.IGNORECASE),
    re.compile(r"\bnever ported\b", re.IGNORECASE),
    re.compile(r"\bnothing (?:writes|updates|touches|inserts)\b", re.IGNORECASE),
    re.compile(r"\b(?:stays|remains|lives) in legacy\b", re.IGNORECASE),
    re.compile(r"\bnever (?:writes|inserts|updates)\b", re.IGNORECASE),
    re.compile(r"\bno longer (?:written|maintained)\b", re.IGNORECASE),
    re.compile(r"\bwrite side\b", re.IGNORECASE),
    re.compile(r"\bstopped moving\b", re.IGNORECASE),
)

#: Model class → why its denial is narrower than the table it sits on.
#:
#: Checked by hand, one at a time. Both entries survived #2008's sweep;
#: the four that did not were rewritten instead.
_ACKNOWLEDGED = {
    "Donation": (
        "the sentence is past tense and bounded — 'between T-011 and"
        " #2007 the surface was read-only' — and exists precisely to"
        " explain the gap in ``created_at`` it left behind"
    ),
    "GroupDonationsAggregate": (
        "'frozen' is scoped to the single ``total_donations`` column and"
        " the paragraph above it lists the three live writers by name"
    ),
}

#: Tables the scan must keep seeing a writer for. Not an invariant about
#: the schema — a canary on :func:`_writers`. Every one of these is
#: written through a different mechanism (ORM ``update``, ``sqlite_insert``
#: upsert, a bare constructor handed to ``session.add``, raw DML in a
#: string constant), so a refactor that blinds the scan to any one style
#: fails here loudly instead of quietly disarming the guard.
_MUST_SEE_A_WRITER = frozenset(
    {"shop_items", "user_achievements", "check_claims", "user_group_joins"}
)


def _tablenames() -> dict[str, tuple[str, int, str]]:
    """Model class name → (table, line of the class, module file name)."""
    found: dict[str, tuple[str, int, str]] = {}
    for path in sorted(MODELS.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign):
                    named = isinstance(stmt.target, ast.Name) and stmt.target.id == "__tablename__"
                elif isinstance(stmt, ast.Assign):
                    named = any(
                        isinstance(t, ast.Name) and t.id == "__tablename__" for t in stmt.targets
                    )
                else:
                    continue
                value = stmt.value
                if not (named and isinstance(value, ast.Constant)):
                    continue
                if isinstance(value.value, str):
                    found[node.name] = (value.value, node.lineno, path.name)
    return found


def _writers(models: dict[str, tuple[str, int, str]]) -> dict[str, set[str]]:
    """Table → the modules under ``src/`` that appear to write it.

    Three signals, all syntactic and all over-eager:

    * a call whose name ends in ``insert``/``update``/``delete``/``add``/
      ``merge``/``upsert`` anywhere in whose subtree a model name appears
      — this catches ``update(ShopItem)``, ``sqlite_insert(X).on_conflict``
      and ``session.add_all(...)`` without knowing any of their signatures;
    * a bare ``Model(...)`` construction, because an ORM instance built
      outside the model layer exists to be persisted;
    * literal ``INSERT``/``UPDATE``/``DELETE`` naming a known table inside
      any string constant.

    Over-eager is the safe direction here. A false positive shows up as a
    failure a human reads; a false negative shows up as nothing at all,
    for as long as it takes someone to notice the guard stopped working.

    Dynamic DML — ``group_migration_service`` interpolates a table name
    into every statement it runs — is invisible to all three and stays
    that way. It renames a group id across whatever table it is handed;
    attributing it to a table statically is impossible, and calling it a
    writer would flag tables that have no domain write path at all.
    """
    by_table = {table for table, _, _ in models.values()}
    hits: dict[str, set[str]] = defaultdict(set)
    for path in sorted(SRC.rglob("*.py")):
        if path.is_relative_to(MODELS):
            continue
        module = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else ""
                )
                if name in models:
                    hits[models[name][0]].add(module)
                elif name and _WRITE_CALL.search(name):
                    for inner in ast.walk(node):
                        referenced = getattr(inner, "id", None) or getattr(inner, "attr", None)
                        if referenced in models:
                            hits[models[referenced][0]].add(module)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for match in _RAW_DML.finditer(node.value):
                    if match.group(1) in by_table:
                        hits[match.group(1)].add(module)
    return hits


def test_the_writer_scan_still_sees_the_writers_it_is_built_on() -> None:
    """Premise: the scan is not silently blind.

    Every check below rests on :func:`_writers` finding write paths. A
    guard that finds none passes forever and tells nobody, so the four
    write styles the package actually uses are pinned here by example.
    """
    models = _tablenames()
    hits = _writers(models)
    missing = sorted(_MUST_SEE_A_WRITER - hits.keys())
    assert not missing, (
        "the writer scan no longer sees a write path for "
        f"{missing} — these are pinned because each is written through a"
        " different mechanism, so this almost certainly means the scan"
        " went blind rather than that the writers were removed. Fix"
        " _writers() before trusting the test below; if a writer really"
        " was deleted, the model's docstring probably needs the opposite"
        " edit from the one #2008 was about."
    )


def test_no_model_denies_a_writer_this_package_ships() -> None:
    """A written table may not be documented as unwritten.

    The failure this catches is not cosmetic. ``UserPrivilege`` said the
    write side "stays in legacy until the shop / VIP flows port" — the
    shop ported, the grants moved into ``PrivilegesRepo``, and the
    sentence stayed. A maintainer reading it would have gone looking in
    ``bot.py`` for a call site that lives thirty lines from the one they
    were editing.
    """
    models = _tablenames()
    hits = _writers(models)
    guilty: list[str] = []
    for cls, (table, lineno, module) in sorted(models.items()):
        if table not in hits or cls in _ACKNOWLEDGED:
            continue
        source = (MODELS / module).read_text()
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.ClassDef) and node.name == cls):
                continue
            doc = " ".join((ast.get_docstring(node) or "").split())
            phrases = sorted({m.group(0) for p in _DENIALS for m in p.finditer(doc)})
            if phrases:
                writers = ", ".join(sorted(hits[table])[:3])
                guilty.append(
                    f"  db/models/{module}:{lineno} {cls} ({table})"
                    f" says {phrases} — written by {writers}"
                )
    assert not guilty, (
        "a model docstring denies a writer that ships in this package."
        " Either the sentence is stale and belongs in the past tense, or"
        " it is about something narrower than the table (one column, one"
        " repo's surface, a closed interval) — in which case say so in"
        " the docstring and record the reason in _ACKNOWLEDGED. Do not"
        " add an entry without reading the docstring first; that is how"
        " #2007 happened:\n" + "\n".join(guilty)
    )
