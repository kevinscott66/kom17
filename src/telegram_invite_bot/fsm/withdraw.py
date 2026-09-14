"""FSM states for the /withdraw flow (#28, T-027).

Two-step interview: the user names an amount, then confirms it on an
inline card before the request is created (and the coins escrowed).

``awaiting_amount``
    Set by ``/withdraw`` (or ``/вывод``) when invoked with no inline
    argument. The next free-text message is parsed as the COM amount.
    On a valid in-band amount the flow advances to ``awaiting_confirm``;
    an invalid / out-of-band amount keeps the user here with a hint.

``awaiting_confirm``
    The amount card with ✅/❌ buttons is shown. The amount is stashed
    in FSM data (source of truth — never trusted off the callback wire).
    Cleared when the user confirms (request created) or cancels, or by
    the global ``/cancel``.

Why a separate module (not inlined in the handler): the FSM sweeper
(``scheduler/fsm_sweeper.py``) imports state names to register timeout
rules, and inlining would invert the dependency (handler imports
sweeper for ``STATE_ENTERED_AT_FIELD``; sweeper imports handler for the
states). Mirrors the ``fsm/support.py`` / ``fsm/rps.py`` precedent.

If a user starts /withdraw and never finishes, the sweeper reclaims the
orphaned state — and because no coins are escrowed until the *confirm*
step, an abandoned interview costs nothing: no row, no debit.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class WithdrawStates(StatesGroup):
    """States for the /withdraw amount→confirm interview."""

    awaiting_amount = State()
    awaiting_confirm = State()
