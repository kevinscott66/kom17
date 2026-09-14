"""Every handler asks aiogram only for names something actually provides.

aiogram resolves handler arguments *by name* out of the middleware data
dict. A required parameter nobody writes into that dict is not a type
error at import time and not a routing failure — the handler registers
fine, matches fine, and then raises ``TypeError`` inside the dispatcher
the first time a real user triggers it. All the user sees is the generic
error card, and the only trace is a ``tib_handler_errors`` tick.

That is exactly how #215 lived: four group welcome commands declared
``data: dict[str, Any]``, no middleware has ever written a key literally
called ``data``, and so ``/setwelcome`` and its three siblings were dead
in every group from the day they shipped. Unit tests called the inner
handlers directly and passed; only feeding a real update through a real
dispatcher could have caught it, and nothing did.

So this guard asks the assembled tree the question directly, and — as in
``test_router_wiring`` — derives *both* sides from the source rather than
from a kept list, because a hand-maintained inventory drifts on the first
commit that adds a middleware, which is the very event it exists to
catch:

* the demand side is every handler registered anywhere in the production
  router tree, introspected with :func:`inspect.signature`;
* the supply side is every key the source writes into a data dict
  (``data["x"] = …`` in a middleware) or returns from a filter (aiogram's
  other injection channel — ``return {"ai_question": …}``), harvested by
  AST walk over ``src/``.

The one kept list left is :data:`_AIOGRAM_PROVIDED`: names the framework
itself puts in ``data``. Those live in aiogram, not in this repo, so no
scan of our source can find them; they change only when aiogram does.

Known limitation, stated rather than papered over: this proves a name is
provided *somewhere*, not that it is provided on the branch that routes
to this particular handler. A repo-scoped middleware that only attaches
to the economy router still counts as supply for a handler on the
moderation router. Narrowing that would mean modelling per-router
middleware chains, which is a much larger claim than the failure being
guarded against needs — the bug class here is "nobody provides this at
all", and that is fully covered.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telegram_invite_bot import handlers as _handlers_pkg

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Dispatcher

_SRC = Path(_handlers_pkg.__file__).parent.parent

#: Names aiogram itself stamps into ``data`` before the handler runs, or
#: injects from a built-in filter. Not discoverable from this repo's
#: source, hence written out. ``command`` comes from ``Command``,
#: ``callback_data`` from ``CallbackData.filter()``; the rest are the
#: dispatcher's own context.
_AIOGRAM_PROVIDED: frozenset[str] = frozenset(
    {
        "bot",
        "callback_data",
        "command",
        "dispatcher",
        "event_chat",
        "event_context",
        "event_from_user",
        "event_router",
        "event_update",
        "fsm_storage",
        "handler",
        "raw_state",
        "state",
    }
)


def _provided_by_source() -> set[str]:
    """Every key this repo writes into an aiogram data dict.

    Two channels, because aiogram has two: a middleware assigning into
    ``data``, and a filter returning a dict (whose keys are merged into
    ``data`` for the handler it let through).

    Keys spelled as a module-level constant are resolved through that
    module's own constants rather than skipped. ``middlewares/base.py``
    writes ``data[CHECKPOINT_KEY]``, not ``data["checkpoint"]``, and a
    literal-only scan therefore called ``checkpoint`` unprovided — which
    would have failed the first handler to require it without a default.
    A guard whose false negatives are invisible is bad; one whose false
    *positives* are a red suite on unrelated work gets deleted.
    """
    names: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_string_consts(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    key = _data_subscript_key(target, consts)
                    if key is not None:
                        names.add(key)
            elif isinstance(node, ast.Call):
                key = _data_setdefault_key(node, consts)
                if key is not None:
                    names.add(key)
            elif isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                names.update(
                    k.value
                    for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                )
    return names


def _module_string_consts(tree: ast.Module) -> dict[str, str]:
    """``NAME = "value"`` at module level, annotated or not.

    Deliberately module level only. A name assigned inside a function
    can be rebound between the assignment and the subscript, and a scan
    that pretended otherwise would report supply that may not exist —
    the one failure mode worse than reporting none.
    """
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            targets: list[ast.expr] = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                consts[target.id] = value.value
    return consts


def _string_key(node: ast.expr | None, consts: dict[str, str]) -> str | None:
    """A literal string, or a module constant holding one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _data_subscript_key(target: ast.expr, consts: dict[str, str]) -> str | None:
    """``data["lang"]`` as an assignment target → ``"lang"``."""
    if not isinstance(target, ast.Subscript):
        return None
    if not (isinstance(target.value, ast.Name) and target.value.id == "data"):
        return None
    return _string_key(target.slice, consts)


