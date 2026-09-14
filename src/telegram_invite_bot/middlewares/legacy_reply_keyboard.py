"""Retire the telebot monolith's stuck reply keyboard.

Legacy showed the owner a **persistent** reply keyboard in the private
chat — ``create_owner_quick_reply_keyboard`` (bot.py:16426) with
``👑 Панель разработчика`` / ``🤖 Ком``, and a group-admin flavour
``create_group_admin_reply_keyboard`` (bot.py:16434) with
``🛡️ Панель администратора`` / ``🤖 Ком``. Both were built with
``one_time_keyboard=False``, so Telegram keeps showing them until
somebody sends a ``ReplyKeyboardRemove``.

The new pipeline sends no reply keyboards at all — every menu here is an
*inline* keyboard attached to a message. That has two consequences the
cutover never handled:

* the legacy keyboard is still sitting above the input field, and
  nothing in the new bot will ever take it away;
* its buttons are dead. Tapping one sends its label as plain text, and
  no handler matches those labels — not in the new pipeline, and not in
  legacy either (the three call sites above are the *only* mentions of
  those strings in ``bot.py``; the keyboard was decorative from the day
  it was written).

So this middleware does both halves at once. On a private-chat message
whose whole text is one of the legacy labels it

1. answers with :class:`ReplyKeyboardRemove` attached, which is what
   actually deletes the keyboard from the user's client — permanently,
   and the note explains where the surface moved; and
2. rewrites the message to the ``/command`` that now owns that surface
   and lets the normal routers dispatch it, exactly the way
   :class:`~telegram_invite_bot.middlewares.text_alias.TextAliasMiddleware`
   restores the legacy plain-text shortcuts. No handler logic is
   duplicated, and the target keeps its own authorisation: the developer
   panel stays developer-only whether it is reached by tap or by typing.

Registered before ``TextAliasMiddleware`` so the labels are claimed here
rather than by the alias map (no alias word matches them today, but the
map is edited far more often than this file). Private chats only: these
keyboards were never shown in a group, and matching there would let any
member hijack a conversation by typing the label.

Once the keyboard is gone the labels can only arrive by hand-typing
them, so this middleware quietly becomes dead weight — which is the
intended end state. Delete it when the owner confirms no client still
shows the old keyboard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from aiogram import BaseMiddleware
from aiogram.types import Message, ReplyKeyboardRemove
from loguru import logger

from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject


#: Legacy button label (casefolded) → the command that owns the surface
#: now. ``🛡️`` carries a variation selector in the legacy source, so the
#: keys are written exactly as ``bot.py`` builds them.
LEGACY_BUTTONS: Final[dict[str, str]] = {
    "👑 панель разработчика": "/admin_panel",
    # NOT ``/groupadmin``: that one is group-only
    # (``with_chat_type_refusal(scope="group")``, groupadmin.py:2108) and
    # would answer a tap here with a "wrong chat" refusal. The legacy
    # keyboard only ever appeared in the private chat, and the private
    # multi-group admin surface is ``/mygroups``.
    "🛡️ панель администратора": "/mygroups",
    "🤖 ком": "/ai",
}


class LegacyReplyKeyboardMiddleware(BaseMiddleware):
    """Remove the legacy reply keyboard and route the tap that removed it."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            rewritten: Message | None = None
            try:
                rewritten = await self._retire(event, data)
            except Exception:  # noqa: BLE001 — never break message propagation
                logger.opt(exception=True).warning("legacy reply keyboard retirement failed")
            if rewritten is not None:
                return await handler(rewritten, data)
        return await handler(event, data)

    async def _retire(self, message: Message, data: dict[str, Any]) -> Message | None:
        """Send the removal and return the rewritten message, or ``None``."""
        if message.chat.type != "private":
            return None
        if message.from_user is None or message.from_user.is_bot:
            return None
        text = (message.text or "").strip()
        if not text:
            return None
        target = LEGACY_BUTTONS.get(text.casefold())
        if target is None:
            return None

        # Same reasoning as TextAliasMiddleware: an FSM text step owns
        # whatever the user sends while it is running, and stealing its
        # input would be a worse bug than a keyboard that outlives its
        # bot. ``raw_state`` is resolved by aiogram's update-level
        # FSMContextMiddleware, so this costs no storage round-trip on
        # the normal path; the ``state`` fallback covers a hand-built
        # ``data`` (tests, a bare dispatcher).
        if "raw_state" in data:
            in_fsm_step = data["raw_state"] is not None
        else:
            state = data.get("state")
            in_fsm_step = state is not None and await state.get_state() is not None
        if in_fsm_step:
            return None

        lang = str(data.get("lang") or "ru")
        try:
            await message.answer(
                t("h_legacy_keyboard_retired", lang),
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception as exc:  # noqa: BLE001 — routing the tap still stands
            logger.bind(chat_id=message.chat.id, exc=repr(exc)).info(
                "legacy reply keyboard removal not delivered",
            )
        return message.model_copy(update={"text": target, "entities": None})
