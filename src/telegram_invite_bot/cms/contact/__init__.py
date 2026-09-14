"""Public contact form — the one channel that works without the bot.

Every contact the service published before this package existed lived
*inside* Telegram: the ``/support`` ticket system, and an optional
``@username``. That is enough for a user, and not enough for the
acquiring bank's reviewer, a regulator, or anyone who has to reach the
operator before deciding whether to install anything at all.

The alternative — printing a personal mailbox on a public page — is a
permanent spam address the operator cannot retract. So the page carries
a form instead: the sender writes a message, the site hands it to the
bot, and the bot delivers it to the operator's own chat. Nothing is
stored, nothing is published, and the address on the page is the site's
own.
"""

from __future__ import annotations

from telegram_invite_bot.cms.contact.router import build_router

__all__ = ["build_router"]