def _data_setdefault_key(node: ast.Call, consts: dict[str, str]) -> str | None:
    """``data.setdefault("lang", …)`` → ``"lang"``."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "setdefault"):
        return None
    if not (isinstance(func.value, ast.Name) and func.value.id == "data"):
        return None
    return _string_key(node.args[0] if node.args else None, consts)


def _required_injections(dispatcher: Dispatcher) -> dict[str, set[str]]:
    """``{parameter name: {owning handler, …}}`` over the whole tree.

    The *first* parameter is skipped unconditionally: aiogram passes the
    event positionally, so its name is free (``message``, ``event``,
    ``query`` — all fine). Everything after it is resolved by name.
    Parameters with defaults are skipped too — a default is precisely
    the way to say "inject this if you have it".
    """
    demand: dict[str, set[str]] = {}

    def _walk(router: object) -> None:
        for name, observer in getattr(router, "observers", {}).items():
            if name == "update":
                # The dispatcher's own root observer — aiogram's code,
                # not ours, and it is what calls everything below.
                continue
            for handler in getattr(observer, "handlers", []):
                _collect(handler.callback, demand)
        for sub in getattr(router, "sub_routers", []):
            _walk(sub)

    _walk(dispatcher)
    return demand


def _collect(fn: Callable[..., Any], demand: dict[str, set[str]]) -> None:
    if not getattr(fn, "__module__", "").startswith("telegram_invite_bot"):
        # Third-party callbacks bring their own contract with them.
        return
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover — builtins, C callables
        return
    owner = f"{fn.__module__}.{getattr(fn, '__qualname__', fn)}"
    for param in params[1:]:
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.default is not param.empty:
            continue
        demand.setdefault(param.name, set()).add(owner)


def test_every_required_handler_argument_is_provided(
    production_dispatcher: Dispatcher,
) -> None:
    """#215 regression, tree-wide.

    The failure message names the offending handlers, because "some
    handler wants an argument nobody supplies" is useless without the
    handler — and the whole point of this guard is that the runtime
    failure is a generic error card that names nothing.
    """
    supplied = _AIOGRAM_PROVIDED | _provided_by_source()
    demand = _required_injections(production_dispatcher)

    unsatisfiable = {
        name: sorted(owners) for name, owners in demand.items() if name not in supplied
    }
    assert not unsatisfiable, (
        "handlers require aiogram to inject names nothing provides — each of "
        "these raises TypeError on its first real call:\n"
        + "\n".join(
            f"  {name}: {', '.join(owners)}" for name, owners in sorted(unsatisfiable.items())
        )
    )


def test_the_guard_can_see_the_shape_of_the_bug_it_guards() -> None:
    """A meta-check, because the assertion above passes just as happily
    when the walk finds nothing at all — a silent regression in
    :func:`_required_injections` (a renamed aiogram attribute, say) would
    turn this file into a no-op that still reports green.

    So: the supply scan must find the keys we know our middlewares write,
    and the aiogram list must not have quietly absorbed them.
    """
    supplied = _provided_by_source()
    # ``lang`` is stamped by the language middleware and injected into
    # more handlers than anything else in the tree; ``economy_repo`` is
    # the busiest of the scoped ones.
    assert {"lang", "economy_repo", "user_service"} <= supplied
    # ``data`` is the name #215 asked for. Nothing provides it, and the
    # day something does, this guard stops being able to catch #215.
    assert "data" not in supplied | _AIOGRAM_PROVIDED
    # ``checkpoint`` is written as ``data[CHECKPOINT_KEY]``. It is the
    # one key in the tree spelled through a constant, so it is also the
    # only proof that the constant resolution above still works — drop
    # that code and this line goes red instead of the guard silently
    # growing a false positive for every future checkpoint handler.
    assert "checkpoint" in supplied
