"""#1998: a cross-reference into this package must name something real.

A ``:class:`` / ``:func:`` / ``:meth:`` / ``:data:`` / ``:mod:`` role is
not decoration. It is an assertion that the name is a live, linkable
symbol — a reader is being told "go and read this". When the symbol has
been deleted, the reader makes a wasted trip and, worse, is left
believing a convention exists that does not.

The audit found three, all from the same shape of change: a cutover
deleted the thing it replaced and left the references behind.
``services/roulette_service.RouletteLimiter`` and its ``AbuseCheck``
were removed by the L-25 games-limiter cutover, yet four sites still
pointed at them. Three of those were plain history ("previously
enforced via…") and only needed the role dropped. The fourth,
``handlers/rp.py``, was a live claim in the present tense: it said
``RpRateLimiter`` mirrored the roulette limiter's injectable-clock
posture. By then that posture existed nowhere — the model was deleted
and its replacement, :class:`GameLimitService`, deliberately does the
opposite (the caller passes ``now``, so no clock is injected at all).
A reader following the house style would have been misled both ways.
``cms/guide_site/markdown.py`` named ``legal.documents.Document``; the
class is ``LegalDoc``.

**Scope: two kinds that can be resolved without guessing.** A bare
``:class:`Message`` may legitimately mean aiogram's, and ``:mod:`os``
the stdlib's, so most unqualified references cannot be checked here at
all. Two kinds can:

1. anything prefixed with ``telegram_invite_bot.`` — a claim the
   package makes about itself;
2. anything naming a PRIVATE symbol (``_foo``) — a leading underscore
   cannot have come from another package, so the name must be bound in
   the module that makes the reference or it is bound nowhere.

The second tier found four more of the same shape, and one of a worse
shape. ``handlers/shop.py`` pointed at ``_SHOP_BUTTON_CAP`` for a
claim that the keyboard truncates at a fixed row count while the body
lists the whole catalog. That constant does not exist, and neither
does the behaviour: the catalog paginates on ``_PAGE_SIZE``, whose own
comment names the truncate-and-list arrangement as the one Stage 25
deliberately left behind. The module docstring — the first thing a
reader of that file sees — was describing the state of the world its
own file repudiates 140 lines lower down. A dangling reference is
worth chasing precisely because it is so often attached to a claim
that stopped being true at the same moment the name did.

See also :mod:`tests.regression.test_prose_names_real_things` (#1997)
and :mod:`tests.regression.test_documented_env_knobs` (#1995), which
hold prose to the same standard for database files and env knobs.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"
PACKAGE = SRC.name

#: ``:role:`target``` with the optional ``~`` abbreviation marker.
#: ``re.S`` because a long target is wrapped across docstring lines.
_XREF = re.compile(r":(class|meth|func|data|attr|mod|exc):`~?([^`]+)`", re.S)

#: A single private name: ``_foo``, not ``_foo.bar`` and not ``__foo``.
#: Dunders are excluded because ``:meth:`__call__`` is a reference to a
#: protocol rather than to a symbol defined anywhere in particular.
_PRIVATE = re.compile(r"^_[a-z0-9][A-Za-z0-9_]*$", re.I)


def _modules() -> dict[str, Path]:
    """Every importable module in the package → the file defining it."""
    found: dict[str, Path] = {}
    for path in sorted(SRC.rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        found[".".join([PACKAGE, *parts]).rstrip(".")] = path
    return found


def _module_level_names(path: Path) -> set[str]:
    """Names a module binds at top level, imports included.

    Imports count: re-exporting a name makes ``module.Name`` a real,
    resolvable target even though the ``def`` lives elsewhere.
    """
    names: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def _class_members(path: Path, class_name: str) -> set[str]:
    """Attributes and methods declared directly on ``class_name``."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if isinstance(item, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(item.name)
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                names.add(item.target.id)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def _resolve(role: str, target: str, modules: dict[str, Path]) -> str | None:
    """``None`` if the reference resolves, else why it does not."""
    if role == "mod":
        return None if target in modules else "no such module"
    parts = target.split(".")
    # Try ``pkg.mod.Name`` first, then ``pkg.mod.Class.member``.
    for tail_len in (1, 2):
        if tail_len >= len(parts):
            break
        module = ".".join(parts[:-tail_len])
        path = modules.get(module)
        if path is None:
            continue
        tail = parts[-tail_len:]
        if tail[0] not in _module_level_names(path):
            return f"{module} defines no {tail[0]!r}"
        if tail_len == 2 and tail[1] not in _class_members(path, tail[0]):
            return f"{module}.{tail[0]} has no member {tail[1]!r}"
        return None
    return "no real module prefix"


