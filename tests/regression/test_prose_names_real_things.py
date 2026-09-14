"""#1997: four claims the package makes about itself, checked.

Both guards exist because the same audit found the same failure twice:
a comment naming something that does not exist, in a place where the
wrong name also weakened the argument the comment was making. Prose
like that is worse than no prose — a reader who checks it wastes the
trip, and a reader who trusts it is misinformed.

**Phantom database files.** ``handlers/p2p_trade.py`` explained a
checkpoint by saying the write opens ``BEGIN IMMEDIATE`` on ``p2p.db``.
There is no ``p2p.db``: every P2P table subclasses ``EconomyBase``, so
the contended file is ``economy.db``, which is the busiest file in the
system rather than a quiet P2P-only one. The wrong name did not just
mislead — it understated the reason the checkpoint is there at all.

**Incomplete prefix tables.** ``keyboards/builders/p2p.py`` opens with
"EVERY P2P callback wire format lives here" and a table introduced as
"all distinct from every other prefix in the codebase", and the table
was missing ``p2p_buymenu``. A table that promises completeness has to
be complete or stop promising, so this pins the promise.

**Phantom lock registries.** ``webhook/server.py``'s single-worker
tripwire justified itself with a list of process-local state that a
fork would break, and one entry — "the marriage locks with the same
shape" — named something that has never existed: the marriage handlers
hold no lock at all. Here the wrong name did real damage, because the
list *is* the argument for the tripwire, and a reader who checked the
one entry they recognised and found nothing had no reason to trust the
rest. The list is now spelled as importable names and this pins them.

**Phantom migrations.** Twenty-odd model docstrings explain a column
by naming the revision that added it — ``0006_rp_18_gate``,
``0012_marriages_in_top_backfill``. That spelling is a promise that the
file is there to read, and it is the only trail from a column back to
the reasoning for it: an Alembic revision id appears nowhere in the
schema, so a reader who cannot find the revision has no second route.
Nothing was phantom when this was added; the guard exists because the
class of rot is the same one the three above are, and here it is
checkable with no judgement at all.

See also :mod:`tests.regression.test_documented_env_knobs` (#1995),
which does the same job for environment variables named in prose.
"""

from __future__ import annotations

import ast
import importlib
import io
import re
import tokenize
from collections import defaultdict
from pathlib import Path

from telegram_invite_bot.config.settings import FsmStorageConfig
from telegram_invite_bot.db.names import DBName

SRC = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"
PACKAGE = SRC.name
MIGRATIONS = SRC.parents[1] / "migrations" / "versions"

#: ``<name>.db`` anywhere in a comment or a string constant.
_DB_TOKEN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.db\b")

#: An Alembic revision id as it is written in prose: four digits, an
#: underscore, then the slug. Deliberately not anchored on backticks —
#: the corpus writes these both ways — and tight enough that nothing
#: else in 45k lines of comments matches it. Dates are written
#: ``2026-09-11`` here, so they do not collide.
_REVISION_TOKEN = re.compile(r"\b(\d{4}_[a-z0-9_]+)\b")

#: A ``telegram_invite_bot.a.b.name`` citation inside double backticks.
#: Anchored on the package name so ordinary prose cannot match, and on
#: the backticks so it only picks up spellings that claim to *be* code.
_DOTTED_CITATION = re.compile(r"``(telegram_invite_bot(?:\.[A-Za-z_][A-Za-z0-9_]*){2,})``")


def _real_db_stems() -> frozenset[str]:
    """Every SQLite file this project actually has.

    The five registry engines come from :class:`DBName`. ``fsm.db`` is
    the sixth and is deliberately NOT in the registry — it holds
    ephemeral flow state rather than a system of record, which is what
    makes ``rm database/fsm.db`` a safe recovery action
    (``config/settings.py``, :class:`FsmStorageConfig`). Its name is
    read off the field default rather than typed here, so renaming the
    default keeps this guard honest instead of breaking it.
    """
    fsm_default = FsmStorageConfig.model_fields["sqlite_path"].default
    return frozenset({db.value for db in DBName} | {Path(fsm_default).stem})


