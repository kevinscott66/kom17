"""``/cancel`` — clear any in-flight FSM state (Stage 36 + T-006 i18n).

Legacy ``/cancel`` (bot.py:23825) is the global escape-hatch for
users who got stuck mid-flow: a withdraw form expecting an amount, a
P2P-trade prompt expecting a username, etc. The legacy implementation
pops from a hand-rolled ``threading.Lock``-protected dict of FSM-like
state (``p2p_state``, ``withdraw_state``) plus a separate
``clear_temp_data(user_id)``.

The new pipeline doesn't carry any of those legacy dicts forward —
they're replaced by aiogram's :class:`FSMContext` backed by
:class:`MemoryStorage` in this process. So the port collapses to a
single ``state.clear()`` call: the FSM key is ``(chat_id, user_id)``
(aiogram default), and ``clear()`` removes both the state name AND the
accumulated context data atomically — semantics identical to what the
legacy two-line dict pop + ``clear_temp_data`` achieved for its own
state.

Behaviour parity:

* Works in **any** chat type. Legacy gates only on
  ``ensure_user_access`` (role check, not chat-type) — same as
  heartbeat — and we accept all chats for the same reason: the
  command's whole job is being available when the user is confused
  about where they are.
* Reply is bilingual via ``t("cancel", lang)`` with language detection
  from Telegram ``language_code``. We don't go through ``UserService``
  here because ``/cancel`` must work even when the user record doesn't
  exist yet (a /cancel issued before /start — rare but legal).
* Idempotent: calling ``/cancel`` with no state set still replies
  with the confirmation. Legacy does the same (the dict ``.pop`` is
  a no-op when the key is missing), and the user-visible promise of
  ``/cancel`` is "the next message I send won't be eaten by an
  unfinished flow" — that's true whether or not there was one.

**T-006**: Migrated from ``_REPLY_RU``/``_REPLY_EN`` constants to
``t("cancel", lang)`` i18n lookup (data/{ru,en}.yaml).

**#259**: the "single ``state.clear()``" above is true of every flow
that belongs to one user. A /cpc or /duel match does not — it is two
people, a live card in someone else's chat, and a resolution step
that moves coins. Those states are handed to the game module that
owns them (:func:`_teardown_game_flow`) so the clear lands under the
same per-match lock every other exit from that match already takes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.handlers import duel, rps
from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.fsm.context import FSMContext
    from aiogram.types import Message


log = logger.bind(component="handlers.cancel")


async def _teardown_game_flow(
    bot: Bot,
    *,
    state: FSMContext,
    state_name: str,
    data: dict[str, object],
    caller_id: int,
    lang: str,
) -> bool:
    """Hand a live game FSM to the handler that owns it (#259).

    Returns ``True`` when a game module took ownership. It has then
    already cleared the state — under its own per-match lock, which is
    the whole point — so the caller must not clear it a second time
    outside that lock.

    M-G-4 originally inlined the cleanup here: a state-prefix branch and
    a few best-effort ``edit_reply_markup(None)`` calls, on the reasoning
    that "today's two flows are cheap enough to inline". They were not.
    Both flows had by then grown a terminal teardown of their own —
    :func:`rps.handle_cpc_cancel` and each game's expiry callbacks — and
    every one of those holds the per-match lock across the clear and
    tells the other seat what happened. The copy living here did
    neither, which made ``/cancel`` a second, unsynchronised way to end
    a match whose resolution step moves coins: the challenger could
    clear the FSM in the gap between a decisive click reading its
    snapshot and the service committing the payout.

    So the branch no longer does the work, it only picks the owner.
    Nothing is inlined that a game module could drift away from.
    """
    if state_name.startswith("RpsStates:"):
        await rps.cancel_match_from_global(
            bot,
            state=state,
            state_name=state_name,
            data=data,
            caller_id=caller_id,
            lang=lang,
        )
        return True
    if state_name.startswith("DuelStates:"):
        await duel.cancel_match_from_global(
            bot,
            state=state,
            state_name=state_name,
            data=data,
            caller_id=caller_id,
            lang=lang,
        )
        return True
    return False


async def handle_cancel(message: Message, state: FSMContext, bot: Bot, lang: str) -> None:
    """Drop FSM state + data, then confirm to the user.

    M-G-4 / #259: when the prior state belongs to a known game flow
    (RpsStates / DuelStates), read FSM data BEFORE clearing and hand the
    whole teardown to the handler that owns that flow — see
    :func:`_teardown_game_flow` for why it is a hand-off rather than the
    few inline keyboard edits it started as. Everything else still
    collapses to the single ``state.clear()`` the legacy port promised.
    """
    # We log the *prior* state before clearing so an operator can
    # correlate "user X cancelled" with which flow they were in. The
    # state name is a free-form string set by whichever flow set it;
    # surface it raw, not the underlying StorageKey (which leaks
    # internal aiogram structure).
    prior = await state.get_state()
    handled = False
    if prior is not None and message.from_user is not None:
        data = await state.get_data()
        try:
            handled = await _teardown_game_flow(
                bot,
                state=state,
                state_name=prior,
                data=data,
                caller_id=message.from_user.id,
                lang=lang,
            )
        except Exception:
            # The owner's teardown is the preferred path, not the only
            # one: /cancel's user-visible promise is "the next message I
            # send won't be eaten by an unfinished flow", and that has to
            # hold even when a keyboard edit or a notify DM blows up.
            # ``handled`` stays False, so the unconditional clear below
            # still runs.
            log.exception("/cancel game teardown failed; continuing to clear FSM")
    if not handled:
        await state.clear()
    reply = "✅ " + t("cancel", lang)
    await message.answer(reply)
    log.bind(
        user_id=message.from_user.id if message.from_user else None,
        chat_id=message.chat.id if message.chat else None,
        prior_state=prior,
    ).info("/cancel cleared FSM state")


def build_router() -> Router:
    """No registry needed — FSMContext is injected by the Dispatcher's
    storage middleware automatically. No chat-type filter at the router
    level: ``/cancel`` is the one command that legitimately MUST work
    anywhere a user might be stuck.
    """
    router = Router(name="cancel")
    # ``отмена`` is not decoration: this is the command a *stuck* user
    # reaches for, the audience is Russian-speaking, and every other
    # command in the catalog carries a Russian alias (``/вывод``,
    # ``/п2п``, ``/передать_права``). The escape hatch was the one that
    # did not.
    #
    # The prompts still *say* ``/cancel``, deliberately: Telegram only
    # renders an ASCII ``/command`` as a tappable link, so the English
    # spelling is the one worth putting in copy. The alias is for the
    # user who types the word they were going to type anyway.
    router.message.register(handle_cancel, Command("cancel", "отмена", ignore_case=True))
    return router
