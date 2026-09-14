"""Non-text input during a text FSM step must not read as a dead bot.

Every text step of every interview in this bot is registered with an
``F.text`` filter next to its :class:`~aiogram.filters.StateFilter` —
``checks`` (amount, count), ``withdraw`` (amount), ``p2p`` (amount,
limits), ``p2p_trade`` (buy amount, express fiat), ``shop`` (custom
title), ``transfer_rights`` (target), ``support`` (ticket text),
``ads`` (ad text), ``groupadmin`` (staff grant, filtered word). The
filter is right: a sticker is not an amount, and letting one through
would only push the parse failure one line deeper.

What was missing is the other half. A photo, a voice message or a
forwarded post sent at that moment matches **nothing**: the step
declines it, no other handler claims an update from a user who is
mid-flow, and the dispatcher drops it. The user, who has just been
asked a question, gets no answer to what they sent — the same silence
#122 and #123 removed from the command surface, in the one place where
the bot itself asked the question. It reads as the bot having died
mid-conversation, and the natural response is to send it again.

:func:`register_text_expected` closes that: one extra registration per
step, matching the same state with the opposite content, that repeats
what is expected and points at the cancel button.

Two deliberate narrowings:

* Only content a human can *send on purpose* is answered
  (:data:`_HUMAN_CONTENT`). Service events — someone joining, a pin, a
  chat title change — arrive as messages too, and a group step
  (``groupadmin``) would otherwise reply "send me text" to the room
  every time somebody joins while an admin has a filter word pending.
* Commands are text, so ``/cancel`` and ``/help`` never reach this
  handler. They must not reach the *step* either — that is what
  :data:`NOT_A_COMMAND` is for, and every text step registers it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from aiogram import F
from aiogram.filters import StateFilter

from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from aiogram import Router
    from aiogram.fsm.state import State
    from aiogram.types import Message

#: Content types a person deliberately sends. Anything outside this set
#: is either text (handled by the step itself) or a service event the
#: user did not author, and replying to those would be noise.
_HUMAN_CONTENT: Final[frozenset[str]] = frozenset(
    {
        "photo",
        "video",
        "animation",
        "document",
        "audio",
        "voice",
        "video_note",
        "sticker",
        "contact",
        "location",
        "venue",
        "poll",
        "dice",
        "game",
        "story",
    }
)


#: A slash command typed mid-interview belongs to the command's own
#: handler, not to the step that happens to be waiting for text.
#:
#: Every text step is gated on an FSM state, and the dispatcher walks
#: routers in include order: whichever router is included first wins.
#: So a step registered early (``support``, ``checks``) claimed every
#: command owned by a later one (``/withdraw``, ``/p2p``, ``/nick`` …)
#: and answered it with "wrong format" — the command never ran, and the
#: only visible symptom was a nonsense complaint about a number.
#:
#: Registered next to ``F.text``, this restores the obvious contract:
#: an interview claims answers, not commands. It deliberately leaves
#: state alone — the command runs, the step stays pending, and the user
#: can still answer it (or let the FSM sweeper expire it).
#:
#: Safe on non-text messages: ``startswith`` on a missing ``text``
#: resolves to ``False``, so a photo passes this filter and is answered
#: by :func:`register_text_expected` as before.
NOT_A_COMMAND: Final = ~F.text.startswith("/")


async def _handle_text_expected(message: Message, lang: str) -> None:
    """Repeat what the step wants. Deliberately does NOT clear state:
    the user's answer is one text message away, and dropping the
    interview because they sent a picture first would be worse than the
    silence this replaces."""
    await message.reply(t("h_fsm_text_expected", lang))


def register_text_expected(router: Router, *states: State) -> None:
    """Answer non-text input while ``router`` holds any of ``states``.

    Registration order does not matter — this handler and the step it
    accompanies are mutually exclusive by content type — but call sites
    put it next to the step it guards so the pair stays visible in one
    place.
    """
    router.message.register(
        _handle_text_expected,
        StateFilter(*states),
        F.content_type.in_(_HUMAN_CONTENT),
        F.from_user,
    )
