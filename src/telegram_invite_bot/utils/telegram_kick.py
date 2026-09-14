"""Shared "kick" primitive — ban, then immediately unban (#269, #285).

Telegram has no dedicated "kick" method. The legacy bot spells a kick as
a ban followed by an unban (``bot.py:9396-9397``) and every port of it
inherited that shape, including the unsafe part: two awaits inside one
``try``, with the whole pair treated as a single all-or-nothing action.
It is not one. The ban is the call that almost always succeeds — it needs
only rights the bot has already exercised — and the unban is the one that
can lose a round trip. When it does, a "kick" has silently become a
permanent ban, and nothing anywhere in this codebase ever lifts it.

#2031 adds a fourth question, which is really the first one: *is there
already a ban here that is not ours?* The pair is a removal, but its
second half is an **unban**, and an unban run over a standing ban lifts
it. Nothing upstream noticed, because every guard on the way in asks
about the target's rank or adminship and none asks about their
membership: a banned user reports ``kicked``, which is not an admin
status, so ``/kick <id of a banned user>`` went straight through and
came out the far side as an unban — performed by a moderator whose rank
grants ``can_kick`` and explicitly withholds ``can_ban``
(``core/ranks.py:311-312``), logged as ``action="kick"``, and leaving
none of the state a real ``/unban`` clears. The captcha timeout
(``handlers/group_events.py:629``) reached the same end by a different
road: ban a joiner mid-captcha and the timer lifts the ban for them.

So the pair now opens with a membership probe and refuses outright when
the target is already banned. It costs one round trip per kick, which
is the price of knowing whose ban we are about to lift.

Defences, in descending order of how much they actually buy:

1. **The ban carries an ``until_date``**, so it is a ban Telegram lifts
   by itself. The worst case degrades from "banned forever" to "cannot
   rejoin for a minute". This is a deliberate divergence from the legacy
   unbounded ban at ``bot.py:9396``, and it is the only one of the three
   that still holds if the process dies between the two calls. Telegram
   reads an ``until_date`` under 30 seconds — or over 366 days — as
   *permanent*, so :data:`_BAN_SECONDS` has to stay inside that band.
   The expiry is also a supergroup feature: in a legacy basic group it
   is ignored and the ban is permanent, which is what defence 2 is for.
2. **The unban is retried** with ``only_if_banned=True``, so a retry
   landing after a slow first attempt cannot lift a ban the admins put
   there on purpose.
3. **The outcome is reported honestly.** A bool cannot separate "the user
   is gone" from "the user is gone and still banned", and a caller that
   reports plain success for the second case is telling an admin nothing
   needs fixing. :attr:`KickOutcome.ALREADY_BANNED` is the same rule
   applied to the case above: "I did nothing, and here is why" is a
   different sentence from "it failed".
4. **The ban we place is one we know is ours** (#2031, the probe). Unlike
   the retry guard, this one holds against a ban that predates the call
   — ``only_if_banned=True`` cannot, because by the time it is evaluated
   the bot itself is the banner.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING, Final

from aiogram.enums import ChatMemberStatus
from loguru import logger

if TYPE_CHECKING:
    from aiogram import Bot

log = logger.bind(component="utils.telegram_kick")

#: Ban window. Long enough that Telegram honours it as temporary (the
#: cutoff is 30 s), short enough that a failed unban is a nuisance
#: rather than an exclusion.
_BAN_SECONDS: Final[int] = 60
_UNBAN_ATTEMPTS: Final[int] = 3
_UNBAN_BACKOFF_SEC: Final[float] = 0.5


class KickOutcome(Enum):
    """What a :func:`kick_member` call actually left behind."""

    #: Ban and unban both landed — the user is out and free to rejoin.
    REMOVED = "removed"
    #: The ban landed, every unban attempt did not. The user is out but
    #: still banned until :data:`_BAN_SECONDS` elapses.
    LEFT_BANNED = "left_banned"
    #: The ban itself failed. Nothing changed; the user is still in.
    FAILED = "failed"
    #: The target is already banned, so the pair was not run at all
    #: (#2031). Distinct from :attr:`FAILED` because nothing went
    #: wrong and nothing should be retried: the user is out, and the
    #: only thing this call could have added is lifting somebody
    #: else's ban.
    ALREADY_BANNED = "already_banned"


async def kick_member(
    bot: Bot,
    chat_id: int,
    user_id: int,
    *,
    attempts: int = _UNBAN_ATTEMPTS,
    backoff_sec: float = _UNBAN_BACKOFF_SEC,
) -> KickOutcome:
    """Remove one member without leaving them banned. Never raises.

    Callers must branch on the result: :attr:`KickOutcome.FAILED` means
    the target is untouched and any compensating state the caller took
    (a captcha mute, say) is still in force.
    :attr:`KickOutcome.ALREADY_BANNED` also means untouched, but for a
    reason no retry improves.

    The probe's failure is spelled ``FAILED`` rather than waved through,
    and that is a deliberate widening of what can fail: a transient
    error on this call now refuses a kick the ban might have completed.
    It is the right direction. Proceeding on an unreadable membership
    means running the unban half without knowing whose ban it would
    lift, which is the whole defect — and the caller already handles
    ``FAILED`` as "nothing happened, ask again".
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — unreadable membership, see above
        log.warning(
            "kick probe failed (chat={c}, user={u}): {e!r}",
            c=chat_id,
            u=user_id,
            e=exc,
        )
        return KickOutcome.FAILED
    if member.status == ChatMemberStatus.KICKED:
        log.bind(chat_id=chat_id, user=user_id).info(
            "kick refused — the target is already banned, and removing them "
            "again would only lift that ban"
        )
        return KickOutcome.ALREADY_BANNED

    try:
        await bot.ban_chat_member(chat_id, user_id, until_date=timedelta(seconds=_BAN_SECONDS))
    except Exception as exc:  # noqa: BLE001 — no rights, or the API is down
        log.warning(
            "kick ban failed (chat={c}, user={u}): {e!r}",
            c=chat_id,
            u=user_id,
            e=exc,
        )
        return KickOutcome.FAILED

    delay = backoff_sec
    for attempt in range(1, attempts + 1):
        try:
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        except Exception as exc:  # noqa: BLE001 — retried, then reported
            log.warning(
                "kick unban attempt {n}/{total} failed (chat={c}, user={u}): {e!r}",
                n=attempt,
                total=attempts,
                c=chat_id,
                u=user_id,
                e=exc,
            )
        else:
            return KickOutcome.REMOVED
        if attempt < attempts:
            await asyncio.sleep(delay)
            delay *= 2

    log.bind(chat_id=chat_id, user=user_id).error(
        "USER LEFT BANNED — the kick removed them but every unban attempt "
        "failed; the ban expires on its own in {sec}s",
        sec=_BAN_SECONDS,
    )
    return KickOutcome.LEFT_BANNED
