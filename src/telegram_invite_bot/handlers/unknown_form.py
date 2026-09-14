"""Answering a known command written in an unknown form (#158).

#122 and #123 closed the *chat-type* half of "the bot said nothing":
a command gated to groups now refuses a DM instead of never matching.
This is the other half, and it has the same root cause.

Almost every ported module registers its handlers with a ``magic``
guard on the arguments — ``F.args.is_(None)`` for the bare form, a
predicate for each accepted shape — and the modules' own tests then
assert that anything off-contract falls through:

    ``/roll abc``, ``/roll 7``, ``/daily foo``, ``/balance @someone``,
    ``/stats 30``, ``/top winrate``, ``/achievements @someone`` …

Falling through was correct while the telebot monolith still ran
beside us: legacy owned those forms and rendered a usage hint for each
(``cmd_roll``'s "Без ставки: число от 1 до 6" at ``bot.py:17385``, and
its siblings). The legacy bridge went away in T-011. A fall-through is
now a dropped update, so a user who mistypes an argument gets exactly
nothing back — no error, no hint, no acknowledgement that the bot even
saw the message. That is worse than a wrong answer: it reads as "the
bot is broken" rather than "I typed it wrong".

So the tail of the router tree gets one more child. It matches the
command *words the tree itself registers* and nothing else, with no
argument guard at all, so it is reached only by an invocation every
real handler declined. The reply names the command, repeats the
one-line description ``/help`` shows for it, and points at ``/help``.

Two properties worth keeping:

* **The word list is derived, never hand-kept.** It is read off the
  assembled router with the same walk the chat-type refusal uses
  (:func:`~handlers.chat_scope.command_specs`), so a command added,
  renamed or retired anywhere changes this list in the same edit.
  ``ignore_case`` and ``prefix`` are carried over per group of words,
  so the hint matches exactly the spellings a real handler would have.
* **Commands nobody may know about stay silent.** ``command_specs``
  drops owner-tier words for the reason #123 documented, and this
  module drops everything ``/help`` cannot describe on top of that.
  The rank check alone was not enough: the ~110 ``/admin_*`` console
  words, ``/payment_keys`` and ``/set_crypto_token`` carry no catalog
  row, so they resolve to rank 0 and sailed straight through it. The
  rule that actually holds is narrower and depends on no table being
  kept current — **if the command is not in ``/help``, we do not
  confirm that it exists.** A command belonging to some *other* bot in
  the group is not in the list at all, so this never answers for it
  either.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command

from telegram_invite_bot.core.ranks import command_entry, command_key_for
from telegram_invite_bot.handlers.chat_scope import command_specs
from telegram_invite_bot.handlers.help_catalog import HELP_HIDDEN_KEYS
from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message


def _describable(word: str) -> bool:
    """Whether the bot may say out loud that ``word`` is one of its own.

    Two exclusions, both about not advertising:

    * **No catalog row.** ``command_key_for`` maps an unknown alias to
      itself so the rank override table can still pin it, which means a
      word absent from the catalog resolves to rank 0 — it passes the
      owner-tier check in :func:`~handlers.chat_scope.command_specs`
      while being *less* public than the words that check exists to
      protect. Every ``/admin_*`` console word sits in that bucket, as
      do ``/payment_keys`` and ``/set_crypto_token``. Requiring a row is
      what actually keeps them quiet.
    * **``HELP_HIDDEN_KEYS``.** Same principle that keeps them out of
      ``/help``: a command the catalog lists but the surface
      deliberately does not show must not be described as if it were on
      offer.

    What survives is exactly the set ``/help`` prints, so the hint can
    never name a command the user had no other way to discover — and
    never renders a raw ``h_cmd_<key>`` either, since a catalog row
    implies the translation (the i18n convergence suite fails
    otherwise).
    """
    key = command_key_for(word)
    return key not in HELP_HIDDEN_KEYS and command_entry(key) is not None


async def handle_unknown_form(
    message: Message,
    command: CommandObject,
    lang: str,
) -> None:
    """Reply to a known command whose argument form matched no handler.

    The command is echoed as the user typed it and HTML-escaped: the
    word reaching here is one of ours, but its *casing* is the user's
    and ``prefix`` may be any character a module registered, so nothing
    about the echoed token is guaranteed inert under HTML parse mode.

    Only reachable for words :func:`_describable` accepted at build
    time, so the description always exists. The guard repeats it anyway:
    "we never name a command we cannot describe" is what keeps the
    developer console out of this reply, and a property carrying that
    much weight should not rest on one call site's argument list.
    """
    if not _describable(command.command):  # pragma: no cover - filtered at build
        return
    await message.answer(
        t(
            "h_unknown_form",
            lang,
            command=html.escape(command.command),
            description=t(f"h_cmd_{command_key_for(command.command)}", lang),
        )
    )


def build_unknown_form_router(tree: Router) -> Router:
    """A router that hints for every command word ``tree`` registers.

    Include it *after* ``tree``'s own children: aiogram walks routers in
    include order, so anything a real handler accepts never reaches
    here. Called with the assembled root before the root includes the
    result — the walk must not see this router's own registrations, or
    the hint would advertise itself as the source of its own word list.
    """
    router = Router(name="unknown_form")
    for (ignore_case, prefix), words in sorted(command_specs(tree).items()):
        describable = sorted(word for word in words if _describable(word))
        if not describable:
            continue
        router.message.register(
            handle_unknown_form,
            Command(*describable, ignore_case=ignore_case, prefix=prefix),
            # Same guard as the chat-type refusal: an anonymous group
            # admin and a channel post carry no ``from_user``, they
            # already fall through silently everywhere else, and making
            # this router the one place that answers them would be a
            # behaviour change with no bug behind it.
            F.from_user,
        )
    return router
