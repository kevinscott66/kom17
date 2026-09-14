"""Every sweeper-covered FSM state must be stamped when it is entered.

:class:`~telegram_invite_bot.scheduler.fsm_sweeper.FsmTimeoutSweeper`
does not track *when* a state was set — aiogram's storage keeps no such
metadata. It reads the deadline out of the FSM data dict, from the
``state_entered_at`` field the entering handler is expected to write
(``fsm_sweeper.STATE_ENTERED_AT_FIELD`` / ``utc_now_iso``).

A handler that sets a covered state without the stamp lands in
``sweep_once``'s defensive branch: log a WARNING, skip the key, move on.
Forever. The flow's busy-gate never clears, the FSM key is (chat, user)
so every *other* gated flow for that user is blocked too, and the only
outward sign is one WARNING per sweep interval in the journal.

That is exactly how ``/transfer_rights`` shipped: the rule was
registered in ``app._transfer_rights_timeout_rules`` with a working
``on_expire`` callback and a documented rationale, and it never once
fired, because ``handle_transfer_pick`` set the state with a bare
``set_data``.

The check is structural — for each function that enters a covered
state, the same function must reference the stamp. It cannot see a
handler that delegates stamping to a helper; if one ever does, extend
:data:`_STAMP_TOKENS` rather than exempting the handler, so the
invariant keeps meaning "the stamp is written on this path".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from telegram_invite_bot.app import _app_timeout_rules

pytestmark = pytest.mark.integration

_SRC = Path(__file__).resolve().parents[2] / "src/telegram_invite_bot"

# Names that, appearing anywhere in the entering function, mean "this
# path writes the stamp". Both are re-exported from the sweeper module;
# a handler that wrote the literal ``"state_entered_at"`` instead would
# be invisible here — and should be fixed to use the constant, which is
# the whole reason the constant exists.
_STAMP_TOKENS = ("STATE_ENTERED_AT_FIELD", "utc_now_iso")


def _entered_states(call: ast.Call) -> list[str]:
    """``state.set_state(FooStates.bar)`` → ``["FooStates:bar"]``.

    Matches the ``State.state`` string format the sweeper indexes its
    rules by, so the two sides compare without a translation table.
    """
    return [
        f"{arg.value.id}:{arg.attr}"
        for arg in call.args
        if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name)
    ]


def _unstamped_entries() -> list[str]:
    covered = {state.state for state in _app_timeout_rules()}
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for func in ast.walk(tree):
            if not isinstance(func, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            dumped = ast.dump(func)
            if any(token in dumped for token in _STAMP_TOKENS):
                continue
            for node in ast.walk(func):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set_state"
                ):
                    continue
                offenders += [
                    f"{path.name}:{node.lineno} {func.name}() enters {state}"
                    for state in _entered_states(node)
                    if state in covered
                ]
    return offenders


def test_every_timeout_rule_has_a_matching_stamp() -> None:
    """A rule without a stamp is a timeout that can never fire."""
    unstamped = _unstamped_entries()
    assert not unstamped, (
        "these handlers enter a sweeper-covered state without writing "
        "state_entered_at, so the state never expires:\n" + "\n".join(unstamped)
    )


def test_the_scan_actually_finds_the_set_state_calls() -> None:
    """Guard the guard: an AST walk that matches nothing asserts nothing.

    If a refactor moves state entry behind a wrapper, the check above
    silently degrades to a tautology. Pin a floor on how many covered
    entries the walk sees so that degradation fails loudly instead.
    """
    covered = {state.state for state in _app_timeout_rules()}
    seen: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_state"
            ):
                seen.update(s for s in _entered_states(node) if s in covered)
    assert len(seen) >= 20, sorted(seen)
