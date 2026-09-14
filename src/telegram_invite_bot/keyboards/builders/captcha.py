"""Join-captcha inline-keyboard CallbackData factory (L-55, cluster G2).

One wire format — Confirm — for the "I'm not a bot" button posted under
the captcha notice when a new member joins a captcha-enabled group
(:mod:`telegram_invite_bot.handlers.group_events`).

The payload carries ``user_id`` — the joiner the notice belongs to — so
the handler can reject anyone else tapping the button (authorization is
``callback.from_user.id == payload.user_id``; the chat id is read from
``callback.message.chat.id``, carrying it would only inflate the wire
format).

Prefix ``cap_ok`` is distinct from every legacy callback payload, so the
strangler bridge regression pin holds: the new pipeline's filter cannot
match a legacy callback and vice-versa.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt


class CaptchaConfirm(CallbackData, prefix="cap_ok"):
    """ "I'm not a bot" button on the join-captcha notice.

    ``user_id`` is the newcomer who must press it. The handler verifies
    the presser's identity against this field — the payload itself is
    not secret (any member can read button payloads via the Bot API),
    but pressing it only ever lifts the presser's OWN restriction, so
    spoofing buys nothing.
    """

    user_id: DbInt
