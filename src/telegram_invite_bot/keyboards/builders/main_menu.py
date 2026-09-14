"""Main-menu inline-button CallbackData factory (A-01 Part B).

The private ``/start`` welcome carries a navigation keyboard so a user
can reach the core read surfaces (profile / balance / referral link)
with a tap instead of remembering the slash command — the legacy
monolith showed an equivalent main menu on ``cmd_start`` and removing
the legacy bridge dropped it.

``action`` is a small closed vocabulary (``profile`` / ``balance`` /
``referral`` / ``home``); the handler dispatches on it and edits the
welcome message in place, so a single message becomes the whole menu.
Every destination carries a "⬅️ back" button (``action="home"``) that
re-renders the menu — no per-user payload is needed because the menu
is identical for everyone and the acting user comes from
``callback.from_user``.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class MainMenu(CallbackData, prefix="menu"):
    """A main-menu navigation tap.

    ``action`` is a small closed vocabulary (``profile`` / ``balance`` /
    ``referral`` / ``shop`` / ``games`` / ``help`` / ``commission`` /
    ``daily`` / ``home``), plus ``help2``, ``help3``… — the help card
    outgrew one Telegram message and pages in place, and the page rides
    *inside* the action rather than in a second field on purpose: adding
    a field would change the packed shape of every button
    (``menu:profile`` → ``menu:profile:1``) and the welcome messages
    already sitting in users' chats would stop unpacking after a deploy.

    No owner id is carried: the menu is public-safe (every destination
    renders the *tapping* user's own data, keyed off
    ``callback.from_user.id``), so a bystander tapping someone else's
    menu just sees their own card — never another user's private data.
    """

    action: str
