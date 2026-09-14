"""FSM states for the /ad advertiser-request flow (L-61).

Single-state group, same shape as ``fsm/support.py``: the user types one
message (their ad proposal), we forward it to the admin chat and clear
state. There is no multi-step interview.

Why a separate module (not inlined in ``handlers/ads.py``)
----------------------------------------------------------
The FSM sweeper (``scheduler/fsm_sweeper.py``) imports state names to
register timeout rules in ``app.py``. Keeping state declarations here
mirrors the ``fsm/support.py`` / ``fsm/rps.py`` precedent and avoids a
handler<->sweeper import cycle.

Legacy parity: the monolith used ``register_next_step_handler`` plus a
10-minute ``schedule_deletion`` on the form message
(``bot.py:36833-36841``). The strangler equivalent is this state plus a
600-second sweeper :class:`TimeoutRule`, registered in ``app.py``
alongside the other FSM timeouts.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdsStates(StatesGroup):
    """States for the /ad interactive request flow.

    ``awaiting_text``
        Set by ``/ad`` (aliases ``/ads``, ``/reklama``) in private chat
        after the advertiser form is sent. Cleared when the user's next
        free-text message arrives (request forwarded to the admin chat),
        when the inline "cancel" button is pressed, when the global
        ``/cancel`` handler fires, or by the FSM sweeper after 600 s
        (legacy 10-minute form expiry, ``bot.py:36841``).
    """

    awaiting_text = State()
