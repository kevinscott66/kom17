"""``/referrals`` — invited-users list, lifetime earnings, chain depth (L-37).

Legacy ``/referrals`` (bot.py:25081) renders two numbers (total invited,
total earned from referral commissions) followed by a top-20 list of
the caller's invitees with each wallet's current balance. The legacy
``get_referrals_list`` query (bot.py:9826) does ``SELECT user_id, balance
FROM economy.users WHERE referred_by=?``; commissions come from a SUM
over ``economy.transactions`` filtered by ``to_id=? AND type='referral'``.

L-37 adds the *chain / depth* surface on top of the legacy card. The data
all comes from the same ``referred_by`` self-join, so no new write-side is
involved:

* **Who invited me** — the caller's own inviter (one level up). Legacy
  never showed this; surfacing it closes the chain so a user can see both
  ends of their referral relationship.
* **Second-level depth** — how many users were invited by the caller's own
  invitees. Purely structural: legacy pays commission on level 1 only, so
  this is an informational "your network reaches N more people" line, not a
  second earnings tier.

The aggregation lives in :class:`ReferralsRepo`; this handler composes the
repo reads with the cross-engine display-name join.

Port strategy:

* Repo reads against ``economy.users`` (invitee wallets + inviter +
  second-level count) and ``economy.transactions`` (commission sum), plus a
  ``users.users`` display-name join done client-side (the two live in
  different SQLite files and we don't cross engines).
* ``html_user_mention`` for each invitee. Display name pulled from
  ``users.users.first_name``; falls back to ``f"ID{user_id}"`` when
  the row is missing (an invitee may have wiped their account or
  blocked the bot — their wallet survives, their profile row may not).
* Display cap of 20 matches legacy verbatim (bot.py:25095). Cap is
  rendering-only — the "total invited" count still spans the whole
  list returned by the inner SELECT.
* ``html.escape`` is handled inside ``html_user_mention`` for display
  names; the only other interpolated values are integers.
* Any chat type — same posture as ``/referral``. Legacy gates on
  ``ensure_user_access`` (role/ban) which the new pipeline doesn't
  model yet; same gap ``/ping`` etc. accept.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.referrals_repo import ReferralInvitee, ReferralsRepo
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.html import html_user_mention

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.services.user_service import UserService


log = logger.bind(component="handlers.referrals")

_DISPLAY_LIMIT = 20
_COIN = "🪙"


async def _fetch_names(registry: EngineRegistry, user_ids: list[int]) -> dict[int, str]:
    """Bulk first_name lookup against users.db. Empty input → empty
    dict (skips the round-trip). Cross-engine read because invitee
    wallets live in economy.db while profile data lives in users.db.
    """
    if not user_ids:
        return {}
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(User.user_id, User.first_name).where(User.user_id.in_(user_ids))
        )
        return {int(uid): (name or "") for uid, name in result.all()}


def _render(
    lang: str,
    *,
    invitees: list[ReferralInvitee],
    earned: int,
    names: dict[int, str],
    inviter_id: int | None,
    second_level: int,
) -> str:
    title = t("my_referrals_title", lang)
    invited_label = t("referrals_invited_label", lang)
    earned_label = t("referrals_earned_label", lang)
    header = (
        f"<b>{title}</b>\n\n"
        f"{invited_label}: <b>{len(invitees)}</b>\n"
        f"{earned_label}: <b>{earned}</b> {_COIN}\n"
    )
    # L-37 chain/depth lines, appended to the header block. "Who invited
    # me" closes the chain upward; the second-level count surfaces the
    # structural reach one ring further out. Both are skipped when there's
    # nothing to show so the card stays compact for organic / leaf users.
    if inviter_id is not None:
        inviter_display = (names.get(inviter_id) or "").strip() or f"ID{inviter_id}"
        inviter_mention = html_user_mention(inviter_id, inviter_display)
        header += f"{t('h_referrals_inviter_label', lang)}: {inviter_mention}\n"
    if second_level > 0:
        header += f"{t('h_referrals_second_level_label', lang)}: <b>{second_level}</b>\n"
    header += "\n"
    if not invitees:
        return header + t("referrals_empty", lang)
    lines: list[str] = []
    for idx, inv in enumerate(invitees[:_DISPLAY_LIMIT], start=1):
        # ``html_user_mention`` escapes the display string itself —
        # passing a pre-escaped name here would double-encode it (see
        # the WHY note in handlers/groupstats.py for the bug we hit
        # last time).
        display = (names.get(inv.user_id) or "").strip() or f"ID{inv.user_id}"
        mention = html_user_mention(inv.user_id, display)
        lines.append(f"{idx}. {mention} — {inv.balance} {_COIN}")
    body = "\n".join(lines)
    overflow = len(invitees) - _DISPLAY_LIMIT
    if overflow > 0:
        # Legacy renders ``... и ещё N`` only when the list overflows
        # the cap. Same suffix here keeps the card identical for users
        # with > 20 invitees. The localisation key intentionally lives
        # only when needed — we splice it inline rather than threading
        # another translation through for a once-per-card footer.
        tail = f"… +{overflow}"
        body = f"{body}\n{tail}"
    return header + body


async def handle_referrals(
    message: Message,
    user_service: UserService,
    registry: EngineRegistry,
    checkpoint: Checkpoint | None = None,
) -> None:
    user = await user_service.touch(require_from_user(message))
    # #1983: end the bookkeeping transaction here rather than hold
    # ``users.db``'s single writer slot across the reply below. See
    # :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        repo = ReferralsRepo(session)
        invitees = await repo.fetch_invitees(user.user_id)
        earned = await repo.fetch_earnings(user.user_id)
        inviter_id = await repo.fetch_inviter(user.user_id)
        second_level = await repo.count_second_level(user.user_id)
    # Name lookup batches the displayed invitees plus the inviter (so the
    # "who invited me" line renders a real name, not an ``ID<n>`` stub).
    name_ids = [inv.user_id for inv in invitees[:_DISPLAY_LIMIT]]
    if inviter_id is not None:
        name_ids.append(inviter_id)
    names = await _fetch_names(registry, name_ids)
    await message.answer(
        _render(
            user.language,
            invitees=invitees,
            earned=earned,
            names=names,
            inviter_id=inviter_id,
            second_level=second_level,
        ),
        disable_web_page_preview=True,
    )
    log.bind(
        uid=user.user_id,
        invitees=len(invitees),
        earned=earned,
        inviter=inviter_id,
        second_level=second_level,
    ).info("/referrals rendered")


def build_router(registry: EngineRegistry) -> Router:
    """Bare-form only (``magic=F.args.is_(None)``). Trailing args
    don't change behaviour today — the list is always the caller's
    own — but pinning bare leaves room for a future
    ``/referrals @other`` admin spelling without colliding here.
    """
    router = Router(name="referrals")

    async def _entry(
        message: Message,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_referrals(message, user_service, registry, checkpoint)

    router.message.register(
        _entry,
        Command(
            "referrals",
            "refs",
            "рефералы",
            "мои_рефералы",
            ignore_case=True,
            magic=F.args.is_(None),
        ),
        F.from_user,
    )
    return router
