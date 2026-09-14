"""The two ``ChatPermissions`` sets every restriction path shares.

Restrictions are applied in four places — ``/mute``
(``moderation.handle_mute``), ``/unmute``
(``moderation.handle_unmute``), the join captcha
(``group_events._restrict_for_captcha``,
``group_events._lift_captcha_restriction``,
``group_events.handle_captcha_confirm``) and the antiflood auto-mute
(``antiflood.AntifloodMiddleware._mute_and_notify``) — and every one of
them used to spell its own permission literal. Named by symbol, not by
line: the previous line anchors had drifted (#1508), and a symbol
survives every edit above it. The first three were
written against Bot API <=6.4, when a single
``can_send_media_messages`` covered audios, documents, photos, videos,
video notes and voice notes. Bot API 6.5 split that flag into six, and
aiogram 3.28.2 dropped it from the model. It did NOT start raising:
``aiogram/types/base.py:11-13`` sets ``ConfigDict(extra="allow")``, so
the stale kwarg is accepted, serialised, and ignored by Telegram.

The media flags survived anyway, but for a reason unrelated to that
kwarg. ``restrict_chat_member`` leaves
``use_independent_chat_permissions`` unset, and in that mode Telegram
documents an implication: ``can_send_other_messages`` and
``can_add_web_page_previews`` imply ``can_send_messages`` and all six
media flags; ``can_send_polls`` implies ``can_send_messages``. It is a
compatibility mode built for exactly the pre-6.5 callers these literals
were. So the code read as "media granted by can_send_media_messages"
while the grant actually came from the implication — and the day
someone passes ``use_independent_chat_permissions=True`` for an
unrelated reason, media would vanish with no diagnostic at all.

The antiflood mute (#680) was the odd one out for a different reason:
it named a single field, ``ChatPermissions(can_send_messages=False)``,
and leaned on Telegram treating every omitted field as False. That is
true today, and the mute it produced was byte-for-byte the one
:data:`MUTED_PERMS` produces — but it is the same shape of implicit
dependency as the one above, and it meant the one restriction path
that is NOT a moderator's deliberate act was also the one nobody
would notice drifting. It now uses the shared constant like the rest.

Hence: no coarse flags, no reliance on implication. Both sets name
every field ChatPermissions has, and ``tests/unit/utils`` fails if a
future Bot API field is left out of either one.

:data:`UNRESTRICTED_PERMS` is all-True on purpose, not "the flags a
member usually has". Telegram documents that idiom directly — "Pass
True for all permissions to lift restrictions from a user" — and it is
what returns the user to plain ``member`` status. Granting only the
send-side flags (what /unmute and the captcha did before) leaves them
in ``ChatMemberRestricted`` forever, unable to invite or pin even where
the chat allows both to everyone.
"""

from __future__ import annotations

from aiogram.types import ChatPermissions

__all__ = ["MUTED_PERMS", "UNRESTRICTED_PERMS"]

MUTED_PERMS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_react_to_messages=False,
    can_edit_tag=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)

UNRESTRICTED_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_react_to_messages=True,
    can_edit_tag=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
    can_manage_topics=True,
)
