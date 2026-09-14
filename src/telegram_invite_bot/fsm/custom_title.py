"""FSM states for the ``custom_title`` shop-item activation flow (L-21).

Single-state group: after the user clicks 🎁 Use on a ``custom_title``
inventory entry, the bot prompts for the title text and parks the user
in :attr:`CustomTitleStates.awaiting_title`. The next free-text message
the user sends is sanitized, clamped, persisted as the title privilege,
and the FSM clears. Mirrors legacy's
``register_next_step_handler(call.message, process_custom_title)``
(``bot.py:13850``) → ``process_custom_title`` (``bot.py:23970``).

Why a separate module (not inlined in the handler)
---------------------------------------------------
The FSM sweeper (``scheduler/fsm_sweeper.py``) imports state names to
register timeout rules. Putting states in ``handlers/`` would create a
circular dependency once the sweeper grows a rule (the sweeper imports
the handler; the handler imports the sweeper for
``STATE_ENTERED_AT_FIELD``). Keeping state declarations here mirrors the
``fsm/support.py`` / ``fsm/rps.py`` precedent.

The handler stamps ``STATE_ENTERED_AT_FIELD`` on ``set_state`` so the
global sweeper can reclaim stuck sessions. If a user starts the flow and
never types a title, the sweeper clears the orphaned state after the
timeout — and because the inventory entry is consumed only INSIDE the
title-message step, an abandoned flow loses nothing: the item stays
unused and the user can re-click 🎁 Use later.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CustomTitleStates(StatesGroup):
    """States for the ``custom_title`` activation flow.

    ``awaiting_title``
        Set by the 🎁 Use callback on a ``custom_title`` entry. Cleared
        when the user's next free-text message arrives and the title is
        saved, OR when the global ``/cancel`` handler / FSM sweeper
        fires.
    """

    awaiting_title = State()
