"""Every registered ``on_expire`` is a notice — nothing more.

Two comments in
:class:`~telegram_invite_bot.scheduler.fsm_sweeper.FsmTimeoutSweeper`
rest on this and say so out loud.

#837, at the post-callback re-read: comparing the state after the
callback with the state before it "is sound because no registered
callback touches FSM storage: not one of the registered ``on_expire``
functions calls ``set_state`` / ``set_data`` / ``update_data``, so
anything that moved here was a handler, not us."

#1515, on the ``TimeoutRule`` docstring: the clear that follows the
callback is a storage write that can fail, and when it does the next
pass calls the callback a second time. "Every registered callback today
only sends a message, so the visible cost is a duplicate notice; the
first one that moves coins in this shape would pay out twice, and this
sentence is what stands between the two."

A sentence is a thin thing to stand between a duplicate DM and a double
payout, so this is that sentence, mechanised. It reads the live
registry — ``app._app_timeout_rules()`` — so a rule added tomorrow is
covered the day it is wired, not the day someone remembers this file.

The check is structural, like
``tests/regression/test_fsm_timeout_stamps.py``: it sees what the
callback function itself calls, not what a helper it delegates to
calls. That limit is stated rather than papered over — if a callback
ever moves its effect one frame down, the fix is to follow the call,
not to trust the green.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from telegram_invite_bot.app import _app_timeout_rules

pytestmark = pytest.mark.integration

# The three writes #837 names. A callback that mutated FSM storage would
# read as a race against itself: the re-read would see its own change,
# declare the key raced and stop clearing it, so the state would expire
# again on every pass, forever.
_FSM_WRITES = ("set_state", "set_data", "update_data")

# Every verb that moves coins, taken from the two layers that own them
# (``services/economy_service.py`` and the repositories underneath).
# ``release`` is here for the escrow release, not for a lock — the
# callbacks serialise with ``async with``, never with a bare release.
_MONEY_VERBS = (
    "credit",
    "debit",
    "hold",
    "release",
    "release_all",
    "release_processing",
    "refund_one",
    "settle_hold",
    "set_balance",
    "transfer",
)


def _called_names(func: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    """Every name this function calls, whether bare or on an object."""
    names: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def _registered_callbacks() -> list[tuple[str, ast.AsyncFunctionDef | ast.FunctionDef]]:
    """``(label, ast node)`` for each callback the app actually wires."""
    found: list[tuple[str, ast.AsyncFunctionDef | ast.FunctionDef]] = []
    seen: set[tuple[str, str]] = set()
    for rule in _app_timeout_rules().values():
        callback = rule.on_expire
        module = inspect.getmodule(callback)
        assert module is not None, callback
        assert module.__file__ is not None, module
        ident = (module.__name__, callback.__name__)
        if ident in seen:
            continue
        seen.add(ident)
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        node = next(
            (
                item
                for item in ast.walk(tree)
                if isinstance(item, ast.AsyncFunctionDef | ast.FunctionDef)
                and item.name == callback.__name__
            ),
            None,
        )
        assert node is not None, f"{ident} is registered but not found in its own module"
        found.append((f"{module.__name__}.{callback.__name__}", node))
    return found


def test_the_registry_is_not_empty() -> None:
    """Guard the guard: an empty registry would pass everything below."""
    assert len(_registered_callbacks()) >= 10


@pytest.mark.parametrize("forbidden", [_FSM_WRITES, _MONEY_VERBS], ids=["fsm", "money"])
def test_no_expire_callback_does_more_than_notify(forbidden: tuple[str, ...]) -> None:
    offenders = [
        f"{label} calls {sorted(_called_names(node) & set(forbidden))}"
        for label, node in _registered_callbacks()
        if _called_names(node) & set(forbidden)
    ]
    assert not offenders, (
        "an on_expire callback stopped being a pure notice: "
        + "; ".join(offenders)
        + ". The sweeper can call it twice (fsm_sweeper.TimeoutRule, #1515) and "
        "compares FSM state across it (#837), so either read is now wrong. "
        "Fix the callback, or — if the effect is genuinely safe — change both "
        "comments in fsm_sweeper.py first and this list second."
    )
