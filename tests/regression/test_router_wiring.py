"""Regression guard: a feature module that is never included does not exist (#160).

``docs/REMAINING_WORK.md`` §D exists because of one specific failure
mode: a feature whose i18n keys, models and handler code are all
present, but whose router is never included in the tree. Nothing about
that state looks broken. The strings are in the YAML and pass the
convergence guard, the module imports cleanly, its unit tests are green
— and the command is simply silent in production, because no
registration for it was ever added to :mod:`~telegram_invite_bot.routers.main_router`.

The find-all audit that produced §D was a one-off sweep by hand. This
suite is the standing version of it, and it holds two invariants:

* **Every module that builds an aiogram router is imported by
  main_router.** A module can only earn its place in the tree through
  that import, so a factory nobody imports is a feature nobody can
  reach. The aiohttp/FastAPI routers (the public site, the payment
  webhooks) are excluded by their return annotation rather than by
  name — they are mounted on the web application, not on the
  dispatcher, and a name-based exclusion list would need editing every
  time a page is added.
* **Every factory main_router imports is also included.** Importing is
  not wiring: an alias that is bound and then never passed to
  ``include_router`` is dead in exactly the way this guard is about,
  and ruff would not flag it because the alias *is* referenced — by the
  import statement itself.

Both are asserted against the source of the tree rather than a kept
list, for the usual reason: a hand-maintained inventory drifts on the
first commit that adds a router, which is the very event it exists to
catch.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

PACKAGE: Final = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"
MAIN_ROUTER: Final = PACKAGE / "routers" / "main_router.py"

#: The aiogram type every dispatcher-side factory returns. Modules that
#: return ``APIRouter`` belong to the web application and are out of
#: scope here.
AIOGRAM_ROUTER: Final = "Router"


def _module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _annotation_names(node: ast.expr | None) -> set[str]:
    """Every bare name in a return annotation (``Router``, ``Router | None``…)."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} if node else set()


def _builds_aiogram_router(source: str) -> bool:
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.FunctionDef)
            and node.name.startswith("build_")
            and AIOGRAM_ROUTER in _annotation_names(node.returns)
        ):
            return True
    return False


def _factory_modules() -> set[str]:
    return {
        _module_name(path)
        for path in PACKAGE.rglob("*.py")
        if _builds_aiogram_router(path.read_text(encoding="utf-8"))
    }


def _main_router_tree() -> ast.Module:
    return ast.parse(MAIN_ROUTER.read_text(encoding="utf-8"))


#: A floor under the discovery, so the suite cannot pass vacuously. Both
#: assertions below are "no orphans", which is trivially true when the
#: scan finds nothing — and the scan reads a return annotation, so a
#: refactor that renames or drops it would silently disarm the guard
#: rather than fail it. The tree has ~190 routers; anything near zero
#: means the discovery broke, not that the wiring got simpler.
MIN_EXPECTED_ROUTERS: Final = 150


def test_the_scan_actually_finds_the_router_modules() -> None:
    found = _factory_modules()
    assert len(found) >= MIN_EXPECTED_ROUTERS, (
        f"only {len(found)} module(s) look like aiogram router factories — the "
        "return-annotation scan has stopped matching, which disarms every other "
        "assertion in this file"
    )


def test_every_aiogram_router_module_is_imported_by_main_router() -> None:
    imported = {
        node.module.removeprefix("telegram_invite_bot.")
        for node in ast.walk(_main_router_tree())
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("telegram_invite_bot.")
    }
    orphans = _factory_modules() - imported - {_module_name(MAIN_ROUTER)}
    assert not orphans, (
        "these modules build an aiogram router that main_router never imports, so "
        f"the feature is unreachable in production: {sorted(orphans)}"
    )


def test_every_imported_factory_is_included_in_the_tree() -> None:
    tree = _main_router_tree()

    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith("telegram_invite_bot."):
            continue
        bound.update(
            alias.asname or alias.name
            for alias in node.names
            if (alias.asname or alias.name).startswith("build_")
        )

    # ``root.include_router(build_x_router(...))`` — the factory is the
    # callee of the call passed to ``include_router``, so walking every
    # Call node and collecting the names it mentions is enough, and it
    # survives the closure form used by /admin_routes.
    included: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
        ):
            included.update(
                n.id for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Name)
            )

    dangling = bound - included
    assert not dangling, (
        "main_router imports these router factories but never passes them to "
        f"include_router — bound, unused, and invisible to ruff: {sorted(dangling)}"
    )
