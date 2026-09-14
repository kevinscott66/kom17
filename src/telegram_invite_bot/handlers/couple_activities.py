"""Couple joint-activities — paid inline-button activities that grant pair XP.

Entry point — ``/activities`` (group-only)
------------------------------------------
The legacy bot attached a "💑 Совместные действия" button to the
relationship/marriage *card*. This module landed before the port had such
a card, so the feature also has a dedicated group-only ``/activities``
command (+ ru alias ``/совместные``): it resolves the caller's bond in
this chat (MARRIAGE first, else RELATIONSHIP) and renders the activity
menu for it. Neither token is a legacy command, so they're added to the
ported-command audit explicitly.

The card entry point has since landed as well: ``/relationship`` with no
reply and ``/marriage`` both hang a ``CoupleMenu`` / ``CoupleHistory``
pair on every row they render (:mod:`~handlers.marriage`), and those
callbacks are consumed here. ``/activities`` stays as the keyboard-free
way in — a user who scrolled the card out of reach still has a command.

The money path (do-activity callback)
-------------------------------------
The clicker PAYS; the PAIR gets XP. Because the wallet lives in
``economy.db`` (``EconomyMiddleware``) and the bond XP lives in
``users.db`` (``SessionMiddleware``) — two independent sessions, two
independent commits — atomicity across the two is achieved by ordering +
compensation, not a shared transaction:

  1. re-resolve the bond (gone → error, no charge);
  2. relationship: re-check level ≥ min_level (locked alert if not);
  3. ATOMIC escrow on economy.db — ``EconomyService.hold`` wraps a single
     guarded ``UPDATE ... WHERE balance >= cost RETURNING``; ``None`` ⇒
     not enough coins, bail with no XP;
  4. grant pair XP on users.db; if the pair row vanished between (1) and
     (4) the grant returns ``None`` — hand the escrow back via
     ``economy.release`` and surface the bond-gone error. Legacy refunded
     only for relationship; we refund for BOTH (mirrors the SEC-2
     unchecked-credit hardening) so a marriage XP-grant race can't
     silently eat coins. When the grant DOES land, ``settle_hold`` books
     the escrow onto ``total_spent`` — the leg that keeps parity with
     legacy's ``remove_coins`` (bot.py:9938).

     Why the escrow pair and not ``debit``/``credit`` (#1546): a
     ``debit`` bumps ``total_spent`` and its compensating ``credit``
     bumps ``total_earned``, so a refunded round trip that moved no coins
     still inflates BOTH lifetime numbers on the ``/balance`` card — and
     the user controls both halves of the race (their own click vs their
     own /breakup), so it is repeatable for free. ``hold``/``release``
     move only ``balance``; ``settle_hold`` moves the counter once the
     purchase is final.

The money moves through :class:`EconomyService`, not ``EconomyRepo``, so
every charge leaves a ``transactions`` row (``couple_activity`` /
``couple_activity_refund``). Talking to the repo directly used to make
this the ONE spend surface with no ledger entry: coins left the wallet
and ``/balance``'s weekly "sent" total never moved, which reads as coins
vanishing. Reads (the catalog's balance lookup) still use the repo —
they mint nothing, so they need no ledger.

Both middlewares commit at handler return: the economy.db commit carries
the escrow and its ledger row (and, on the refund path, the release
plus its own row, so the net is zero and the audit trail shows both
legs); the users.db commit carries the XP bump. No cooldown —
legacy had none for this surface, so none is added.

Where that commit happens is not uniform, and #1869 made the split
explicit. The two refusal branches — no coins, and the bond vanishing
between the gate and the XP grant — end their transaction BEFORE the
``show_alert`` popup: ``hold`` is a guarded ``UPDATE`` and takes the
economy write lock even when it matches nothing, and neither branch
leaves anything owed, so holding two write slots across a Telegram
round trip buys nothing. The success path keeps the transaction open on
purpose: an undeliverable result card must roll the spend and the XP
back together, because charging a couple for an activity nobody can see
is the worst of the three possible endings. Lock duration is the price
of that refund, and it is paid deliberately.

HTML parse_mode: first_names are attacker-controlled, so mentions go
through :func:`~telegram_invite_bot.utils.names.mention`, which resolves
the localized fallback for a missing name and escapes what it renders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.core.couple_activities import (
    HISTORY_ONLY_ICONS,
    MARRIAGE_ACTIVITIES,
    MARRIAGE_BY_KEY,
    RELATIONSHIP_ACTIVITIES,
    RELATIONSHIP_BY_KEY,
    effect_hours_split,
    effect_template_key,
    relationship_available,
)
from telegram_invite_bot.handlers.group_only import handle_group_only
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import CoupleActivity
from telegram_invite_bot.keyboards.builders.couple_activities import (
    CoupleHistory,
    CoupleMenu,
)
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.utils.aiogram import (
    UndeliverableResultError,
    edit_card,
    reply_or_send,
    require_from_user,
)
from telegram_invite_bot.utils.bonds import (
    format_db_date,
    marriage_level_name,
    marriage_xp_to_level,
)
from telegram_invite_bot.utils.html import legacy_md_to_html
from telegram_invite_bot.utils.names import mention
from telegram_invite_bot.utils.numbers import (
    format_number,
    format_xp_short,
    format_xp_spark,
)

log = logger.bind(component="handlers.couple_activities")

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.bonds_repo import BondsWriteRepo
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.services.economy_service import EconomyService


# Coin sign appended to the not-enough-coins / cost lines. Legacy uses a
# bare emoji; the i18n ``rel_activity_coin_suffix`` carries the textual
# "i¢" fallback, kept here so the {sign} substitution stays locale-stable.
_COIN_SIGN = "🪙"

# Telegram rejects an inline-button caption over 64 characters. Legacy
# clipped at exactly that (bot.py:23316) and so do we — the catalog BODY
# carries the full title, so a clipped caption never hides information.
_BUTTON_CAPTION_MAX = 64

# Fallback glyph for a history row whose key is in neither catalog and
# not in HISTORY_ONLY_ICONS either — a key no version of this bot has
# ever known how to name.
_UNKNOWN_ACTIVITY_ICON = "💕"

# ...and legacy's title for that same case (bot.py:23093, :23407). An
# em dash says "this row happened, we cannot name it"; the raw key says
# "the bot is broken".
_UNKNOWN_ACTIVITY_TITLE = "—"


def _activity_icon(key: str) -> str:
    """The language-neutral glyph for ``key``, ``💕`` if it is unknown."""
    act = MARRIAGE_BY_KEY.get(key) or RELATIONSHIP_BY_KEY.get(key)
    if act is not None:
        return act.icon
    return HISTORY_ONLY_ICONS.get(key, _UNKNOWN_ACTIVITY_ICON)


def _activity_title(key: str, lang: str) -> str:
    """The localised title for ``key`` (RR-5 #49, #232).

    Titles moved out of an in-module ``(ru, en)`` table and into
    ``h_couple_act_name_*`` so the ru/en convergence and
    no-Cyrillic-in-``en.yaml`` guards can see them. The membership check
    is load-bearing: ``t()`` echoes the key back when it is missing, so
    an unguarded lookup would render the literal string
    ``h_couple_act_name_cafe``.

    #232: the check used to cover the two CATALOGS only, and returned
    the raw key for everything else. But the history log is not written
    by the catalogs alone — every RP action lands there too, and so do
    activity keys retired before the catalog was last reshuffled. On
    production that was 14 of 18 rows reading ``💕 rp_tickle``.
    ``HISTORY_ONLY_ICONS`` is the third source of nameable keys, and
    legacy named all three (bot.py:23088-23093, :23401-23407). Only a
    key none of them knows falls through, and it falls through to the
    same ``—`` legacy used, not to the key.
    """
    if (
        key not in MARRIAGE_BY_KEY
        and key not in RELATIONSHIP_BY_KEY
        and key not in HISTORY_ONLY_ICONS
    ):
        return _UNKNOWN_ACTIVITY_TITLE
    return t(f"h_couple_act_name_{key}", lang)


# ---------------------------------------------------------------------------
# Menu render
# ---------------------------------------------------------------------------


def _history_button(
    kind: str, partner_id: int, lang: str, *, owner_id: int
) -> InlineKeyboardButton:
    """The "📜 history" button shared by both menus + both status cards.

    ``owner_id`` is the user the card is rendered for (#463); the handler
    rejects a click from anyone else.
    """
    return InlineKeyboardButton(
        text=t("h_couple_history_btn", lang),
        callback_data=CoupleHistory(kind=kind, partner_id=partner_id, owner_id=owner_id).pack(),
    )


def _catalog_line(key: str, *, ok: bool, cost: int, xp: int, lang: str) -> str:
    """One row of the text catalog above the buttons (RR-5 #49).

    Legacy rendered ``✅ «Название» +3k, 1 350 i¢`` (bot.py:23308). We
    keep the shape and add the row's ``icon`` plus ``<b>`` on the title,
    and spell the coin sign with the same 🪙 the BUTTONS use rather than
    the ``rel_activity_coin_suffix`` "i¢" legacy put here — two different
    signs for one currency in one message is the kind of detail that
    reads as sloppy, and 🪙 is what every other economy surface in the
    new pipeline shows.
    """
    glyph = "✅" if ok else "🕔"
    title = _activity_title(key, lang)
    return (
        f"{glyph} {_activity_icon(key)} <b>{title}</b> — "
        f"{format_xp_short(xp)} XP · {format_number(cost)} {_COIN_SIGN}"
    )


def _button_caption(key: str, *, ok: bool, cost: int, xp: int, lang: str) -> str:
    """Button caption, clipped to Telegram's 64-character limit."""
    glyph = "✅" if ok else "🕔"
    text = (
        f"{glyph} {_activity_icon(key)} {_activity_title(key, lang)} ({cost}{_COIN_SIGN} +{xp} XP)"
    )
    if len(text) > _BUTTON_CAPTION_MAX:
        text = text[: _BUTTON_CAPTION_MAX - 1] + "…"
    return text


def _build_marriage_menu(
    partner_id: int, lang: str, *, balance: int, owner_id: int
) -> InlineKeyboardMarkup:
    """One button per marriage activity — no level gate, only affordability.

    Marriage rows carry no ``min_level``, so ``ok`` here is purely "can
    the clicker pay for it right now". A 🕔 row is still clickable: the
    do-activity handler re-checks and surfaces the precise reason, so a
    user who tops up between renders isn't stuck.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for act in MARRIAGE_ACTIVITIES:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_button_caption(
                        act.key,
                        ok=balance >= act.cost,
                        cost=act.cost,
                        xp=act.xp,
                        lang=lang,
                    ),
                    callback_data=CoupleActivity(
                        kind="marry",
                        key=act.key,
                        partner_id=partner_id,
                        owner_id=owner_id,
                    ).pack(),
                )
            ]
        )
    rows.append([_history_button("marry", partner_id, lang, owner_id=owner_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _marriage_catalog(lang: str, *, balance: int) -> str:
    """The marriage text catalog + the ✅/🕔 legend."""
    lines = [
        _catalog_line(act.key, ok=balance >= act.cost, cost=act.cost, xp=act.xp, lang=lang)
        for act in MARRIAGE_ACTIVITIES
    ]
    return "\n".join(lines) + "\n\n" + t("h_couple_act_legend_marry", lang)


def _relationship_catalog(lang: str, *, level: int, balance: int) -> str:
    """The relationship text catalog + the ✅/🕔 legend."""
    lines = [
        _catalog_line(
            act.key,
            ok=relationship_available(act, level=level, balance=balance),
            cost=act.cost,
            xp=act.xp,
            lang=lang,
        )
        for act in RELATIONSHIP_ACTIVITIES
    ]
    return "\n".join(lines) + "\n\n" + t("h_couple_act_legend_rel", lang)


def _build_relationship_menu(
    partner_id: int, lang: str, *, level: int, balance: int, owner_id: int
) -> InlineKeyboardMarkup:
    """One button per relationship activity; ✅ if available, 🕔 if locked.

    "Locked" means the couple is below the activity's ``min_level`` OR the
    clicker can't afford the cost (mirrors legacy's single locked glyph
    for both). Locked rows still render a button — the do-activity handler
    re-checks and surfaces the precise reason (level vs coins) as an
    alert, so a user who tops up between renders isn't stuck.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for act in RELATIONSHIP_ACTIVITIES:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_button_caption(
                        act.key,
                        ok=relationship_available(act, level=level, balance=balance),
                        cost=act.cost,
                        xp=act.xp,
                        lang=lang,
                    ),
                    callback_data=CoupleActivity(
                        kind="rel",
                        key=act.key,
                        partner_id=partner_id,
                        owner_id=owner_id,
                    ).pack(),
                )
            ]
        )
    rows.append([_history_button("rel", partner_id, lang, owner_id=owner_id)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def handle_activities(
    message: Message,
    bonds_write_repo: BondsWriteRepo,
    economy_repo: EconomyRepo,
    lang: str,
) -> None:
    """``/activities`` — render the joint-activity menu for the caller's bond.

    MARRIAGE is checked first (a married caller sees the marriage menu);
    otherwise the caller's strongest relationship in this chat is used.
    If the caller has neither bond → "no pair".
    """
    tg_user = require_from_user(message)
    chat_id = message.chat.id
    user_id = tg_user.id

    marriage = await bonds_write_repo.get_marriage(chat_id, user_id)
    if marriage is not None:
        partner_id = marriage.user2_id if marriage.user1_id == user_id else marriage.user1_id
        partner_name = await bonds_write_repo.get_first_name(partner_id)
        me_mention = mention(user_id, tg_user.first_name, lang)
        partner_mention = mention(partner_id, partner_name, lang)
        wallet = await economy_repo.get(user_id)
        balance = wallet.balance if wallet is not None else 0
        body = (
            legacy_md_to_html(t("marry_activity_title", lang))
            + "\n\n"
            + t("rel_activity_list_header", lang, me=me_mention, partner=partner_mention)
            + "\n\n"
            + _marriage_catalog(lang, balance=balance)
        )
        await message.reply(
            body,
            reply_markup=_build_marriage_menu(partner_id, lang, balance=balance, owner_id=user_id),
        )
        return

    rels = await bonds_write_repo.list_relationships_for(chat_id, user_id)
    if not rels:
        await message.reply(t("h_couple_act_no_pair", lang))
        return

    # Strongest bond first — ``list_relationships_for`` sorts on the
    # DECAYED experience (#477), which matters here more than in the
    # other two callers: this index picks WHICH pair the activity menu
    # acts on, so ordering by the stale stored column could aim the menu
    # at a bond that has decayed to nothing.
    rel = rels[0]
    partner_id = rel.user2_id if rel.user1_id == user_id else rel.user1_id
    level = bonds_write_repo._rel_xp_to_level(rel.experience or 0)
    wallet = await economy_repo.get(user_id)
    balance = wallet.balance if wallet is not None else 0

    partner_name = await bonds_write_repo.get_first_name(partner_id)
    me_mention = mention(user_id, tg_user.first_name, lang)
    partner_mention = mention(partner_id, partner_name, lang)
    body = (
        legacy_md_to_html(t("rel_activity_title", lang))
        + "\n\n"
        + t("rel_activity_list_header", lang, me=me_mention, partner=partner_mention)
        # RR-5 #49: the text catalog — the buttons alone never showed
        # WHICH rows are within reach at a glance, and a locked row's
        # cost/XP was only readable by squinting at a clipped caption.
        + "\n\n"
        + _relationship_catalog(lang, level=level, balance=balance)
        # RR-5 #50: restore the RP-unlock preview + the closing usage hint
        # (both keys existed but were no longer rendered).
        + "\n\n"
        + t("rel_activity_rp_unlock_lines", lang)
        + "\n\n"
        + t("rel_activity_footer_hint", lang)
    )
    await message.reply(
        body,
        reply_markup=_build_relationship_menu(
            partner_id, lang, level=level, balance=balance, owner_id=user_id
        ),
    )


# ---------------------------------------------------------------------------
# Do-activity callback (the money path)
# ---------------------------------------------------------------------------


async def handle_couple_activity(
    callback: CallbackQuery,
    callback_data: CoupleActivity,
    bonds_write_repo: BondsWriteRepo,
    economy_service: EconomyService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """A menu button was clicked — charge the clicker, grant the pair XP.

    See the module docstring for the cross-DB atomicity/refund contract.
    """
    assert callback.from_user is not None  # F.from_user filter guarantees
    assert callback.message is not None
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    partner_id = callback_data.partner_id

    # #463 auth tag: the card was rendered for ``owner_id``; a click from
    # anyone else is rejected. Nothing below leaks or spends across
    # users — every branch re-resolves the bond from the CLICKER — so
    # what this stops is defacement: in a group, a passer-by could edit
    # somebody else's card into their own menu, which reads to the chat
    # as the owner's data changing under them. Same posture as
    # ``withdraw.handle_withdraw_confirm``.
    if user_id != callback_data.owner_id:
        await callback.answer(t("h_couple_foreign_click", lang), show_alert=True)
        return

    if callback_data.kind == "marry":
        await _do_marriage_activity(
            callback,
            callback_data,
            bonds_write_repo,
            economy_service,
            lang,
            chat_id,
            user_id,
            checkpoint,
        )
    else:
        await _do_relationship_activity(
            callback,
            callback_data,
            bonds_write_repo,
            economy_service,
            lang,
            chat_id,
            user_id,
            partner_id,
            checkpoint,
        )


async def _do_marriage_activity(
    callback: CallbackQuery,
    callback_data: CoupleActivity,
    bonds: BondsWriteRepo,
    economy: EconomyService,
    lang: str,
    chat_id: int,
    user_id: int,
    checkpoint: Checkpoint | None,
) -> None:
    act = MARRIAGE_BY_KEY.get(callback_data.key)
    if act is None:
        await callback.answer("❌")
        return

    # 1. re-resolve the bond
    marriage = await bonds.get_marriage(chat_id, user_id)
    if marriage is None:
        await callback.answer(t("h_couple_act_bond_gone", lang)[:200], show_alert=True)
        return

    # 3. atomic escrow (no level gate for marriage). #1546: ``hold``
    # rather than ``debit`` because step 4 can still bounce, and a
    # ``debit``/``credit`` round trip inflates BOTH lifetime counters
    # by ``act.cost`` while moving no coins — repeatable by racing
    # /breakup against one's own click.
    wallet = await economy.hold(
        user_id, act.cost, type="couple_activity", reason=f"marry:{act.key}"
    )
    if wallet is None:
        # #1869: ``hold`` is a guarded UPDATE, so it took the economy
        # write lock even though it matched no row. End the transaction
        # before the popup — nothing was escrowed, so there is nothing
        # left to unwind.
        if checkpoint is not None:
            await checkpoint()
        await callback.answer(
            t("rel_activity_no_coins", lang, cost=act.cost, sign=_COIN_SIGN)[:200],
            show_alert=True,
        )
        return

    # 4. grant pair XP; hand the escrow back on a vanished pair row
    new_exp = await bonds.add_marriage_xp(chat_id, user_id, act.xp)
    if new_exp is None:
        await economy.release(  # money-guard: allow (escrow handed back)
            user_id,
            act.cost,
            type="couple_activity_refund",
            reason=f"marry:{act.key}",
        )
        # #1869: the refund is the last write of this branch. Commit it
        # before the popup so a failed alert cannot re-charge the user.
        if checkpoint is not None:
            await checkpoint()
        await callback.answer(t("h_couple_act_bond_gone", lang)[:200], show_alert=True)
        return

    # The XP landed, so the escrow is a real spend — book it on the
    # lifetime counter the way legacy's ``remove_coins`` did.
    await economy.settle_hold(user_id, act.cost)

    partner_id = marriage.user2_id if marriage.user1_id == user_id else marriage.user1_id
    # L-34: record the activity so the marriage card's history view can
    # show it. Same users.db session as the XP grant — both commit
    # together, so a logged row and the XP it records never diverge.
    await bonds.log_marriage_activity(chat_id, user_id, partner_id, act.key, act.xp, user_id)
    # #1869: NO checkpoint here, deliberately. The transaction stays
    # open across the card draw because an undeliverable card must
    # refund the whole activity — charging for something nobody can
    # see is the worst of the three endings, and the session
    # middleware's rollback is what prevents it. See
    # ``test_undeliverable_result_rolls_back_the_whole_activity``.
    # The refusal branches above carry no such debt and do commit.
    partner_name = await bonds.get_first_name(partner_id)
    await _render_done(
        callback,
        kind="marry",
        key=act.key,
        xp=act.xp,
        lang=lang,
        new_exp=new_exp,
        # Was ``None``, which rendered the tier name as an empty <b></b>
        # in ``marry_activity_done`` — RR-5 #53 fixed the render but not
        # the caller. The level comes from the POST-grant XP so a step-up
        # shows in the very card that caused it.
        level=marriage_xp_to_level(new_exp),
        effect_hours=None,
        actor=mention(user_id, callback.from_user.first_name, lang),
        partner=mention(partner_id, partner_name, lang),
    )
    log.bind(chat_id=chat_id, user_id=user_id, partner_id=partner_id, key=act.key).info(
        "couple marriage activity"
    )


async def _do_relationship_activity(
    callback: CallbackQuery,
    callback_data: CoupleActivity,
    bonds: BondsWriteRepo,
    economy: EconomyService,
    lang: str,
    chat_id: int,
    user_id: int,
    partner_id: int,
    checkpoint: Checkpoint | None,
) -> None:
    act = RELATIONSHIP_BY_KEY.get(callback_data.key)
    if act is None:
        await callback.answer("❌")
        return

    # 1. re-resolve the bond
    rel = await bonds.get_relationship(chat_id, user_id, partner_id)
    if rel is None:
        await callback.answer(t("h_couple_act_bond_gone", lang)[:200], show_alert=True)
        return

    # 2. re-check level
    level = bonds._rel_xp_to_level(rel.experience or 0)
    if level < act.min_level:
        await callback.answer(t("rel_activity_locked", lang)[:200], show_alert=True)
        return

    # 3. atomic escrow — same reasoning as the marriage branch (#1546).
    wallet = await economy.hold(user_id, act.cost, type="couple_activity", reason=f"rel:{act.key}")
    if wallet is None:
        # #1869: same as the marriage branch — the guarded UPDATE locked
        # economy.db for a refusal that moved nothing.
        if checkpoint is not None:
            await checkpoint()
        await callback.answer(
            t("rel_activity_no_coins", lang, cost=act.cost, sign=_COIN_SIGN)[:200],
            show_alert=True,
        )
        return

    # 4. grant pair XP; hand the escrow back on a vanished pair row
    new_exp = await bonds.add_relationship_xp(chat_id, user_id, partner_id, act.xp)
    if new_exp is None:
        await economy.release(  # money-guard: allow (escrow handed back)
            user_id,
            act.cost,
            type="couple_activity_refund",
            reason=f"rel:{act.key}",
        )
        # #1869: commit the refund before the popup.
        if checkpoint is not None:
            await checkpoint()
        await callback.answer(t("h_couple_act_bond_gone", lang)[:200], show_alert=True)
        return

    # The XP landed — settle the escrow into ``total_spent``.
    await economy.settle_hold(user_id, act.cost)

    # L-35: record the activity for the relationship card's history view.
    await bonds.log_relationship_activity(chat_id, user_id, partner_id, act.key, act.xp, user_id)
    # #1869: no checkpoint on the success path — see the marriage
    # branch for why the rollback window is deliberate here.

    new_level = bonds._rel_xp_to_level(new_exp)
    partner_name = await bonds.get_first_name(partner_id)
    await _render_done(
        callback,
        kind="rel",
        key=act.key,
        xp=act.xp,
        lang=lang,
        new_exp=new_exp,
        level=new_level,
        effect_hours=act.effect_hours,
        actor=mention(user_id, callback.from_user.first_name, lang),
        partner=mention(partner_id, partner_name, lang),
    )
    log.bind(chat_id=chat_id, user_id=user_id, partner_id=partner_id, key=act.key).info(
        "couple relationship activity"
    )


def _story_line(key: str, lang: str, *, actor: str, partner: str) -> str:
    """The flavoured "who did what for whom" opener (RR-5 #51).

    Legacy built this from the row's ``done_line_ru``/``done_line_en``
    and led it with ``💕 {icon} | `` (bot.py:22335-22343); the split port
    replaced the whole thing with a generic "you and your partner:
    <activity>" line that named neither person.

    Substitution is ``str.replace``, NOT ``format`` — legacy calls this
    out explicitly and it is a real hazard here: ``actor``/``partner``
    are rendered mentions built from attacker-controlled first names, so
    a display name containing ``{`` or ``}`` would raise inside
    ``format_map``. Passing them as ``t()`` kwargs would also let a name
    smuggle a ``{level}``-style placeholder into the template.
    """
    template = t(f"h_couple_act_done_{key}", lang)
    if template == f"h_couple_act_done_{key}":
        # Retired catalog key (history-only rows) — no story exists.
        template = f"{{actor}} — {{partner}}: {_activity_title(key, lang)}"
    story = template.replace("{actor}", actor).replace("{partner}", partner)
    return f"💕 {_activity_icon(key)} | {story}"


async def _render_done(
    callback: CallbackQuery,
    *,
    kind: str,
    key: str,
    xp: int,
    lang: str,
    new_exp: int,
    level: int | None,
    effect_hours: int | None,
    actor: str,
    partner: str,
) -> None:
    """Build + send the confirmation card; ACK the callback.

    Both kinds now open with the :func:`_story_line` flavour opener and
    share the ``rel_activity_done_bond`` XP line; marriage closes with
    the tier-name stats line, relationship with the cosmetic effect
    flavour plus its own ``_stats`` line. ``actor``/``partner`` arrive
    already escaped (they are :func:`utils.names.mention` output).
    """
    assert level is not None
    lines = [
        _story_line(key, lang, actor=actor, partner=partner),
        t("rel_activity_done_bond", lang, gain=format_xp_spark(xp, lang)),
    ]
    if kind == "marry":
        # RR-5 #53: the marriage TIER NAME ("Супруги"), not a bare number.
        lines.append(
            t(
                "h_marry_act_done_stats",
                lang,
                level_name=marriage_level_name(level, lang),
                experience=format_number(new_exp),
            )
        )
    else:
        assert effect_hours is not None
        days, hours = effect_hours_split(effect_hours)
        lines.append(t(effect_template_key(effect_hours), lang, days=days, hours=hours))
        lines.append(
            t(
                "rel_activity_done_stats",
                lang,
                level=level,
                experience=format_number(new_exp),
            )
        )
    body = "\n\n".join(lines)

    await callback.answer()
    # The coins are already spent and the XP is already written, so the
    # couple has to SEE this — the old blanket suppress here charged for
    # an activity that could then produce nothing at all, and left no
    # log line either.
    if isinstance(callback.message, Message) and not await reply_or_send(callback.message, body):
        log.bind(uid=callback.from_user.id).warning("/cpc activity result undeliverable")
        # Nothing reached the chat — kicked, blocked, deleted. Raising
        # hands the whole update back to the session middleware, which
        # rolls the escrow and the XP grant back together: an activity
        # that never happened beats one that was paid for and vanished.
        raise UndeliverableResultError("/cpc activity result")


# ---------------------------------------------------------------------------
# L-34 / L-35: open-menu-from-card + history callbacks
# ---------------------------------------------------------------------------


async def handle_couple_menu(
    callback: CallbackQuery,
    callback_data: CoupleMenu,
    bonds_write_repo: BondsWriteRepo,
    economy_repo: EconomyRepo,
    lang: str,
) -> None:
    """A status-card "activities" button was clicked — edit it into the menu.

    Re-resolves the caller's bond (the clicker may be either spouse) and
    edits the card message in place into the same activity menu
    ``/activities`` renders, so the card and the menu share one render
    path. ``callback_data.partner_id`` pins the relationship pair; for
    marriage the bond is re-resolved from the clicker alone.
    """
    assert callback.from_user is not None
    assert callback.message is not None
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    # #463 auth tag: the card was rendered for ``owner_id``; a click from
    # anyone else is rejected. Nothing below leaks or spends across
    # users — every branch re-resolves the bond from the CLICKER — so
    # what this stops is defacement: in a group, a passer-by could edit
    # somebody else's card into their own menu, which reads to the chat
    # as the owner's data changing under them. Same posture as
    # ``withdraw.handle_withdraw_confirm``.
    if user_id != callback_data.owner_id:
        await callback.answer(t("h_couple_foreign_click", lang), show_alert=True)
        return

    if callback_data.kind == "marry":
        marriage = await bonds_write_repo.get_marriage(chat_id, user_id)
        if marriage is None:
            await callback.answer(t("h_marry_single", lang)[:200], show_alert=True)
            return
        partner_id = marriage.user2_id if marriage.user1_id == user_id else marriage.user1_id
        partner_name = await bonds_write_repo.get_first_name(partner_id)
        me_mention = mention(user_id, callback.from_user.first_name, lang)
        partner_mention = mention(partner_id, partner_name, lang)
        wallet = await economy_repo.get(user_id)
        balance = wallet.balance if wallet is not None else 0
        body = (
            legacy_md_to_html(t("marry_activity_title", lang))
            + "\n\n"
            + t("rel_activity_list_header", lang, me=me_mention, partner=partner_mention)
            + "\n\n"
            + _marriage_catalog(lang, balance=balance)
        )
        markup = _build_marriage_menu(partner_id, lang, balance=balance, owner_id=user_id)
    else:
        partner_id = callback_data.partner_id
        rel = await bonds_write_repo.get_relationship(chat_id, user_id, partner_id)
        if rel is None:
            await callback.answer(t("h_rel_single", lang)[:200], show_alert=True)
            return
        level = bonds_write_repo._rel_xp_to_level(rel.experience or 0)
        wallet = await economy_repo.get(user_id)
        balance = wallet.balance if wallet is not None else 0
        partner_name = await bonds_write_repo.get_first_name(partner_id)
        me_mention = mention(user_id, callback.from_user.first_name, lang)
        partner_mention = mention(partner_id, partner_name, lang)
        body = (
            legacy_md_to_html(t("rel_activity_title", lang))
            + "\n\n"
            + t("rel_activity_list_header", lang, me=me_mention, partner=partner_mention)
            + "\n\n"
            + _relationship_catalog(lang, level=level, balance=balance)
        )
        markup = _build_relationship_menu(
            partner_id, lang, level=level, balance=balance, owner_id=user_id
        )

    await callback.answer()
    if isinstance(callback.message, Message):
        # Re-opening the menu/page already on screen is a no-op edit,
        # and the callback is ACKed above either way. Narrowed from a
        # blanket suppress so a malformed card still surfaces.
        await edit_card(callback.message, body, reply_markup=markup)


async def handle_couple_history(
    callback: CallbackQuery,
    callback_data: CoupleHistory,
    bonds_write_repo: BondsWriteRepo,
    lang: str,
) -> None:
    """A "history" button was clicked — edit the message into the last-15 log.

    L-34 (marriage) / L-35 (relationship). Reads the bond activity-log
    (newest first) and renders one line per row; an empty log shows the
    "no records yet" copy. A "back to activities" button returns to the
    menu without losing the card.
    """
    assert callback.from_user is not None
    assert callback.message is not None
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    # #463 auth tag: the card was rendered for ``owner_id``; a click from
    # anyone else is rejected. Nothing below leaks or spends across
    # users — every branch re-resolves the bond from the CLICKER — so
    # what this stops is defacement: in a group, a passer-by could edit
    # somebody else's card into their own menu, which reads to the chat
    # as the owner's data changing under them. Same posture as
    # ``withdraw.handle_withdraw_confirm``.
    if user_id != callback_data.owner_id:
        await callback.answer(t("h_couple_foreign_click", lang), show_alert=True)
        return

    if callback_data.kind == "marry":
        marriage = await bonds_write_repo.get_marriage(chat_id, user_id)
        if marriage is None:
            await callback.answer(t("h_marry_single", lang)[:200], show_alert=True)
            return
        partner_id = marriage.user2_id if marriage.user1_id == user_id else marriage.user1_id
        entries = await bonds_write_repo.get_marriage_activity_log(
            chat_id, user_id, partner_id, limit=15
        )
    else:
        partner_id = callback_data.partner_id
        rel = await bonds_write_repo.get_relationship(chat_id, user_id, partner_id)
        if rel is None:
            await callback.answer(t("h_rel_single", lang)[:200], show_alert=True)
            return
        entries = await bonds_write_repo.get_relationship_activity_log(
            chat_id, user_id, partner_id, limit=15
        )

    if entries:
        lines = [
            t(
                "h_couple_history_line",
                lang,
                activity=(
                    f"{_activity_icon(e.activity_key)} {_activity_title(e.activity_key, lang)}"
                ),
                xp=e.xp_gained,
                date=format_db_date(e.created_at),
            )
            for e in entries
        ]
        body = t("h_couple_history_title", lang) + "\n\n" + "\n".join(lines)
    else:
        body = t("h_couple_history_title", lang) + "\n\n" + t("h_couple_history_empty", lang)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_couple_history_back_btn", lang),
                    callback_data=CoupleMenu(
                        kind=callback_data.kind,
                        partner_id=partner_id,
                        owner_id=user_id,
                    ).pack(),
                )
            ]
        ]
    )

    await callback.answer()
    if isinstance(callback.message, Message):
        # Re-opening the menu/page already on screen is a no-op edit,
        # and the callback is ACKed above either way. Narrowed from a
        # blanket suppress so a malformed card still surfaces.
        await edit_card(callback.message, body, reply_markup=markup)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(registry: EngineRegistry) -> Router:
    """Build the couple-activities router.

    Mounts BOTH middlewares on each event observer:

    * :class:`SessionMiddleware` (users.db) → ``bonds_write_repo`` for the
      bond lookup + XP grant;
    * :class:`EconomyMiddleware` (economy.db) → ``economy_service`` for
      the ledgered hold/release money path, and ``economy_repo`` for the
      plain balance reads the catalog renders.

    Each opens its own session and commits independently at handler return
    (see the module docstring on the cross-DB refund contract). The
    callback observer gets its own copies so a button click sees both
    repos injected, mirroring handlers/shop's parallel-middleware posture.

    ``/activities`` is group-only via the ``F.chat.type`` filter, with a
    private-chat twin that says so (#122) — "private invocations fall
    through" used to mean "fall through to the legacy process", and
    with legacy gone it meant falling through to silence. The
    do-activity callback carries NO chat-type filter — like the shop
    callbacks it must resolve a click on a forwarded/stale button —
    and reads chat_id off ``callback.message.chat``.
    """
    router = Router(name="couple_activities")
    router.message.middleware(SessionMiddleware(registry))
    router.message.middleware(EconomyMiddleware(registry))
    router.callback_query.middleware(SessionMiddleware(registry))
    router.callback_query.middleware(EconomyMiddleware(registry))

    router.message.register(
        handle_activities,
        Command("activities", "acts", "совместные", ignore_case=True),
        F.from_user,
        F.chat.type.in_(GROUP_TYPES),
    )
    router.message.register(
        handle_group_only,
        Command("activities", "acts", "совместные", ignore_case=True),
        F.chat.type == ChatType.PRIVATE,
    )
    router.callback_query.register(
        handle_couple_activity,
        CoupleActivity.filter(),
        F.from_user,
    )
    # L-34/L-35: open-menu-from-card + history views. Like the do-activity
    # callback these carry no chat-type filter (a click on a stale/edited
    # card must still resolve) and read chat_id off callback.message.chat.
    router.callback_query.register(
        handle_couple_menu,
        CoupleMenu.filter(),
        F.from_user,
    )
    router.callback_query.register(
        handle_couple_history,
        CoupleHistory.filter(),
        F.from_user,
    )
    return router
