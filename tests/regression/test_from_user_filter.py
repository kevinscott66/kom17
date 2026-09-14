"""``require_from_user`` needs ``F.from_user`` on the registration.

:func:`telegram_invite_bot.utils.aiogram.require_from_user` asserts that
the sender is present. aiogram types ``Message.from_user`` as
``User | None`` — channel posts legitimately have no author — so the
assert is the only thing standing between a sender-less update and an
``AttributeError`` deeper in. What makes it *safe* is the registration:
``F.from_user`` refuses the update before the handler is entered.

Before #303/#304 that pairing was informal, and the comments claimed a
guarantee nobody had written. Ten handlers in ``marriage.py`` and
nineteen registrations across seven more modules opened with ``assert
message.from_user is not None  # guaranteed by F.chat.type filter +
group`` while no registration carried ``F.from_user`` at all. Nothing
broke — the Bot API fills a fake ``from`` for on-behalf-of-a-chat
messages in groups (see ``handlers/moderation.py:799-802``) and channel
posts arrive as ``channel_post`` updates, which ``router.message`` never
sees — but the justification was fiction, and the type system's own
answer to "can this be ``None``?" was still yes.

This test makes the pairing structural. It walks every handler module —
``handlers/*.py`` *and* the 111 modules under ``handlers/admin/``, which a
non-recursive ``glob`` silently skipped until #332 (``/give``, the
coin-minting command, was among them) —
marks a function "needs a sender" when it calls ``require_from_user`` on
``message`` (or still carries the old bare assert), propagates that
backwards to every function that forwards ``message`` to it — the
registrations mostly name a thin local wrapper, so a direct name match
would miss almost all of them — and then requires ``F.from_user`` on
each message registration that lands in the resulting set.

Propagation crosses module boundaries, because the chain usually does:
``_require_admin`` is defined once in ``moderation.py`` and imported
verbatim by ``clear.py``, ``modcfg.py``, ``wordfilter.py`` and five
others. It does *not* propagate by bare name — a dozen modules call
their entry wrapper ``_entry``, and merging those would demand filters
on handlers that never read a sender. Each name is resolved in its own
module first, then through that module's ``from
telegram_invite_bot.handlers.X import ...`` bindings.
"""

from __future__ import annotations

import ast
from pathlib import Path

HANDLERS = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot" / "handlers"

_OLD_ASSERT = "message.from_user is not None"


def _functions(tree: ast.Module) -> dict[str, list[ast.AST]]:
    found: dict[str, list[ast.AST]] = {}

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            found.setdefault(node.name, []).append(node)
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    Visitor().visit(tree)
    return found


def _key(path: Path) -> str:
    """Module key: the dotted suffix under ``handlers/`` plus ``.py``.

    ``handlers/moderation.py`` -> ``moderation.py``, ``handlers/admin/give.py``
    -> ``admin.give.py``. The dotted form is what :func:`_handler_imports`
    derives from ``from telegram_invite_bot.handlers.X import ...``, so the two
    sides meet without a second translation — and, unlike a bare filename, it
    stays unique once the walk descends into ``handlers/admin/``.
    """
    return ".".join(path.relative_to(HANDLERS).with_suffix("").parts) + ".py"


def _handler_imports(tree: ast.Module) -> dict[str, str]:
    """Local name -> sibling handler module it was imported from."""
    bindings: dict[str, str] = {}
    prefix = "telegram_invite_bot.handlers."
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(prefix):
            origin = node.module[len(prefix) :] + ".py"
            for alias in node.names:
                bindings[alias.asname or alias.name] = origin
    return bindings


def _demands_sender(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert) and ast.unparse(sub.test) == _OLD_ASSERT:
            return True
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "require_from_user"
            and [ast.unparse(arg) for arg in sub.args] == ["message"]
        ):
            return True
    return False


def _message_calls(node: ast.AST) -> set[str]:
    """Names this function calls while handing over ``message``."""
    names: set[str] = set()
    for sub in ast.walk(node):
        if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)):
            continue
        passes = [ast.unparse(a) for a in sub.args]
        passes += [ast.unparse(k.value) for k in sub.keywords]
        if "message" in passes:
            names.add(sub.func.id)
    return names


def test_message_registrations_pair_require_from_user_with_the_filter() -> None:
    trees = {
        _key(path): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(HANDLERS.rglob("*.py"))
    }
    funcs = {name: _functions(tree) for name, tree in trees.items()}
    imports = {name: _handler_imports(tree) for name, tree in trees.items()}

    def resolve(module: str, name: str) -> tuple[str, str] | None:
        if name in funcs[module]:
            return module, name
        origin = imports[module].get(name)
        if origin in funcs and name in funcs[origin]:
            return origin, name
        return None

    needs: set[tuple[str, str]] = {
        (module, name)
        for module, table in funcs.items()
        for name, nodes in table.items()
        if any(_demands_sender(n) for n in nodes)
    }
    assert needs, "no handler demands a sender — the scan is broken, not the code"

    while True:
        grown = set()
        for module, table in funcs.items():
            for name, nodes in table.items():
                if (module, name) in needs:
                    continue
                for node in nodes:
                    hits = {resolve(module, called) for called in _message_calls(node)}
                    if hits & needs:
                        grown.add((module, name))
                        break
        if not grown:
            break
        needs |= grown

    missing: list[str] = []
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "register" or not node.args:
                continue
            if not ast.unparse(node.func.value).endswith(".message"):
                continue
            handler = ast.unparse(node.args[0])
            if resolve(module, handler) not in needs:
                continue
            if any(ast.unparse(arg) == "F.from_user" for arg in node.args):
                continue
            missing.append(f"{module}:{node.lineno} {handler}")

    assert missing == [], (
        "these message registrations reach require_from_user without F.from_user:\n"
        + "\n".join(missing)
    )
