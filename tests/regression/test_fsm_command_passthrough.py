"""An interview must claim answers, not commands (#162).

Every text step of every interview is registered as ``StateFilter(...)``
plus ``F.text``. That pair says "any text, while this state is set" —
and a slash command is text. The dispatcher walks routers in include
order and stops at the first match, so a step in a router included
*early* silently outranked the ``Command`` handler of every router
included *later*: a user who was asked for an amount and typed
``/withdraw`` got "неверная сумма" back, and the command never ran.

Four steps already carried the ``~F.text.startswith("/")`` carve-out
(``ads``, ``broadcast``, both ``groupadmin`` ones). The other ten did
not. A sweep of the assembled tree over every (state × registered
command × chat type) triple counted **2610** such captures across nine
states before the fix.

The carve-out now lives in one place —
:data:`~telegram_invite_bot.handlers.fsm_text.NOT_A_COMMAND` — and this
file guards it from two sides:

* **The invariant**, over the whole production tree: no message handler
  gated on a real FSM state may match a slash command unless it *is* a
  command handler. This is what catches the eleventh interview, added
  later by someone who copied one of the ten.
* **The symptom**, end-to-end through router order: for a handful of
  (state, command) pairs that used to be captured, resolution now lands
  on exactly the handler it lands on when no state is set.

What deliberately did **not** change: the step keeps its state. The
command runs, the interview stays pending, and the user can still
answer it or let the FSM sweeper expire it.

``/support`` used to be the one exception — it carried a router-level
``StateFilter(None)`` and so landed on ``unknown_form`` mid-interview,
telling a user who typed the escape hatch correctly that they had used
the wrong *argument form*. #165 removed that gate: the command now runs
from any state and decides for itself (see ``handlers/support.py``), so
nothing in the tree refuses a command purely because a state is set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram import Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from telegram_invite_bot.fsm.support import SupportStates
from telegram_invite_bot.handlers.checks import CheckCreateStates
from telegram_invite_bot.handlers.p2p import P2pSellStates
from telegram_invite_bot.handlers.transfer_rights import TransferStates

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aiogram import Dispatcher, Router

pytestmark = pytest.mark.integration

# Chat types both halves are checked in: the private interviews and the
# two group-scoped ``groupadmin`` steps.
_CHAT_TYPES = ("private", "supergroup")

# Enough shapes to catch a carve-out written as ``!= "/cancel"`` or as a
# Latin-only prefix test.
_COMMAND_TEXTS = ("/help", "/withdraw 100", "/п2п", "/zzz_not_a_command")


def _message(text: str, chat_type: str) -> Message:
    """A plain text message from a real user in ``chat_type``."""
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 1_700_000_000,
            "chat": {"id": -100 if chat_type != "private" else 42, "type": chat_type},
            "from": {"id": 42, "is_bot": False, "first_name": "T"},
            "text": text,
        }
    )


def _routers(router: Router) -> Iterator[Router]:
    """``router`` and every router below it, in dispatch order."""
    yield router
    for sub in router.sub_routers:
        yield from _routers(sub)


def _filters(handler: Any) -> list[Any]:
    """The filter objects of a handler, unwrapped from aiogram's
    ``CallableObject`` envelopes (``handler.filters`` holds wrappers,
    and the thing worth type-checking is what they call)."""
    return [flt.callback for flt in (handler.filters or [])]


def _gating_state(filters: list[Any]) -> str | None:
    """The raw state string this handler is gated on, if any.

    ``None`` for a handler that is stateless or explicitly gated on
    "no state" (``StateFilter(None)``) — neither can capture a command
    from a user who is mid-interview, which is the failure under test.
    """
    for flt in filters:
        if not isinstance(flt, StateFilter):
            continue
        for state in flt.states:
            if isinstance(state, State):
                return state.state
            if isinstance(state, str):
                return state
            if isinstance(state, type) and issubclass(state, StatesGroup):
                names = state.__state_names__
                if names:
                    return str(names[0])
    return None


def _owner(handler: Any) -> str:
    fn = handler.callback
    return f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', '?')}"


# ── the invariant ───────────────────────────────────────────────────


@pytest.mark.parametrize("chat_type", _CHAT_TYPES)
@pytest.mark.parametrize("text", _COMMAND_TEXTS)
async def test_no_state_step_claims_a_slash_command(
    production_dispatcher: Dispatcher, chat_type: str, text: str
) -> None:
    """A state-gated handler that is not a command handler must decline
    slash-command text — whatever router it sits in, whatever chat it
    sits in, and whichever router happens to own that command."""
    bot = Bot(token="123:abc")
    message = _message(text, chat_type)
    offenders: list[str] = []
    try:
        for router in _routers(production_dispatcher):
            for handler in router.message.handlers:
                filters = _filters(handler)
                if any(isinstance(flt, Command) for flt in filters):
                    continue  # a command handler is *supposed* to match
                raw_state = _gating_state(filters)
                if raw_state is None:
                    continue
                matched, _ = await handler.check(message, bot=bot, raw_state=raw_state)
                if matched:
                    offenders.append(f"{_owner(handler)} @ {raw_state}")
    finally:
        await bot.session.close()

    assert not offenders, (
        f"these steps swallow {text!r} instead of letting the command run: "
        + ", ".join(sorted(offenders))
        + " — add NOT_A_COMMAND (handlers/fsm_text.py) beside the F.text filter"
    )


async def test_no_command_is_gated_on_having_no_state(
    production_dispatcher: Dispatcher,
) -> None:
    """The mirror image of the invariant above (#165).

    That one stops an interview step from eating a command. This one
    stops a command from opting out of being reachable: a ``Command``
    handler that also carries ``StateFilter(None)`` runs only when no
    state is set, so a user mid-interview gets the ``unknown_form``
    hint — told they used the wrong *argument form* of a command they
    typed perfectly.

    ``/support`` shipped that way and it was the escape hatch, dead in
    the one situation it exists for. A command may still decline while
    another flow is open — but that is a decision for the handler,
    which can say *why*, not for a router filter, which can only go
    silent.
    """
    offenders: list[str] = []
    for router in _routers(production_dispatcher):
        for handler in router.message.handlers:
            filters = _filters(handler)
            if not any(isinstance(flt, Command) for flt in filters):
                continue
            if any(isinstance(flt, StateFilter) and None in flt.states for flt in filters):
                offenders.append(_owner(handler))

    assert not offenders, (
        "these commands are unreachable from inside any interview: "
        + ", ".join(sorted(offenders))
        + " — drop the StateFilter(None) and reject unwanted states inside "
        "the handler instead, so the user gets an explanation (see "
        "handlers/support.py and handlers/ads.py)"
    )


# ── the symptom ─────────────────────────────────────────────────────


async def _resolve(router: Router, message: Message, **data: Any) -> str | None:
    """Which handler the dispatcher would run, by the same walk it uses:
    router filters first, then this router's handlers in registration
    order, then the sub-routers in include order."""
    for flt in router.message._handler.filters or []:  # noqa: SLF001 — no public accessor
        if not await flt.call(message, **data):
            return None
    for handler in router.message.handlers:
        matched, _ = await handler.check(message, **data)
        if matched:
            return _owner(handler)
    for sub in router.sub_routers:
        found = await _resolve(sub, message, **data)
        if found is not None:
            return found
    return None


# Each pair is a capture the sweep reported: the state belongs to a
# router included before ``withdraw`` (position 904 in main_router), so
# its text step used to answer ``/withdraw`` with a parse complaint.
@pytest.mark.parametrize(
    "state",
    [
        SupportStates.awaiting_text,
        CheckCreateStates.awaiting_amount,
        P2pSellStates.awaiting_amount,
        TransferStates.awaiting_target,
    ],
    ids=lambda s: str(s.state),
)
async def test_a_command_resolves_the_same_mid_interview(
    production_dispatcher: Dispatcher, state: State
) -> None:
    """``/withdraw`` typed mid-interview reaches the same handler it
    reaches when nothing is pending — the interview is not in its way."""
    bot = Bot(token="123:abc")
    message = _message("/withdraw", "private")
    try:
        baseline = await _resolve(production_dispatcher, message, bot=bot, raw_state=None)
        in_state = await _resolve(production_dispatcher, message, bot=bot, raw_state=state.state)
    finally:
        await bot.session.close()

    assert baseline is not None, "/withdraw resolves to nothing at all — wiring changed"
    assert in_state == baseline
