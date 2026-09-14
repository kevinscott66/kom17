"""Aiogram FSM state vocabularies for multi-turn handler flows.

Stage 34 lands the first inhabitant of this package
(:class:`RpsStates` for the /cpc challenge-accept / choose-move
dance). Subsequent flows (withdraw, p2p-trade) will add sibling
modules — one per cross-handler flow, named after the slash command
that owns the state machine.

Splitting the state classes out of ``handlers/*`` keeps the wire
contract (state names + the FSM data shape each state implies)
co-located with the contract document (the module docstrings here)
rather than buried inside a handler that also owns rendering. A
future stage that ports legacy's withdraw FSM will land
``fsm/withdraw.py`` here without touching this ``__init__``.
"""

from telegram_invite_bot.fsm.rps import RpsStates

__all__ = ["RpsStates"]
