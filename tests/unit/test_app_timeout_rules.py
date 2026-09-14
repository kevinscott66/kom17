"""GAP-1: every stateful FSM flow must register a sweeper timeout rule.

Withdraw and support set FSM state (and stamp ``state_entered_at``) but
had no ``TimeoutRule``, so an abandoned interview lingered forever — the
withdraw busy-gate then refused re-entry until the user found /cancel.
This pins the registration so the gap can't silently reopen.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any

from telegram_invite_bot.app import _app_timeout_rules
from telegram_invite_bot.fsm.duel import DuelStates
from telegram_invite_bot.fsm.rps import RpsStates
from telegram_invite_bot.fsm.support import SupportStates
from telegram_invite_bot.fsm.withdraw import WithdrawStates


def test_all_stateful_flows_have_a_timeout_rule() -> None:
    rules = _app_timeout_rules()
    expected = {
        RpsStates.awaiting_acceptance,
        RpsStates.awaiting_moves,
        DuelStates.awaiting_acceptance,
        DuelStates.awaiting_rolls,
        WithdrawStates.awaiting_amount,
        WithdrawStates.awaiting_confirm,
        SupportStates.awaiting_text,
    }
    missing = expected - set(rules)
    assert not missing, f"FSM states without a sweeper timeout rule: {missing}"


def _on_expire_ast(fn: Any) -> ast.AsyncFunctionDef | ast.FunctionDef:
    """The parsed definition of a timeout callback.

    #280: ``callable(...)`` is true of ``async def _(*a): pass``, so it
    proves nothing about a callback whose job is to notify a player or
    hand an escrow back. Reading the definition is the cheapest thing
    that can tell an empty stub from a real one.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    func = tree.body[0]
    assert isinstance(func, ast.AsyncFunctionDef | ast.FunctionDef), f"{fn!r} is not a def"
    return func


def test_every_rule_has_positive_timeout_and_callback() -> None:
    """A rule is only as good as the callback it fires.

    #280: this test used to assert ``timeout_seconds > 0`` and
    ``callable(on_expire)`` — both of which a do-nothing stub satisfies.
    Every one of these callbacks exists to reach the outside world (tell
    the players the match died, put an escrowed stake back), so it must
    contain at least one ``await``. That is a floor, not a proof of
    correctness, but it is a floor an empty stub cannot clear.
    """
    checked = 0
    for state, rule in _app_timeout_rules().items():
        assert rule.timeout_seconds > 0, f"{state}: non-positive timeout"
        assert callable(rule.on_expire), f"{state}: on_expire not callable"

        func = _on_expire_ast(rule.on_expire)
        body = func.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]  # the docstring is not an implementation
        assert body, f"{state}: {func.name}() is a docstring and nothing else"
        assert not all(isinstance(node, ast.Pass) for node in body), (
            f"{state}: {func.name}() does nothing"
        )
        assert any(isinstance(node, ast.Await) for node in ast.walk(func)), (
            f"{state}: {func.name}() awaits nothing, so it can neither "
            f"notify anyone nor return an escrow"
        )
        checked += 1

    # Guard the guard: a loop over an empty dict asserts nothing.
    assert checked >= 15, f"only {checked} rules inspected"
