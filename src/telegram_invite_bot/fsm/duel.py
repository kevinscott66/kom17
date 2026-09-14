"""FSM states for the /duel challenge-accept / roll dance (T-018).

Twin of :mod:`telegram_invite_bot.fsm.rps`. Same Variant A posture:
ONE FSM session per match, owned by the challenger; opponent-side
callbacks carry ``challenger_id`` in the payload so the handler can
locate the session from any seat. See ``fsm/rps.py``'s docstring for
the full rationale; the duel port differs only in being group-chat
scoped (FSM key uses the group ``chat_id`` rather than the
challenger's private chat).

Two states:

* ``awaiting_acceptance`` — opponent has been pinged via the inline
  Accept/Decline buttons on the challenge card. No coins at stake
  yet (escrow happens only at resolution — ADR 0009).
* ``awaiting_rolls`` — opponent accepted; both seats have a "🎲 Roll"
  button. Either side may have already rolled (stored as
  ``challenger_roll`` / ``opponent_roll`` in FSM data); resolution
  fires when both are non-None.

FSM data dict carries::

    {
        "opponent_id": int,
        "bet": int,
        "chat_id": int,                  # group chat id
        "state_entered_at": str,         # ISO UTC, sweeper deadline ref
        "challenge_message_id": int | None,
        # filled in after opponent accepts:
        "challenger_roll": int | None,
        "opponent_roll": int | None,
    }
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class DuelStates(StatesGroup):
    """States for the /duel challenge flow, owned by the challenger.

    Terminal outcomes (decline, /cancel, both-rolled-and-resolved,
    escrow failure) all collapse to ``state.clear()`` — no named
    DONE state, the next /duel reopens the flow from scratch.
    """

    awaiting_acceptance = State()
    awaiting_rolls = State()
