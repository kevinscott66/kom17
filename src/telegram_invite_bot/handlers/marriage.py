"""/marry, /marry_accept, /marry_decline, /divorce, /breakup — Stage T-019.

Ports the marriage-proposal flow and breakup command from legacy
``bot.py`` to the new aiogram pipeline.  All writes go through
:class:`~telegram_invite_bot.repositories.bonds_repo.BondsWriteRepo`;
the handler is a thin parse-and-delegate shell.

Commands claimed (group-only):
  * ``/marry``         (aliases ``брак``, ``жениться``)   — propose by reply
  * ``/marry_accept``  (alias ``принять_брак``)            — accept latest proposal
  * ``/marry_decline`` (alias ``отклонить_брак``)          — decline latest proposal
  * Inline callbacks  ``marry_accept_<id>`` / ``marry_decline_<id>``
  * ``/divorce``       (alias ``развод``)                  — unilateral soft-divorce
  * ``/breakup``       (alias ``расстаться``)              — end a relationship
  * ``/relationship``  (aliases ``rel``, ``отношения``, ``в_отношениях``)
                       — propose a relationship by reply, or list own bonds
  * Inline callbacks  ``rel_accept_<id>`` / ``rel_decline_<id>``

A-02 note: ``/marry`` requires a level-6 relationship, and a relationship
row is *only* created via ``/relationship`` propose→accept. With the
legacy bridge removed, leaving ``/relationship`` unported bricked the
whole marriage feature, so it lives here now.

L-03..L-07 (Wave 1-A): the second-tier marriage command surface is now
ported here:
  * ``/marriage`` (alias ``my_marriage``, ``брак_статус``) — own status card
  * ``/marry_top_on`` / ``/marry_top_off`` — rating-inclusion toggle
  * ``/marry_extend <days>`` — paid renewal (10 coins/day, atomic escrow)
  * ``/marry_auto_divorce <off|one|two>`` — auto-divorce mode setting
  * ``/marry_other`` (reply/mention) — another user's status card

OUT OF SCOPE: the background auto-divorce sweep (only the setting is
ported, not the periodic enforcement) and the relationship *activity*
subsystem (``rel_activity_menu_*`` / ``rel_history_*``, RP commands).

Legacy ``/marry`` already gated on group-only; the new handlers mirror
that via :func:`~telegram_invite_bot.handlers.chat_scope.with_chat_type_refusal`,
applied to the finished router at the bottom of this module. It
re-registers every command word this module owns under a private-chat
filter and answers the "только в группе" refusal there, so a DM gets
a reply rather than silence.

HTML parse_mode note: the new pipeline defaults to HTML (``app.py``).
User-supplied first_names are escaped via :func:`html_user_mention`
before embedding — same contract as ``handlers/relations.py``. A missing
first_name falls back to the localized label resolved by
:mod:`telegram_invite_bot.utils.names`; this module used to hardcode the
Russian ``Пользователь`` in fourteen places, which an English-speaking
user read inside an otherwise English card.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.repositories.bonds_repo import ProposalAlreadyResolvedError
from telegram_invite_bot.utils.aiogram import edit_card, require_from_user
from telegram_invite_bot.utils.bonds import (
    format_db_date,
    format_duration,
    marriage_category,
    marriage_level_name,
    marriage_xp_to_level,
)
from telegram_invite_bot.utils.html import html_user_mention
from telegram_invite_bot.utils.names import display_name, mention
from telegram_invite_bot.utils.numbers import is_int_token, parse_int_token

log = logger.bind(component="handlers.marriage")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import CallbackQuery

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo
    from telegram_invite_bot.services.economy_service import EconomyService

# L-05: paid renewal cost per day (mirrors bot.py:22723
# ``MARRIAGE_EXTEND_COST_PER_DAY``).
_MARRIAGE_EXTEND_COST_PER_DAY: int = 10
# L-05: legacy clamps the requested days into [1, 365] (bot.py:22779).
_MARRIAGE_EXTEND_MAX_DAYS: int = 365
# L-06: accepted auto-divorce modes. Mirrors the token table at
# bot.py:22810-22818 (ru/en/digit aliases collapse to these canonicals).
_AUTO_DIVORCE_MODES: dict[str, str] = {
    "один": "one",
    "one": "one",
    "1": "one",
    "два": "two",
    "two": "two",
    "2": "two",
    "выключить": "off",
    "off": "off",
    "отключить": "off",
}
# L-05 coin sign (mirrors handlers/couple_activities._COIN_SIGN).
_COIN_SIGN = "🪙"

# Ceiling on the no-reply ``/relationship`` list. Each entry is two text
# lines plus a button row, so ~45 of them would exceed Telegram's 4096
# limit and the reply would fail outright.
_REL_LIST_MAX: int = 15

# Relationship-level constants (mirrors bot.py:22097-22099)
_MARRIAGE_MIN_REL_LEVEL: int = 6
_RELATIONSHIP_LEVEL_XP: tuple[int, ...] = (
    0,
    150,
    1500,
    5000,
    10000,
    30000,
    60000,
    150000,
    300000,
    1_000_000,
    3_000_000,
    10_000_000,
)
_RELATIONSHIP_LEVEL_NAMES: dict[int, tuple[str, str]] = {
    1: ("Знакомые", "Acquaintances"),
    2: ("Дружеские отношения", "Friendly"),
    3: ("Тёплые отношения", "Warm"),
    4: ("Симпатия", "Crush"),
    5: ("Флирт", "Flirting"),
    6: ("Ухаживания", "Courtship"),
    7: ("Отношения", "Relationship"),
    8: ("Серьёзные отношения", "Serious"),
    9: ("Любовь", "Love"),
    10: ("Великая любовь", "Great love"),
    11: ("Любовь всей жизни", "Love of a lifetime"),
}


def _rel_level_name(level: int, lang: str) -> str:
    names = _RELATIONSHIP_LEVEL_NAMES.get(level, ("Отношения", "Relationship"))
    return names[0] if lang == "ru" else names[1]


def _rel_xp_threshold(level: int) -> int:
    if level < len(_RELATIONSHIP_LEVEL_XP):
        return _RELATIONSHIP_LEVEL_XP[level]
    return _RELATIONSHIP_LEVEL_XP[-1]


# ---------------------------------------------------------------------------
# /marry  — propose marriage by replying to a user's message
# ---------------------------------------------------------------------------


async def handle_marry(
    message: Message,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
) -> None:
    """Propose marriage to the user whose message was replied to."""
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply(t("h_marry_reply", lang))
        return

    target = message.reply_to_message.from_user
    target_id = target.id

    if target_id == user_id:
        await message.reply(t("h_marry_self", lang))
        return
    if target.is_bot:
        await message.reply(t("h_marry_bot", lang))
        return

    # Caller must not already be married
    if await bonds_write_repo.get_marriage(chat_id, user_id) is not None:
        await message.reply(t("h_marry_already_married", lang))
        return
    # Target must not already be married
    if await bonds_write_repo.get_marriage(chat_id, target_id) is not None:
        await message.reply(t("h_marry_target_married", lang))
        return

    # Relationship-level gate
    rel = await bonds_write_repo.get_relationship(chat_id, user_id, target_id)
    rel_level = bonds_write_repo._rel_xp_to_level(rel.experience or 0) if rel is not None else 0
    if rel_level < _MARRIAGE_MIN_REL_LEVEL:
        level_name = _rel_level_name(_MARRIAGE_MIN_REL_LEVEL, lang)
        xp_needed = _rel_xp_threshold(_MARRIAGE_MIN_REL_LEVEL)
        await message.reply(
            t(
                "h_marry_need_rel_level",
                lang,
                level=_MARRIAGE_MIN_REL_LEVEL,
                level_name=level_name,
                xp_required=xp_needed,
            )
        )
        return

    prop = await bonds_write_repo.propose_marriage(chat_id, user_id, target_id)

    # Escaped here rather than mentioned: the proposal copy names both
    # parties in prose, not as tg:// links.
    from_name = html.escape(display_name(tg_user.first_name, lang))
    target_name = html.escape(display_name(target.first_name, lang))

    text = (
        t("h_marry_proposal", lang, from_name=from_name, target_name=target_name)
        + "\n\n"
        + t("h_marry_proposal_hint", lang)
    )

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_marry_accept_btn", lang),
                    callback_data=f"marry_accept_{prop.id}",
                ),
                InlineKeyboardButton(
                    text=t("h_marry_decline_btn", lang),
                    callback_data=f"marry_decline_{prop.id}",
                ),
            ]
        ]
    )

    # M-G-7: the proposal row is already inserted (line above). If the
    # outbound reply fails, the row would otherwise sit pending forever
    # with no card to resolve it — and because
    # ``get_latest_proposal_for`` returns only the NEWEST pending
    # proposal addressed to a user, that invisible row also shadows the
    # ``/marry_decline`` slash form for every proposal made after it.
    #
    # #1741: an earlier version of this comment claimed the row acts as
    # an interlock ("both seats can't issue another /marry until
    # someone declines"). There is no such interlock — ``propose_*`` is
    # a plain INSERT with no dedupe and no uniqueness constraint, so a
    # sender can stack proposals without limit. That is a separate open
    # ticket; the deletion below is justified by the shadowing above,
    # not by an interlock.
    #
    # The except below catches exactly the two REPORTABLE failures:
    # the target deleted the message we're replying to (BadRequest),
    # or the group kicked the bot (Forbidden). Those two delete the
    # row here and answer with a localised ``h_marry_send_failed``.
    # Anything else — a transient network error, say — is NOT caught
    # here: it propagates, the session middleware rolls the whole
    # transaction back with the insert inside it, and the errors
    # router renders its generic toast. Either way the row does not
    # survive a failed send; only the reply the caller sees differs.
    # Widening the tuple to ``TelegramAPIError`` would unify that
    # reply, and is deliberately not done: the two named here are the
    # ones the user can actually act on.
    try:
        await message.reply_to_message.reply(text, reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        await bonds_write_repo.delete_proposal(prop.id)
        log.bind(
            chat_id=chat_id, from_id=user_id, to_id=target_id, prop_id=prop.id, err=str(exc)
        ).warning("/marry proposal send failed; rolled back proposal row")
        await message.reply(t("h_marry_send_failed", lang))
        return
    log.bind(chat_id=chat_id, from_id=user_id, to_id=target_id, prop_id=prop.id).info(
        "/marry proposal sent"
    )


# ---------------------------------------------------------------------------
# /marry_accept and /marry_decline  — slash-command form
# ---------------------------------------------------------------------------


async def handle_marry_accept_decline(
    message: Message,
    command: CommandObject,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Respond to the latest pending marriage proposal via slash command.

    R-FIX-012: branch on ``command.command`` (aiogram strips any
    ``@BotName`` suffix) instead of re-parsing ``message.text``. The
    previous parser collapsed ``/marry_accept@MyBot`` to a decline
    because the literal ``"/marry_accept"`` token failed to match.
    """
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    prop = await bonds_write_repo.get_latest_proposal_for(chat_id, user_id)
    if prop is None:
        await message.reply(t("h_marry_no_proposal", lang))
        return

    # aiogram's CommandObject already strips '/' and any '@BotName'
    # suffix. ``command.command`` is the bare verb (lowercased by our
    # ``Command(..., ignore_case=True)`` registration).
    verb = (command.command or "").lower()
    is_accept = verb in {"marry_accept", "m_accept", "принять_брак"}

    if is_accept:
        await _do_accept(message, bonds_write_repo, prop, lang, checkpoint)
    else:
        try:
            await bonds_write_repo.decline_proposal(prop.id)
        except ProposalAlreadyResolvedError:
            await message.reply(t("h_marry_already_resolved", lang))
            return
        # #1860: ``SessionMiddleware`` keeps one users.db session per
        # update, commits it only after the handler returns, and rolls
        # it back on any raise — while ``db/engines.py`` promoted it to
        # ``BEGIN IMMEDIATE`` on the guarded UPDATE above. Left alone
        # the lock spans the reply, and a reply that fails (kicked bot,
        # deleted thread) silently resurrects a proposal the user had
        # already turned down. Commit the resolution first; a lost card
        # then costs only the card.
        if checkpoint is not None:
            await checkpoint()
        await message.reply(t("h_marry_declined", lang))


