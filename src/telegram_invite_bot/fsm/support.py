"""FSM states for the /support ticket-creation flow (T-022).

Single-state group: the user types one message, we save it. There is no
multi-step interview, no acceptance from a second party, and no move
sequence — just one "waiting for text" phase that collapses to
``state.clear()`` on receipt.

Why a separate module (not inlined in the handler)
---------------------------------------------------
The FSM sweeper (``scheduler/fsm_sweeper.py``) imports state names to
register timeout rules. Putting states in ``handlers/`` would create a
circular dependency once the sweeper grows a T-022 rule: the sweeper
imports the handler; the handler imports the sweeper (for
``STATE_ENTERED_AT_FIELD``). Keeping state declarations here mirrors the
``fsm/rps.py`` precedent.

The handler stamps ``STATE_ENTERED_AT_FIELD`` when it calls
``set_state`` so the global sweeper can reclaim stuck sessions — same
contract as ``handlers/rps.py``. If a user starts /support and never
replies, the sweeper will eventually clear the orphaned state; no ticket
row is lost because no row was created yet.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SupportStates(StatesGroup):
    """States for the /support interactive ticket-creation flow.

    A single state suffices: the flow is "user sends one message → we
    save it". There is no back-and-forth after that — the user's next
    action is simply to wait for an admin reply DM.

    ``awaiting_text``
        Set by ``/support`` (or ``/ticket``) when invoked with no
        inline argument. Cleared when the user's next free-text message
        arrives and the ticket is saved, OR when the global ``/cancel``
        handler fires.
    """

    awaiting_text = State()
