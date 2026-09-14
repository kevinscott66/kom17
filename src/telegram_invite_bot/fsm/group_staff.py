"""FSM states for the /groupadmin staff-grant flow (RR-4 #38).

Single-state group. Tapping ➕ on the staff page parks the admin in
:attr:`GroupStaffStates.awaiting_grant`; the next message they send in
that chat is parsed as ``<user_id|@username> <rank>`` and applied.
Mirrors legacy's
``register_next_step_handler(call.message, process_moderation_mod_input)``
(``bot.py:31165``) — but the state is keyed by (chat, user) instead of
legacy's process-global ``temp_data``, so two admins prompting at the
same time no longer overwrite each other's pending action.

Why a separate module (not inlined in the handler)
---------------------------------------------------
Same reason as :mod:`telegram_invite_bot.fsm.custom_title`: the FSM
sweeper imports state names to register timeout rules, and declaring
them inside ``handlers/`` would make the sweeper import the handler
while the handler imports the sweeper's ``STATE_ENTERED_AT_FIELD``.

Abandoning the flow costs nothing — the rank write happens only when a
parseable message arrives, so a swept-away session leaves every rank
exactly as it was. The timeout matters for a different reason: while
the state is set, the admin's ordinary chat messages are being read as
staff input, and nobody should stay in that mode indefinitely.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class GroupStaffStates(StatesGroup):
    """States for granting a rank from the /groupadmin staff page.

    ``awaiting_grant``
        Set by the ➕ button. Cleared when the admin's next message is
        parsed and applied, by ``/cancel``, or by the FSM sweeper.
    """

    awaiting_grant = State()