# ---------------------------------------------------------------------------
# Inline callbacks  marry_accept_<id> / marry_decline_<id>
# ---------------------------------------------------------------------------


async def callback_marry_accept(
    call: CallbackQuery,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    assert call.from_user is not None
    assert call.data is not None
    assert call.message is not None

    # #1692: ``str.split`` always yields at least one element, so the
    # IndexError arm never fired, and the ValueError arm caught the
    # wrong half of the problem — a twenty-digit tail parsed cleanly
    # and raised OverflowError on the SELECT below, one layer past the
    # handler that had already declared the payload usable. The filter
    # bounds the character class, not the length.
    prop_id = parse_int_token(call.data.split("_", 2)[-1])
    if prop_id is None:
        await call.answer("❌")
        return

    chat_id = call.message.chat.id
    prop = await bonds_write_repo.get_proposal_by_id(prop_id, chat_id)
    if prop is None:
        await call.answer(t("h_marry_no_proposal", lang)[:200])
        return
    if prop.to_id != call.from_user.id:
        await call.answer(t("h_marry_not_for_you", lang)[:200])
        return

    try:
        ok, err_key = await bonds_write_repo.accept_proposal(prop)
    except ProposalAlreadyResolvedError:
        # A parallel /marry_accept (slash or button) won the claim
        # first — surface a friendly toast and don't write a second
        # marriage row (R-FIX-010).
        await call.answer(t("h_marry_already_resolved", lang)[:200])
        return
    if not ok:
        # Map legacy error keys to new h_ i18n keys
        key_map = {
            "marry_need_rel_level6_accept": "h_marry_need_rel_level_accept",
            "marry_already_married_accept": "h_marry_already_married_accept",
            "marry_save_error": "h_marry_save_error",
        }
        msg_key = key_map.get(err_key, err_key)
        if err_key == "marry_need_rel_level6_accept":
            level_name = _rel_level_name(_MARRIAGE_MIN_REL_LEVEL, lang)
            xp_needed = _rel_xp_threshold(_MARRIAGE_MIN_REL_LEVEL)
            text = t(
                msg_key,
                lang,
                level=_MARRIAGE_MIN_REL_LEVEL,
                level_name=level_name,
                xp_required=xp_needed,
            )
        else:
            text = t(msg_key, lang)
        await call.answer(text[:200])
        return

    # #1860: the bond exists now, so end the write transaction before
    # the two network calls below. ``edit_card`` already swallows a
    # stale-card error, but ``call.answer`` does not — and an update
    # that raises is rolled back whole, un-marrying a couple that was
    # never told anything. The ``edit_card`` note further down promises
    # exactly this durability; the checkpoint is what makes it true.
    #
    # The refusal arms above are deliberately NOT checkpointed: they
    # consumed the proposal on purpose, and committing that consumption
    # on its own would make an unreported failure permanent.
    if checkpoint is not None:
        await checkpoint()
    from_mention = mention(prop.from_id, await bonds_write_repo.get_first_name(prop.from_id), lang)
    to_mention = mention(prop.to_id, call.from_user.first_name, lang)
    text = t("h_marry_done", lang, from_mention=from_mention, to_mention=to_mention)
    # ``call.message`` is ``Message | InaccessibleMessage`` per aiogram
    # 3.x — InaccessibleMessage lacks edit_text. Narrow before mutating;
    # if the original card got too old / was deleted, we still ACK the
    # callback so the user's button doesn't spin forever. ``edit_card``
    # is what makes that promise true: the proposal is already accepted
    # in the DB by this point, so a card past its edit window must not
    # raise into the error router and tell the newlyweds the wedding
    # failed.
    if isinstance(call.message, Message):
        await edit_card(call.message, text)
    await call.answer("💒")
    log.bind(chat_id=chat_id, prop_id=prop_id).info("marriage accepted via callback")


async def callback_marry_decline(
    call: CallbackQuery,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    assert call.from_user is not None
    assert call.data is not None
    assert call.message is not None

    # #1692: see :func:`callback_marry_accept`.
    prop_id = parse_int_token(call.data.split("_", 2)[-1])
    if prop_id is None:
        await call.answer("❌")
        return

    chat_id = call.message.chat.id
    prop = await bonds_write_repo.get_proposal_by_id(prop_id, chat_id)
    if prop is None:
        await call.answer(t("h_marry_no_proposal", lang)[:200])
        return
    if prop.to_id != call.from_user.id:
        await call.answer(t("h_marry_not_for_you", lang)[:200])
        return

    try:
        await bonds_write_repo.decline_proposal(prop.id)
    except ProposalAlreadyResolvedError:
        await call.answer(t("h_marry_already_resolved", lang)[:200])
        return
    # #1860: as in the slash form — the decline is persisted, so it must
    # not ride on the card being redrawn or the toast being delivered.
    if checkpoint is not None:
        await checkpoint()
    text = t("h_marry_declined", lang)
    if isinstance(call.message, Message):
        # Decline is already persisted — a card past its edit window
        # must not raise into the error router.
        await edit_card(call.message, text)
    await call.answer("💔")


# ---------------------------------------------------------------------------
# /divorce  — unilateral soft divorce
# ---------------------------------------------------------------------------


async def handle_divorce(
    message: Message,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    # #1860: no checkpoint on the refusal arm — ``soft_divorce`` returns
    # ``False`` straight off a SELECT that found no marriage, so nothing
    # write-headed ran and no lock exists to drop.
    if not await bonds_write_repo.soft_divorce(chat_id, user_id):
        await message.reply(t("h_marry_single", lang))
        return

    # #1860: the divorce is written. Commit before the reply so a failed
    # reply cannot put the couple back together.
    if checkpoint is not None:
        await checkpoint()
    await message.reply(t("h_marry_divorced", lang))
    log.bind(chat_id=chat_id, user_id=user_id).info("/divorce executed")


# ---------------------------------------------------------------------------
# /breakup  — unilateral relationship termination
# ---------------------------------------------------------------------------


async def handle_breakup(
    message: Message,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply(t("h_breakup_reply", lang))
        return

    partner_id = message.reply_to_message.from_user.id
    if partner_id == user_id:
        await message.reply(t("h_breakup_self", lang))
        return

    # #1860: no checkpoint here either — ``terminate_relationship`` bails
    # on the same read-only miss as /divorce above (and with no row there
    # is no lazy XP decay to flush).
    if not await bonds_write_repo.terminate_relationship(chat_id, user_id, partner_id):
        await message.reply(t("h_breakup_not_together", lang))
        return

    # #1860: the relationship is terminated; do not make that depend on
    # the reply landing.
    if checkpoint is not None:
        await checkpoint()
    await message.reply(t("h_breakup_done", lang))
    log.bind(chat_id=chat_id, user_id=user_id, partner_id=partner_id).info("/breakup executed")


# ---------------------------------------------------------------------------
# /relationship  — propose / list relationships  (A-02 unbrick)
# ---------------------------------------------------------------------------
#
# Marriage requires a level-6 relationship (``_MARRIAGE_MIN_REL_LEVEL``),
# and the *only* way to create a relationship row is this propose→accept
# flow. With the legacy bridge gone, an unported ``/relationship`` left
# ``/marry`` permanently unreachable for any couple without pre-existing
# legacy data. This restores the propose + accept/decline path so the XP
# curve — and therefore marriage — is reachable again.
#
# The relationship *activity* subsystem has since landed too, in its own
# modules: :mod:`~handlers.couple_activities` (the ``cpl_menu`` /
# ``cpl_hist`` callback factories, legacy ``rel_activity_menu_*`` /
# ``rel_history_*``) and :mod:`~handlers.rp` (the RP verbs). So the
# no-reply status view below is NOT plain text — every listed
# relationship carries an activities + history button pair, and both
# callbacks have a live consumer.


async def handle_relationship(
    message: Message,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
) -> None:
    """Propose a relationship (by reply) or list the caller's own bonds."""
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    # Anonymous senders can't be tied to a user_id (mirrors legacy
    # ``daily_anonymous_denied`` guard at bot.py:23150). #303: this is
    # the check that does the real work for both chat-backed shapes.
    # Telegram gives an anonymous admin a ``from_user`` of
    # ``GroupAnonymousBot`` and a member posting as their channel a fake
    # one, so ``F.from_user`` on the registration lets both through and
    # only ``sender_chat`` tells them apart from a person.
    if message.sender_chat is not None:
        await message.reply(t("h_rel_anonymous_denied", lang))
        return

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        target_id = target.id

        if target_id == user_id:
            await message.reply(t("h_rel_self", lang))
            return
        if target.is_bot:
            await message.reply(t("h_rel_bot", lang))
            return
        # ``get_relationship`` normalises the pair, so one call covers
        # both directions (legacy probes both explicitly).
        if await bonds_write_repo.get_relationship(chat_id, user_id, target_id):
            await message.reply(t("h_rel_already_with_this", lang))
            return

        prop = await bonds_write_repo.propose_relationship(chat_id, user_id, target_id)

        from_name = html.escape(display_name(tg_user.first_name, lang))
        target_name = html.escape(display_name(target.first_name, lang))
        text = t("h_rel_proposal", lang, from_name=from_name, target_name=target_name)

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=t("h_rel_accept_btn", lang),
                        callback_data=f"rel_accept_{prop.id}",
                    ),
                    InlineKeyboardButton(
                        text=t("h_rel_decline_btn", lang),
                        callback_data=f"rel_decline_{prop.id}",
                    ),
                ]
            ]
        )
        # Same M-G-7 rollback contract as /marry above, including the
        # split between the two reportable failures caught here and
        # everything else being rolled back by the session middleware.
        try:
            await message.reply_to_message.reply(text, reply_markup=markup)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            await bonds_write_repo.delete_relationship_proposal(prop.id)
            log.bind(
                chat_id=chat_id,
                from_id=user_id,
                to_id=target_id,
                prop_id=prop.id,
                err=str(exc),
            ).warning("/relationship proposal send failed; rolled back row")
            await message.reply(t("h_rel_save_error", lang))
            return
        log.bind(chat_id=chat_id, from_id=user_id, to_id=target_id, prop_id=prop.id).info(
            "/relationship proposal sent"
        )
        return

    # No reply → list the caller's relationships. Each row carries an
    # activities + history button pair, collected into ``kb_rows`` below
    # and consumed by :mod:`~handlers.couple_activities`.
    rels = await bonds_write_repo.list_relationships_for(chat_id, user_id)
    if not rels:
        await message.reply(t("h_rel_single", lang))
        return

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from telegram_invite_bot.keyboards.builders.couple_activities import (
        CoupleHistory,
        CoupleMenu,
    )

    lines = [t("h_rel_status_header", lang)]
    # ``list_relationships_for`` has no LIMIT and every row costs two
    # text lines AND a button row, so an active chat member walks the
    # message into Telegram's 4096 ceiling (~45 bonds) and gets a silent
    # 400. Truncating rather than paginating: the buttons belong to the
    # rows, and a continuation message cannot carry them. The repo
    # orders by DECAYED experience DESC (#477), so what survives is the
    # strongest bonds as they stand today — the ones the caller came to
    # look at — and not the ones that merely peaked highest.
    shown = rels[:_REL_LIST_MAX]
    hidden = len(rels) - len(shown)
    # L-35: one "activities + history" button row per relationship, wired
    # to the EXISTING couple-activity callbacks (execution lives in
    # handlers/couple_activities — not duplicated here). partner_id pins
    # which pair each button acts on.
    kb_rows: list[list[InlineKeyboardButton]] = []
    for rel in shown:
        partner_id = rel.user2_id if rel.user1_id == user_id else rel.user1_id
        exp = rel.experience or 0
        level = bonds_write_repo._rel_xp_to_level(exp)
        level_name = _rel_level_name(level, lang) if level > 0 else "—"
        # Resolved once: the same string goes into the HTML mention (which
        # escapes it) and into the button caption (which must NOT be
        # escaped — button text is plain, and ``&amp;`` would show).
        partner_name = display_name(await bonds_write_repo.get_first_name(partner_id), lang)
        partner_mention = html_user_mention(partner_id, partner_name)
        date_str = format_db_date(rel.created_at)
        lines.append(
            t(
                "h_rel_status_line",
                lang,
                partner=partner_mention,
                level=level,
                level_name=level_name,
                exp=exp,
                date=date_str,
            )
        )
        # RR-5 #47: restore the "to next level" progress line (key existed
        # but was no longer rendered). Omitted at the max level.
        _rel_xp = bonds_write_repo.RELATIONSHIP_LEVEL_XP
        if level + 1 < len(_rel_xp):
            lines.append("  " + t("rel_status_xp_next", lang, xp_left=_rel_xp[level + 1] - exp))
        # RR-5 #48: surface the relationship tier on the activities button
        # (legacy showed the level name on the button itself).
        _btn_text = t("h_rel_card_activities_btn", lang, partner=partner_name)
        if level > 0:
            _btn_text += f" · {level_name}"
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=_btn_text,
                    callback_data=CoupleMenu(
                        kind="rel", partner_id=partner_id, owner_id=user_id
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=t("h_couple_history_btn", lang),
                    callback_data=CoupleHistory(
                        kind="rel", partner_id=partner_id, owner_id=user_id
                    ).pack(),
                ),
            ]
        )
    if hidden > 0:
        lines.append(t("h_rel_status_more", lang, count=hidden))
    await message.reply(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


async def callback_rel_accept(
    call: CallbackQuery,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    assert call.from_user is not None
    assert call.data is not None
    assert call.message is not None

    # #1692: see :func:`callback_marry_accept`.
    prop_id = parse_int_token(call.data.split("_", 2)[-1])
    if prop_id is None:
        await call.answer("❌")
        return

    chat_id = call.message.chat.id
    prop = await bonds_write_repo.get_relationship_proposal_by_id(prop_id, chat_id)
    if prop is None or prop.to_id != call.from_user.id:
        await call.answer(t("h_rel_not_for_you", lang)[:200])
        return

    try:
        ok = await bonds_write_repo.accept_relationship_proposal(prop)
    except ProposalAlreadyResolvedError:
        await call.answer(t("h_rel_already_resolved", lang)[:200])
        return
    if not ok:
        await call.answer(t("h_rel_save_error", lang)[:200])
        return

    # #1860: the bond is written — commit it before the card work, for
    # the same reason as the marriage twin above.
    if checkpoint is not None:
        await checkpoint()
    from_mention = mention(prop.from_id, await bonds_write_repo.get_first_name(prop.from_id), lang)
    to_mention = mention(prop.to_id, call.from_user.first_name, lang)
    text = t("h_rel_done", lang, from_mention=from_mention, to_mention=to_mention)
    if isinstance(call.message, Message):
        # Same as the marriage path above — the bond is committed before
        # the card is redrawn, so a failed redraw is cosmetic.
        await edit_card(call.message, text)
    await call.answer("💕")
    log.bind(chat_id=chat_id, prop_id=prop_id).info("relationship accepted via callback")


async def callback_rel_decline(
    call: CallbackQuery,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    assert call.from_user is not None
    assert call.data is not None
    assert call.message is not None

    # #1692: see :func:`callback_marry_accept`.
    prop_id = parse_int_token(call.data.split("_", 2)[-1])
    if prop_id is None:
        await call.answer("❌")
        return

    chat_id = call.message.chat.id
    prop = await bonds_write_repo.get_relationship_proposal_by_id(prop_id, chat_id)
    if prop is None or prop.to_id != call.from_user.id:
        await call.answer(t("h_rel_not_for_you", lang)[:200])
        return

    try:
        await bonds_write_repo.decline_relationship_proposal(prop.id)
    except ProposalAlreadyResolvedError:
        await call.answer(t("h_rel_already_resolved", lang)[:200])
        return
    # #1860: the decline is persisted — see the marriage twin above.
    if checkpoint is not None:
        await checkpoint()
    if isinstance(call.message, Message):
        # Same as the marriage decline above — the write already landed.
        await edit_card(call.message, t("h_rel_declined", lang))
    await call.answer("💔")


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


async def _do_accept(
    message: Message,
    repo: BondsWriteRepo,
    prop: object,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Shared accept logic for the slash-command form."""
    from telegram_invite_bot.db.models.users import MarriageProposal  # local import

    assert isinstance(prop, MarriageProposal)
    tg_user = require_from_user(message)
    if prop.to_id != tg_user.id:
        await message.reply(t("h_marry_not_for_you", lang))
        return

    try:
        ok, err_key = await repo.accept_proposal(prop)
    except ProposalAlreadyResolvedError:
        await message.reply(t("h_marry_already_resolved", lang))
        return
    if not ok:
        key_map = {
            "marry_need_rel_level6_accept": "h_marry_need_rel_level_accept",
            "marry_already_married_accept": "h_marry_already_married_accept",
            "marry_save_error": "h_marry_save_error",
        }
        msg_key = key_map.get(err_key, err_key)
        if err_key == "marry_need_rel_level6_accept":
            level_name = _rel_level_name(_MARRIAGE_MIN_REL_LEVEL, lang)
            xp_needed = _rel_xp_threshold(_MARRIAGE_MIN_REL_LEVEL)
            await message.reply(
                t(
                    msg_key,
                    lang,
                    level=_MARRIAGE_MIN_REL_LEVEL,
                    level_name=level_name,
                    xp_required=xp_needed,
                )
            )
        else:
            await message.reply(t(msg_key, lang))
        return

    # The proposer is not the sender here, so their name comes from the
    # DB — the callback twin above already did this; the slash-command
    # form used to render the fallback label unconditionally, naming a
    # stranger at their own wedding.
    # #1860: the marriage row is in place; the slash form needs the same
    # durability the callback twin gets. ``checkpoint`` is threaded in
    # from :func:`handle_marry_accept_decline` rather than injected,
    # because this helper is not a handler.
    if checkpoint is not None:
        await checkpoint()
    from_mention = mention(prop.from_id, await repo.get_first_name(prop.from_id), lang)
    to_mention = mention(
        prop.to_id,
        tg_user.first_name,
        lang,
    )
    await message.reply(t("h_marry_done", lang, from_mention=from_mention, to_mention=to_mention))


# ---------------------------------------------------------------------------
# L-03  /marriage — own marriage status card
# ---------------------------------------------------------------------------
#
# L-03..L-07 (Wave 1-A): the second-tier marriage command surface. The
# Marriage model + BondsWriteRepo write methods (set_marriage_in_top,
# extend_marriage, set_marriage_auto_divorce) were landed ahead of these
# handlers; this block is the parse-and-delegate shell that wires them to
# the command surface.
#
# The marriage *activity* / *history* inline buttons that legacy
# ``cmd_marriage`` attaches (``marriage_activity_menu`` /
# ``marriage_history``) ARE ported: the status card built by
# :func:`handle_marriage` below hangs a ``CoupleMenu`` / ``CoupleHistory``
# pair on itself, and both are consumed by the couple-activity subsystem
# in :mod:`~handlers.couple_activities`.


async def _partner_id_of(marriage: object, user_id: int) -> int:
    """Return the other spouse's id given a Marriage row and one party."""
    from telegram_invite_bot.db.models.users import Marriage  # local import

    assert isinstance(marriage, Marriage)
    return marriage.user2_id if marriage.user1_id == user_id else marriage.user1_id


async def handle_marriage(
    message: Message,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
) -> None:
    """Show the caller's own marriage status card (partner, date, level/XP).

    Mirrors ``cmd_marriage`` at ``bot.py:22663``. The marriage-level
    arithmetic reuses the pure formatters in :mod:`utils.bonds`.
    """
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    marriage = await bonds_write_repo.get_marriage(chat_id, user_id)
    if marriage is None:
        await message.reply(t("h_marry_single", lang))
        return

    partner_id = await _partner_id_of(marriage, user_id)
    partner_mention = mention(partner_id, await bonds_write_repo.get_first_name(partner_id), lang)

    date_str = format_db_date(marriage.created_at)
    duration = format_duration(marriage.created_at, lang=lang)
    extra_days = marriage.duration_days or 0
    category = marriage_category(marriage.created_at, extra_days, lang=lang)
    exp = marriage.experience or 0
    level = marriage_xp_to_level(exp)
    level_name = marriage_level_name(level, lang)

    text = t("h_marriage_status", lang, partner=partner_mention, date=date_str)
    text += "\n\n" + t("h_marriage_duration", lang, duration=duration)
    text += "\n" + t("h_marriage_category", lang, category=category)
    text += "\n" + t(
        "h_marriage_level_line",
        lang,
        level_name=level_name,
        level=level,
        experience=exp,
    )
    # L-33: attach the joint-activities + history inline menu. The buttons
    # reuse the EXISTING couple-activity callbacks (FEAT-COUPLE) — the
    # activity EXECUTION logic is not duplicated here, only the entry
    # points. ``CoupleMenu``/``CoupleHistory`` are consumed by
    # handlers/couple_activities.
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from telegram_invite_bot.keyboards.builders.couple_activities import (
        CoupleHistory,
        CoupleMenu,
    )

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_couple_activities_btn", lang),
                    callback_data=CoupleMenu(
                        kind="marry", partner_id=partner_id, owner_id=user_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("h_couple_history_btn", lang),
                    callback_data=CoupleHistory(
                        kind="marry", partner_id=partner_id, owner_id=user_id
                    ).pack(),
                )
            ],
        ]
    )
    await message.reply(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# L-04  /marry_top_on /marry_top_off — rating-inclusion toggle
# ---------------------------------------------------------------------------


async def handle_marry_top_toggle(
    message: Message,
    command: CommandObject,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Toggle the marriage ``in_top`` flag. Mirrors ``cmd_marry_top_on`` /
    ``cmd_marry_top_off`` at ``bot.py:22728``/``bot.py:22746``."""
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    verb = (command.command or "").lower()
    on = verb in {"marry_top_on", "m_top_on", "брак_рейтинг_вкл"}

    if not await bonds_write_repo.set_marriage_in_top(chat_id, user_id, in_top=on):
        # #1860: the guarded UPDATE matched nothing, but a
        # write-headed statement takes ``BEGIN IMMEDIATE`` either
        # way — a lock over nothing, held for the whole reply.
        # Nothing to commit; just drop it.
        if checkpoint is not None:
            await checkpoint()
        await message.reply(t("h_marry_single", lang))
        return
    # #1860: the flag is flipped; the reply must not be able to undo it.
    if checkpoint is not None:
        await checkpoint()
    await message.reply(t("h_marry_top_on" if on else "h_marry_top_off", lang))


# ---------------------------------------------------------------------------
# L-05  /marry_extend <days> — paid renewal (10 coins/day, atomic escrow)
# ---------------------------------------------------------------------------


def _parse_extend_days(command: CommandObject) -> int | None:
    """First positive integer in the args, clamped to [1, 365]; else None.

    Mirrors the digit scan at ``bot.py:22777-22779`` (which skips the
    command token itself; ``command.args`` already excludes it here).
    """
    for tok in (command.args or "").split():
        if is_int_token(tok):
            return min(_MARRIAGE_EXTEND_MAX_DAYS, max(1, int(tok)))
    return None


async def handle_marry_extend(
    message: Message,
    command: CommandObject,
    bonds_write_repo: BondsWriteRepo,
    economy_service: EconomyService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Extend the marriage by N days, charging 10 coins/day.

    Mirrors ``cmd_marry_extend`` at ``bot.py:22764``. Cross-DB flow:
    the bond row lives in users.db (``bonds_write_repo``) and the coins
    in economy.db (``economy_service``) — two independent sessions. We
    escrow first (atomic guarded ``hold`` with a ledger row), then
    extend; if the extend write reports no active marriage (TOCTOU:
    divorced between the gate check and the write) we ``release`` the
    coins so the round trip nets to zero — mirroring the SEC-2
    unchecked-credit hardening and the couple-activity refund contract.
    On the happy path ``settle_hold`` books the escrow as a real spend.

    Escrow rather than ``debit``/``credit`` (#1546): the pair moves the
    lifetime counters in opposite directions, so a refunded round trip
    that moved no coins still leaves ``total_spent`` AND ``total_earned``
    permanently inflated by ``cost`` — free and repeatable for anyone
    willing to race their own /divorce. ``hold``/``release`` touch only
    ``balance``; the counter moves once, at settlement.
    """
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    # Gate on an active marriage up front so we never charge a single
    # user (matches legacy, which reads the marriage before charging).
    if await bonds_write_repo.get_marriage(chat_id, user_id) is None:
        await message.reply(t("h_marry_single", lang))
        return

    days = _parse_extend_days(command)
    if days is None:
        await message.reply(
            t(
                "h_marry_extend_usage",
                lang,
                cost_per_day=_MARRIAGE_EXTEND_COST_PER_DAY,
                sign=_COIN_SIGN,
            )
        )
        return

    cost = days * _MARRIAGE_EXTEND_COST_PER_DAY
    # #1546: escrow, not a spend — the bond row can still vanish between
    # here and the UPDATE below, and the rollback then has to be free.
    # ``debit``/``credit`` would net the BALANCE to zero but leave both
    # lifetime counters permanently inflated by ``cost``, which the user
    # can repeat at will by racing /divorce against their own command.
    wallet = await economy_service.hold(
        user_id, cost, type="marriage_extend", reason="marriage_extend"
    )
    if wallet is None:
        # #1860: ``hold``'s ``WHERE balance >= amount`` matched no row,
        # but the statement still took ``BEGIN IMMEDIATE`` on economy.db
        # — the single busiest file in the bot, locked over nothing for
        # the length of a Telegram round trip. Nothing to commit; drop
        # the lock.
        if checkpoint is not None:
            await checkpoint()
        await message.reply(t("h_marry_extend_no_coins", lang, cost=cost, sign=_COIN_SIGN))
        return

    if not await bonds_write_repo.extend_marriage(chat_id, user_id, days):
        # Bond vanished between the gate and the write — hand the escrow
        # back. ``release`` is the mirror of ``hold``: it returns the
        # exact ``cost`` just parked and leaves ``total_earned`` alone.
        await economy_service.release(  # money-guard: allow (escrow handed back)
            user_id, cost, type="marriage_extend_refund", reason="marriage_extend_refund"
        )
        # #1860: the checkpoint goes AFTER the release, never between
        # the hold and it. Placed earlier it would break the refund
        # contract above: an exception on the way to ``release`` would
        # leave the hold committed and the coins simply gone. Here both
        # legs are booked, the round trip nets to zero, and only the
        # locks on economy.db (hold + refund) and users.db (the 0-row
        # extend) are still standing — those the reply must not keep.
        if checkpoint is not None:
            await checkpoint()
        await message.reply(t("h_marry_single", lang))
        return

    # The purchase stuck, so the escrow is now a real spend. Legacy's
    # ``remove_coins`` bumped ``total_spent`` here (bot.py:9938), and
    # this is the leg that keeps that parity.
    await economy_service.settle_hold(user_id, cost)

    # #1860: the purchase is complete across BOTH databases — the spend
    # in economy.db and the new expiry in users.db. This is the only
    # site in the module that holds two write locks at once, and it held
    # them across the confirmation card. Commit here: everything below
    # is presentation, and a card the user never sees must not refund a
    # renewal they already own.
    if checkpoint is not None:
        await checkpoint()
    await message.reply(t("h_marry_extend_ok", lang, days=days, cost=cost, sign=_COIN_SIGN))
    log.bind(chat_id=chat_id, user_id=user_id, days=days, cost=cost).info("/marry_extend executed")


# ---------------------------------------------------------------------------
# L-06  /marry_auto_divorce <off|one|two> — auto-divorce mode toggle
# ---------------------------------------------------------------------------


async def handle_marry_auto_divorce(
    message: Message,
    command: CommandObject,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Set the marriage auto-divorce mode. Mirrors ``cmd_marry_auto_divorce``
    at ``bot.py:22796``. Only the setting is ported, not the periodic
    enforcement sweep (out of scope, see module docstring)."""
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    mode: str | None = None
    for tok in (command.args or "").lower().split():
        if tok in _AUTO_DIVORCE_MODES:
            mode = _AUTO_DIVORCE_MODES[tok]
            break
    if mode is None:
        await message.reply(t("h_marry_auto_divorce_usage", lang))
        return

    if not await bonds_write_repo.set_marriage_auto_divorce(chat_id, user_id, mode):
        # #1860: the guarded UPDATE matched nothing, but a
        # write-headed statement takes ``BEGIN IMMEDIATE`` either
        # way — a lock over nothing, held for the whole reply.
        # Nothing to commit; just drop it.
        if checkpoint is not None:
            await checkpoint()
        await message.reply(t("h_marry_single", lang))
        return

    # #1860: the mode is stored; the reply must not be able to undo it.
    if checkpoint is not None:
        await checkpoint()
    if mode == "off":
        await message.reply(t("h_marry_auto_divorce_off", lang))
    else:
        await message.reply(t("h_marry_auto_divorce_set", lang, mode=mode))


# ---------------------------------------------------------------------------
# L-07  /marry_other — another user's marriage status card
# ---------------------------------------------------------------------------


def _resolve_target_user(message: Message) -> object | None:
    """Target from a reply, or from a ``text_mention`` entity.

    Mirrors ``_resolve_target_user_from_message`` at ``bot.py:22714``.
    (A bare ``@username`` mention carries no user id in the entity, so —
    like legacy — only reply and ``text_mention`` resolve here.)
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    for ent in message.entities or []:
        if ent.type == "text_mention" and ent.user is not None:
            return ent.user
    return None


async def handle_marry_other(
    message: Message,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
) -> None:
    """Show another user's marriage status card (by reply or text_mention).

    Mirrors ``cmd_marry_other`` at ``bot.py:22832``.

    The only handler here that does not read the sender: it answers about
    whoever was replied to / mentioned, so who asked is irrelevant. It
    used to open with ``assert message.from_user is not None`` all the
    same — a guard over a value it never dereferenced — and it is the one
    registration below without ``F.from_user`` for the same reason.
    """
    chat_id = message.chat.id

    target = _resolve_target_user(message)
    if target is None or getattr(target, "is_bot", False):
        await message.reply(t("h_marry_reply", lang))
        return

    target_id = target.id  # type: ignore[attr-defined]
    marriage = await bonds_write_repo.get_marriage(chat_id, target_id)
    if marriage is None:
        await message.reply(t("h_marry_other_single", lang))
        return

    u1_mention = mention(
        marriage.user1_id, await bonds_write_repo.get_first_name(marriage.user1_id), lang
    )
    u2_mention = mention(
        marriage.user2_id, await bonds_write_repo.get_first_name(marriage.user2_id), lang
    )

    duration = format_duration(marriage.created_at, lang=lang)
    extra_days = marriage.duration_days or 0
    category = marriage_category(marriage.created_at, extra_days, lang=lang)

    await message.reply(
        t(
            "h_marry_other_status",
            lang,
            u1=u1_mention,
            u2=u2_mention,
            category=category,
            duration=duration,
        )
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(registry: EngineRegistry) -> Router:
    """Build the marriage/breakup router.

    Attaches its own :class:`SessionMiddleware` (users.db) so writes
    share the same per-update transaction as the read-side repos.
    Group-only: the ``F.chat.type`` filter keeps private-chat usage out
    of the handlers, and the #123 refusal twin answers it instead.
    """
    router = Router(name="marriage")
    router.message.middleware(SessionMiddleware(registry))
    router.callback_query.middleware(SessionMiddleware(registry))
    # L-05 /marry_extend needs a coin escrow (economy.db); attach the
    # EconomyMiddleware so ``economy_service`` is injected. The two
    # middlewares open independent sessions — see handle_marry_extend for
    # the cross-DB refund contract (mirrors handlers/couple_activities).
    router.message.middleware(EconomyMiddleware(registry))

    group_filter = F.chat.type.in_(GROUP_TYPES)

    # #303: ``F.from_user`` on every message registration whose handler
    # reads the sender — all of them except ``/marry_other``, which does
    # not. What it buys is honesty, not a crash fix, and the distinction
    # is worth spelling out because the comments it replaced got it
    # wrong. Those handlers used to open with ``assert message.from_user
    # is not None  # guaranteed by F.chat.type filter + group``, and no
    # registration carried ``F.from_user`` at all. The chat type does not
    # guarantee a sender; nothing here did.
    #
    # It does not follow that the assert was firing. ``router.message``
    # never sees a channel post (that is a ``channel_post`` update), and
    # for a message sent *on behalf of a chat* into a group the Bot API
    # fills ``from`` with a fake sender user — which is exactly why #246
    # had to look at ``sender_chat`` instead of ``from_user`` to catch a
    # member posting as their own channel (see
    # ``handlers/moderation.py:799-802``). So this filter guards a shape
    # aiogram's types admit and Telegram does not currently deliver here.
    # It is cheap, it makes
    # :func:`~telegram_invite_bot.utils.aiogram.require_from_user`'s own
    # justification true rather than aspirational, and it means the next
    # person to read these handlers is not told something false.
    #
    # It also does NOT filter out anonymous *admins* or channel-backed
    # senders — both carry a ``from_user`` — so the ``sender_chat``
    # refusal in ``handle_relationship`` remains the check that actually
    # turns those away, and remains reachable.

    # /marry
    router.message.register(
        handle_marry,
        Command("marry", "брак", "жениться", ignore_case=True),
        F.from_user,
        group_filter,
    )
    # /marry_accept and /marry_decline
    router.message.register(
        handle_marry_accept_decline,
        Command(
            "marry_accept",
            "marry_decline",
            "m_accept",
            "m_decline",
            "принять_брак",
            "отклонить_брак",
            ignore_case=True,
        ),
        F.from_user,
        group_filter,
    )
    # Inline keyboard callbacks
    router.callback_query.register(
        callback_marry_accept,
        F.data.regexp(r"^marry_accept_\d+$"),
    )
    router.callback_query.register(
        callback_marry_decline,
        F.data.regexp(r"^marry_decline_\d+$"),
    )
    # /divorce
    router.message.register(
        handle_divorce,
        Command("divorce", "развод", ignore_case=True),
        F.from_user,
        group_filter,
    )
    # /breakup
    router.message.register(
        handle_breakup,
        Command("breakup", "расстаться", ignore_case=True),
        F.from_user,
        group_filter,
    )
    # /relationship  (A-02 unbrick) — propose by reply or list own bonds
    router.message.register(
        handle_relationship,
        Command(
            "relationship",
            "rel",
            "отношения",
            "в_отношениях",
            ignore_case=True,
        ),
        F.from_user,
        group_filter,
    )
    # Relationship inline callbacks. The ``\d+$`` anchor keeps these from
    # clashing with legacy-era ``rel_activity_menu_*`` / ``rel_history_*``
    # buttons still sitting in old chat history (those carry a partner_id,
    # not a proposal_id, but share the ``rel_`` prefix). The port's own
    # activity buttons use the ``cpl_menu`` / ``cpl_hist`` prefixes
    # (:mod:`~keyboards.builders.couple_activities`) and never collide.
    router.callback_query.register(
        callback_rel_accept,
        F.data.regexp(r"^rel_accept_\d+$"),
    )
    router.callback_query.register(
        callback_rel_decline,
        F.data.regexp(r"^rel_decline_\d+$"),
    )

    # L-03 /marriage — own status card
    router.message.register(
        handle_marriage,
        Command("marriage", "my_marriage", "брак_статус", ignore_case=True),
        F.from_user,
        group_filter,
    )
    # L-04 /marry_top_on /marry_top_off — rating-inclusion toggle
    router.message.register(
        handle_marry_top_toggle,
        Command(
            "marry_top_on",
            "marry_top_off",
            "m_top_on",
            "m_top_off",
            "брак_рейтинг_вкл",
            "брак_рейтинг_выкл",
            ignore_case=True,
        ),
        F.from_user,
        group_filter,
    )
    # L-05 /marry_extend <days> — paid renewal
    router.message.register(
        handle_marry_extend,
        Command("marry_extend", "m_extend", "брак_продлить", ignore_case=True),
        F.from_user,
        group_filter,
    )
    # L-06 /marry_auto_divorce <off|one|two> — auto-divorce mode toggle
    router.message.register(
        handle_marry_auto_divorce,
        Command(
            "marry_auto_divorce",
            "auto_divorce",
            "брак_режим_развода",
            ignore_case=True,
        ),
        F.from_user,
        group_filter,
    )
    # L-07 /marry_other — another user's status card (reply / text_mention)
    router.message.register(
        handle_marry_other,
        Command("marry_other", "m_other", "твой_брак", ignore_case=True),
        group_filter,
    )

    return with_chat_type_refusal(router, scope="group")
