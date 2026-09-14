"""Deep-link payloads the bot answers to, and the URLs that carry them.

A Telegram deep link is ``https://t.me/<bot>?start=<payload>``: pressing
it opens a private chat and sends ``/start <payload>``. Every payload
this bot understands is namespaced by a prefix so the routers that own
each domain can claim theirs with ``CommandStart(deep_link=True,
magic=F.args.startswith(...))`` and never see each other's traffic —
``ref_`` (``handlers/start.py``), ``check_`` (``handlers/checks.py``)
and, since #1926, ``grp_``.

``grp_`` answers a question every group→DM button used to drop on the
floor: *which group did this person come from?* The buttons all pointed
at a bare ``?start``, so a member who tapped "open in private" from a
chat arrived in a DM the bot could not connect to that chat — anything
group-scoped (the group's share of what they spend, the panel they were
reaching for) had nothing to key off. The payload carries the source
chat id across the jump; :data:`~db.models.user_settings.UserSetting.current_group_id`
is where the DM side keeps it.

Only the *shape* lives here, deliberately: this module has no I/O and
no aiogram import, so the three button sites (``chat_scope``,
``start``, ``group_events``) can build a link without importing a
handler, and the handler can parse one without importing them.

Telegram's payload alphabet is ``A-Za-z0-9_-``, up to 64 characters. A
chat id spends at most 14 of them plus the sign, so ``grp_`` never
comes close to the ceiling — but :func:`parse_group_payload` still
validates rather than trusting, because the payload is a URL anyone can
retype by hand.
"""

from __future__ import annotations

from typing import Final

from telegram_invite_bot.utils.numbers import parse_int_token

#: Namespace for "this DM continues a conversation that started in
#: group ``<chat_id>``".
GROUP_PREFIX: Final = "grp_"


def group_payload(chat_id: int) -> str:
    """The ``?start=`` payload naming ``chat_id`` as the source group."""
    return f"{GROUP_PREFIX}{chat_id}"


def dm_start_url(username: str, *, group_chat_id: int | None = None) -> str:
    """Link into the bot's DM, optionally carrying the source group.

    ``group_chat_id=None`` yields the plain ``?start`` every one of
    these buttons used to hard-code — still the right answer when there
    is no group behind the tap (a channel, or a caller we can't
    attribute).
    """
    if group_chat_id is None:
        return f"https://t.me/{username}?start"
    return f"https://t.me/{username}?start={group_payload(group_chat_id)}"


def parse_group_payload(args: str | None) -> int | None:
    """Chat id out of a ``grp_`` payload, or ``None`` if it isn't one.

    ``None`` covers every malformed spelling — wrong namespace, empty
    tail, non-numeric, a float, a leading ``+`` — because the caller's
    next move is the same for all of them: ignore the payload and show
    the ordinary card. A deep link is user-editable, so "0" and
    "-1001234567890abc" arrive here as routinely as the real thing.

    Group ids are negative in Telegram, so the sign is part of the
    number and not a separator — hence ``signed=True``. The parsing goes
    through :func:`~telegram_invite_bot.utils.numbers.parse_int_token`
    rather than a bare ``int()`` for the reason that helper documents:
    ``int()`` accepts unicode digits, whitespace and a sign that no
    Telegram id contains, and one gate that disagrees with the ``int()``
    behind it is the whole bug family ``tests/regression/
    test_unicode_digit_parsing.py`` guards. Nothing is trimmed either:
    Telegram's payload alphabet has no whitespace in it, so a payload
    carrying some was assembled by hand and gets the same ``None`` as
    any other malformed one.

    A leading ``+`` is rejected on top of that: ``parse_int_token``
    takes it, but no chat id is ever written that way, and keeping the
    payload's one canonical spelling means the round-trip through
    :func:`group_payload` is exact.
    """
    if not args or not args.startswith(GROUP_PREFIX):
        return None
    raw = args[len(GROUP_PREFIX) :]
    if raw.startswith("+"):
        return None
    chat_id = parse_int_token(raw, signed=True)
    if chat_id is None:
        return None
    # ``0`` is not a chat id, and it is what the settings column uses
    # for "no group" in rows written before it was nullable.
    return chat_id or None