def _prose(path: Path) -> list[tuple[int, str]]:
    """Comments and string constants in ``path``, with line numbers."""
    source = path.read_text()
    items = [
        (token.start[0], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            items.append((node.lineno, node.value))
    return items


def _bound_anywhere(path: Path) -> set[str]:
    """Every name bound anywhere in the module, at any nesting depth.

    Deliberately generous — nested defs, walrus targets, comprehension
    variables and attribute names all count. A private reference is
    interesting only when the name is bound NOWHERE in its own file, so
    over-collecting here costs nothing and keeps the guard free of the
    false positives that would make it get switched off.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[-1])
    return names


def _collect_private() -> tuple[list[str], list[str]]:
    """``(resolved, broken)`` for private references, per module."""
    resolved: list[str] = []
    broken: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        bound: set[str] | None = None
        for lineno, text in _prose(path):
            for role, raw in _XREF.findall(text):
                target = re.sub(r"\s+", "", raw)
                if not _PRIVATE.match(target):
                    continue
                if bound is None:
                    bound = _bound_anywhere(path)
                where = f"{path.relative_to(SRC)}:{lineno}"
                if target in bound:
                    resolved.append(f":{role}:`{target}`")
                else:
                    broken.append(f"  {where}  :{role}:`{target}`")
    return resolved, broken


def _collect() -> tuple[list[str], list[str]]:
    """``(resolved, broken)`` — broken entries carry file, line and reason."""
    modules = _modules()
    resolved: list[str] = []
    broken: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, text in _prose(path):
            for role, raw in _XREF.findall(text):
                # A wrapped target carries the docstring's indentation.
                target = re.sub(r"\s+", "", raw)
                if not target.startswith(f"{PACKAGE}."):
                    continue
                reason = _resolve(role, target, modules)
                where = f"{path.relative_to(SRC)}:{lineno}"
                if reason is None:
                    resolved.append(f":{role}:`{target}`")
                else:
                    broken.append(f"  {where}  :{role}:`{target}`  [{reason}]")
    return resolved, broken


def test_every_qualified_cross_reference_names_a_real_symbol() -> None:
    _, broken = _collect()
    assert not broken, (
        "a cross-reference points at a symbol this package does not"
        " define. A reader is being told to go and read it, so either"
        " fix the name or, if the symbol was deleted and the sentence is"
        " history, drop the role and leave a plain ``literal`` — history"
        " is worth keeping, a dead link is not:\n" + "\n".join(sorted(broken))
    )


def test_the_scan_actually_finds_cross_references() -> None:
    """Guard the guard.

    Every assertion above is vacuously true if the regex stops matching
    or the package moves, and a silently empty scan is the failure mode
    a prose guard is most prone to.
    """
    resolved, _ = _collect()
    assert len(resolved) >= 20, (
        f"only {len(resolved)} qualified cross-references found — the scan broke, not the prose"
    )


def test_the_resolver_rejects_a_name_that_does_not_exist() -> None:
    """The other half: prove the resolver can say no.

    Both spellings are checked because the two failures found in the
    audit were of exactly these kinds — a module that no longer exists
    and a symbol missing from a module that does.
    """
    modules = _modules()
    assert _resolve("mod", f"{PACKAGE}.no_such_module", modules) is not None
    assert _resolve("class", f"{PACKAGE}.config.settings.NoSuchConfig", modules) is not None
    # …and can still say yes, so the rejection is not blanket.
    assert _resolve("mod", f"{PACKAGE}.config.settings", modules) is None


def test_every_private_cross_reference_is_bound_in_its_own_module() -> None:
    """A ``_name`` reference must resolve inside the file that makes it.

    Nothing outside a module can supply a private name, so this needs
    no import graph and admits no ambiguity: either the file binds it
    or the reference is dead.
    """
    _, broken = _collect_private()
    assert not broken, (
        "a reference names a private symbol its own module does not"
        " bind. These are usually a helper that was inlined or renamed"
        " and, because the surrounding sentence was written to explain"
        " that helper, the sentence is usually wrong too — read it"
        " before renaming the reference:\n" + "\n".join(sorted(broken))
    )


def test_the_private_scan_actually_finds_references() -> None:
    """Guard the guard, second tier."""
    resolved, _ = _collect_private()
    assert len(resolved) >= 20, (
        f"only {len(resolved)} private cross-references found — the scan broke"
    )
