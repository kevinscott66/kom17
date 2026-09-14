"""The answer a group-only command owes a private chat (#122).

A command pinned to groups with ``F.chat.type.in_(GROUP_TYPES)`` on its
registration and nothing else does not *refuse* a private invocation —
it never matches one. aiogram walks the routers, finds no handler, drops
the update, and the user gets nothing at all: no error, no hint, no
clock. Several handlers carry a comment saying private use "falls
through to legacy, where it'll get the same refusal", and that was true
while the telebot process still ran beside this one. It doesn't, so the
fallthrough lands on the floor.

Silence is the worst refusal there is. It reads as "the bot is broken"
or "I typed it wrong", and both send the user to support instead of to
the group where the command works.

So every group-only family registers this handler a second time under
``F.chat.type == ChatType.PRIVATE``. Two chat-type-disjoint
registrations of the same command word is the shape
:mod:`~handlers.report` already uses — the group half keeps its own
filters, this half can never shadow it, and the refusal is one line per
family rather than a hand-written branch inside each handler.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message


async def handle_group_only(
    message: Message,
    command: CommandObject,
    lang: str,
) -> None:
    """Tell a private-chat caller the command lives in groups.

    Echoes back the alias the user actually typed rather than a
    canonical name: someone who typed ``/пары`` should not be told
    about ``/marriages``. aiogram fills
    :attr:`CommandObject.command` from the message itself, and the
    ``Command`` filter has already matched it against this handler's
    own alias list — so the value is one of our own words, not free
    user input. Escaped anyway, because the answer renders as HTML and
    the alias list is one regexp away from admitting something else.
    """
    await message.answer(t("h_group_only_command", lang, command=html.escape(command.command)))
