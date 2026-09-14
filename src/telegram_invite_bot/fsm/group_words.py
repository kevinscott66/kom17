"""FSM states for the /groupadmin word-filter add flow (RR-4 #43).

Single-state group. Tapping ➕ on the Words page parks the admin in
:attr:`GroupWordsStates.awaiting_word`; the next message they send in
that chat becomes a banned word for THAT group. Mirrors legacy's
``register_next_step_handler(call.message, process_banned_word)``
(``bot.py:32408``) with two differences that matter:

* legacy's pending action lived in a process-global ``temp_data`` dict
  keyed by user id (``bot.py:32413``), so one admin prompting in two
  chats overwrote themselves; the aiogram FSM key is (chat, user);
* legacy's ``profanity_filter`` was a single process-wide word list, so
  the word an admin typed here was banned in EVERY group the bot served
  — ours writes ``word_filters`` for the group whose card was tapped.

Why a separate module (not inlined in the handler)
---------------------------------------------------
Same reason as :mod:`telegram_invite_bot.fsm.group_staff`: the FSM
sweeper imports state names to register timeout rules, and declaring
them inside ``handlers/`` would make the sweeper import the handler
while the handler imports the sweeper's ``STATE_ENTERED_AT_FIELD``.

Abandoning the flow costs nothing — the word is stored only when a
message actually arrives. The timeout exists because while the state is
set the admin's ordinary chat messages are being read as filter input,
and that is not a mode anyone should sit in indefinitely.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class GroupWordsStates(StatesGroup):
    """States for adding a filter word from the /groupadmin Words page.

    ``awaiting_word``
        Set by the ➕ button. Cleared when the admin's next message is
        stored (or refused), by ``/cancel``, or by the FSM sweeper.
    """

    awaiting_word = State()