def _prose_of(path: Path) -> list[tuple[int, str]]:
    """Every comment and string constant in ``path``, with line numbers."""
    source = path.read_text()
    prose = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    prose += [
        (node.lineno, node.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    return prose


def test_no_comment_names_a_database_file_that_does_not_exist() -> None:
    real = _real_db_stems()
    phantoms: dict[str, set[str]] = defaultdict(set)
    for path in sorted(SRC.rglob("*.py")):
        for lineno, text in _prose_of(path):
            for stem in _DB_TOKEN.findall(text):
                # ``telegram_invite_bot.db`` is the DB *package*, not a
                # file — the same spelling, a different kind of thing.
                if stem in real or stem == PACKAGE:
                    continue
                phantoms[f"{stem}.db"].add(f"{path.relative_to(SRC)}:{lineno}")
    assert not phantoms, (
        "a comment names a database file this project does not have. The"
        f" real ones are {sorted(real)}; check whether the claim around it"
        " is still true once the name is corrected — a wrong file name"
        " usually means a wrong contention argument too:\n"
        + "\n".join(f"  {name} at {sorted(where)}" for name, where in sorted(phantoms.items()))
    )


def test_the_single_worker_tripwire_names_state_that_exists() -> None:
    """Every registry ``_assert_single_worker`` cites must resolve.

    The docstring's list is the whole argument for the tripwire, so it
    has to be checkable. Each citation is split at the last dot and
    looked up as ``getattr(import(module), attr)`` — the same trip a
    sceptical reader would make, done once per suite run instead.

    Scoped to this one docstring on purpose. A repo-wide version would
    have to decide what every dotted name in 45k lines of prose means,
    and would start failing on citations of things that legitimately
    no longer exist (legacy ``bot.py`` symbols, historical migrations).
    Here the contract is narrow and absolute: this list describes the
    process *as it is now*, or the tripwire is lying.
    """
    from telegram_invite_bot.webhook.server import _assert_single_worker  # noqa: PLC0415

    doc = _assert_single_worker.__doc__
    assert doc is not None, "the tripwire's justification was deleted, not the list"
    cited = _DOTTED_CITATION.findall(doc)
    assert len(cited) >= 8, (
        "the list of process-local state shrank; if a registry really went"
        f" away, say so in the docstring rather than dropping it: {cited}"
    )

    missing: list[str] = []
    for name in cited:
        module_path, _, attr = name.rpartition(".")
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            missing.append(f"{name} (no module {module_path})")
            continue
        if not hasattr(module, attr):
            missing.append(f"{name} (module has no {attr})")
    assert not missing, (
        "the single-worker tripwire justifies itself with state that does"
        " not exist. This is the exact failure the list was rewritten to"
        " end — a reader who checks one entry and finds nothing stops"
        " trusting the rest:\n  " + "\n  ".join(missing)
    )


def test_the_p2p_prefix_table_lists_every_prefix_the_module_defines() -> None:
    """The module says EVERY P2P wire format lives there. Hold it to it."""
    module = SRC / "keyboards" / "builders" / "p2p.py"
    tree = ast.parse(module.read_text())
    defined: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for keyword in node.keywords:
            if keyword.arg != "prefix" or not isinstance(keyword.value, ast.Constant):
                continue
            if isinstance(keyword.value.value, str):
                defined[keyword.value.value] = node.name
    docstring = ast.get_docstring(tree) or ""
    documented = set(re.findall(r"``(p2p_[a-z_]+)``\s{2,}", docstring))

    missing = {prefix: cls for prefix, cls in defined.items() if prefix not in documented}
    assert not missing, (
        "the prefix table promises to list every P2P callback prefix and"
        " does not. Add a row for each of:\n"
        + "\n".join(f"  ``{prefix}`` ({cls})" for prefix, cls in sorted(missing.items()))
    )

    imaginary = documented - set(defined)
    assert not imaginary, (
        f"the prefix table has rows for prefixes nothing defines: {sorted(imaginary)}"
    )


def test_no_two_callback_factories_share_a_prefix() -> None:
    """The other half of the same claim: "all distinct from every other
    prefix in the codebase".

    Two factories sharing a prefix would make one of them unroutable —
    aiogram matches on ``prefix + sep``, so identical prefixes are
    genuinely ambiguous while ``gadm`` and ``gadms`` are not (``gadm:``
    never matches ``gadms:``). Only exact collisions are checked, and
    the scan covers the whole package rather than the P2P module,
    because that is the scope the claim uses.
    """
    owners: dict[str, list[str]] = defaultdict(list)
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ClassDef):
                continue
            for keyword in node.keywords:
                if keyword.arg != "prefix" or not isinstance(keyword.value, ast.Constant):
                    continue
                prefix = keyword.value.value
                if isinstance(prefix, str):
                    owners[prefix].append(f"{path.relative_to(SRC)}:{node.lineno} {node.name}")
    assert owners, "no CallbackData factories found — the scan broke, not the code"
    collisions = {prefix: sites for prefix, sites in owners.items() if len(sites) > 1}
    assert not collisions, (
        "two CallbackData factories share a prefix; one of them is"
        " unroutable:\n"
        + "\n".join(f"  {prefix!r}: {sites}" for prefix, sites in sorted(collisions.items()))
    )


def _real_revision_ids() -> frozenset[str]:
    """Every id an ``alembic upgrade`` could actually land on.

    Read off the ``revision`` assignment rather than the filename: the
    two agree by convention throughout this tree, and the convention is
    exactly the kind of thing a guard must not assume — it is the
    assignment that Alembic reads, so it is the assignment that decides
    whether a cited id is real.
    """
    assignment = re.compile(r'^revision:\s*str\s*=\s*"([^"]+)"', re.MULTILINE)
    ids: set[str] = set()
    for path in MIGRATIONS.rglob("*.py"):
        match = assignment.search(path.read_text())
        if match is not None:
            ids.add(match.group(1))
    return frozenset(ids)


def test_no_comment_names_a_migration_that_does_not_exist() -> None:
    """A cited revision id must be a file somebody can open.

    The five databases each carry their own Alembic lineage, so an id
    is the only handle prose has on a schema change — the column it
    created says nothing about where it came from. When the id is
    wrong, the reader's single route to the reasoning is gone, and
    unlike a wrong line number there is no nearby text to recover from:
    the name resolves or it does not.

    The escape hatch, if a revision really is removed one day, is to
    rewrite the sentence so it does not promise a file — say what the
    change did, not which vanished revision did it. Leaving a dead id
    in place is the one thing this refuses.
    """
    real = _real_revision_ids()
    assert real, "no migrations found — the guard would pass vacuously"

    phantoms: dict[str, set[str]] = defaultdict(set)
    for path in sorted(SRC.rglob("*.py")):
        for lineno, text in _prose_of(path):
            for cited in _REVISION_TOKEN.findall(text):
                if cited not in real:
                    phantoms[cited].add(f"{path.relative_to(SRC)}:{lineno}")

    assert not phantoms, (
        "prose names an Alembic revision that no migration defines. If the"
        " revision was renamed, re-point the comment; if it was removed,"
        " rewrite the sentence to describe the change rather than cite a"
        " file that is not there:\n"
        + "\n".join(f"  {name} at {sorted(where)}" for name, where in sorted(phantoms.items()))
    )
