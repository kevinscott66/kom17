"""``/vip`` and friends — Stage 15 stubs + the real ``/vip_shop``.

Most of the commands routed here are pure static-text "feature coming
soon" replies — no DB, no services, no FSM. Porting them bought two
things:

#1657: the count and the source that used to open this paragraph were
both invented. It named ``bot/handlers/vip_emoji_voice.py``, a
directory that has never existed in this repository, and claimed five
of six commands. Legacy has command handlers for only two of the five
routed below — ``cmd_voice_vip`` and ``cmd_voice_settings`` (which also
takes ``/voice_settings_ru``). ``/vip`` and ``/vip_shop`` have no legacy
command at all, and ``voice_stats`` exists in legacy only as the
callback ``cb_voice_stats``, never as a command.

1. The legacy module is small enough to retire entirely. One less file
   the strangler-bridge has to fall through to.
2. When the real VIP layer lands the command surface is already routed
   by the new dispatcher — only the handler bodies change.

The stub copy is byte-identical to legacy (translations and emoji),
re-rendered to HTML because the new bot's default parse_mode is HTML
(the legacy module used Markdown).

Private-only — same boundary as every other ported stub. Groups keep
flowing into legacy (which itself answers in any chat); once the legacy
module is fully retired the group case is a no-op.

PARITY NOTE: ``/voice_settings`` in legacy (``bot.py:32268``) is a
group-admin command that renders a per-group settings menu; the
monolith returns early if not in a group. In private the new handler
answers a stub that explains where to find the real setting. Groups
still hit the real menu via the legacy bridge.

``/vip_shop`` — the real flow (replaces the Stage 15 stub)
----------------------------------------------------------
Legacy sells VIP as ordinary ``shop_items`` rows with ``type='vip'``
(``init_default_items`` at ``bot.py:12330-12361`` seeds the four
canonical plans: 1 / 3 / 6 / 12 months). The legacy VIP-shop view
filters the catalog to those rows, renders the perk blurb ("VIP статус
на 30 дней: +1 монета за сообщение, +15% к daily, −50% налог на
переводы", ``bot.py:12332``) and offers a buy button per plan. After
purchase the legacy auto-apply path grants the status
(``apply_vip_status``, ``bot.py:13436``).

The new pipeline already implements every piece of this:

* ``ShopItemsRepo.list_by_type('vip')`` surfaces the buyable plans
  (price-ASC, sold-out hidden) — same semantics as ``/shop``.
* The inline "Buy" buttons carry :class:`ShopBuyPrompt`, the SAME
  callback the ``/shop`` keyboard uses. The shop router's
  prompt → confirm → execute ladder (``handlers/shop.py``) handles
  them dispatcher-wide, so a VIP buy runs through the identical atomic
  debit (``PurchaseService``: ``WHERE balance >= price`` rowcount
  guard, no float money) and writes the identical inventory row.
* The VIP *status* is granted when the user activates that inventory
  row via ``/inventory`` → 🎁 Use, which routes through
  ``InventoryUseService`` → ``VipRepo.grant_global`` (the VIP_GRANT
  planner kind). This matches legacy's "buy item, then it applies"
  shape exactly, just split across the already-ported buy and use
  surfaces instead of a single auto-apply.

So ``/vip_shop`` is a thin discovery + entry-point surface over the
existing economy infra — it adds NO new money path, NO new grant path,
and NO new confirm FSM. It reuses ``ShopBuyPrompt`` so the confirm step
the lead asked about is the one ``/shop`` already ships.

Stage 26 removed the ``/lang`` stub that used to live here: that
command is now a real language switch (see ``handlers/language.py``).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import ShopBuyPrompt
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.repositories.vip_repo import DEFAULT_VIP
from telegram_invite_bot.services.inventory_use_planner import (
    VIP_NAME_DURATIONS,
    declared_vip_duration_days,
)
from telegram_invite_bot.utils.render import clamp_utf16

log = logger.bind(component="handlers.vip")

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import StatsConfig
    from telegram_invite_bot.core.entities.shop import ShopItemEntity
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo
    from telegram_invite_bot.repositories.transactions_repo import (
        TransactionsRepo,
        VoiceUsageStats,
    )
    from telegram_invite_bot.repositories.vip_repo import VipRepo


# ``type`` discriminator for VIP rows in ``shop_items`` — see the legacy
# seed at ``bot.py:12335``. The planner (inventory_use_planner) keys VIP
# grants off this exact string, so the shop surface uses it too.
_VIP_ITEM_TYPE = "vip"

# How many plans one ``/vip_shop`` card may render. ``list_by_type`` has
# no LIMIT, and the card spends four to five lines per plan plus a
# keyboard row — so the row count alone decides whether the message
# clears Telegram's 4096 ceiling, and past it ``reply`` is a 400 the
# user never sees. Unlike ``/filter_list`` this surface is NOT the one
# that must show everything: ``/shop`` already paginates the whole
# catalog with the identical buy buttons, so the overflow has a real
# destination and a window is honest here where truncation elsewhere
# would not be. 8 matches ``shop._PAGE_SIZE`` for the same reason it
# was chosen there — the four canonical plans leave the cap unreachable
# in practice, and an operator who seeds a dozen more gets a readable
# card instead of silence.
_MAX_PLANS = 8
# Ceilings on operator-supplied plan copy (a hand-seeded row falls back
# to the catalog's own ``name`` / ``description`` for RU — see
# :func:`_plan_copy`). Neither column is length-checked on the write
# side, so one verbose row would blow the card on its own; measured in
# UTF-16 units because that is what Telegram counts.
_PLAN_NAME_MAX = 48
_PLAN_DESC_MAX = 160

# Terms that have curated ``h_vip_plan_name_{days}`` / ``…_desc_{days}``
# copy in both locales. #192: the term a plan GRANTS and the term a plan
# has CURATED COPY for used to be the same question, because the planner
# refused to grant anything it couldn't name. Now
# :func:`resolve_vip_duration_days` answers the first for every row
# (production's only VIP SKU is called ``👑 VIP статус`` and carries its
# 30 days in ``data``), so the second needs its own answer — otherwise
# the card would ask i18n for a key like ``h_vip_plan_name_14`` and get
# the raw key back. This file therefore reads
# :func:`declared_vip_duration_days`, which says ``None`` rather than
# handing back the planner's 30-day default: a term nobody declared is
# not a term worth printing, comparing or discounting against.
_CURATED_TERMS: frozenset[int] = frozenset(VIP_NAME_DURATIONS.values())

# NOTE: ``/emojis /emoji_set /emoji_buy /emoji_preview /эмодзи`` left this
# static-stub file when they became the real VIP cosmetic-badge feature
# (#25) — their handlers now live in ``handlers/emoji.py`` over
# ``EmojiBadgeService``. Same move ``/voice_stats`` made when it became
# ledger-backed; the wiring assertion lives in test_main_router_wiring.py.

# The VIP copy that used to live here as Russian module constants
# (``_VIP`` / ``_VIP_SHOP_HEADER`` / ``_VIP_SHOP_EMPTY`` / ``_VOICE_SETTINGS``
# / ``_VOICE_STATS_*``) moved into the i18n layer (``h_vip*`` / ``h_voice*``
# keys) so EN users stop seeing Russian. RU values are byte-identical to
# the old literals — see the data YAMLs.


async def handle_vip(message: Message, vip_repo: VipRepo, lang: str, *, tz: ZoneInfo) -> None:
    """Render the live VIP status card.

    Restores the legacy ``/vip`` richness (bot.py:13511): a real
    active/inactive state, the concrete expiry date + days-left when
    active, and the actual perk *values* pulled from :data:`DEFAULT_VIP`
    so the advertised numbers can never drift from the payout the economy
    middleware really applies. Inactive callers get the same perk list
    framed as an offer, so the card always shows what VIP concretely buys.

    #1957: ``tz`` is the configured display zone (``STATS_TIMEZONE``),
    and it is required — not defaulted — because the two things this
    card prints are the two things that used to disagree with
    ``/profile``. Both cards read the SAME ``users.vip_till``, a unix
    timestamp, so the stored value was never in question; what differed
    was the frame each one rendered it in. This card used UTC while
    ``profile.py::_vip_line`` used ``ZoneInfo(stats_config.timezone)``,
    so on the prod host (MSK, UTC+3) a grant expiring between 00:00 and
    03:00 local printed one calendar date under ``/vip`` and the next
    one under ``/profile``. The day count disagreed on top of that:
    ``max(1, ceil(remaining / 86400))`` here against a calendar-date
    difference there, so the last day of a grant read "1 дн." on one
    card and "0 дн." on the other. Legacy settles both: ``bot.py:40240``
    formats with a naive ``fromtimestamp`` (host local, i.e. MSK) and
    floors the remainder, so UTC and the ``ceil`` were the outliers.
    Both now match ``/profile``.
    """
    uid = message.from_user.id if message.from_user else 0
    now = datetime.now(tz)
    vip_till = await vip_repo.get_vip_till(uid)
    perks = DEFAULT_VIP
    # Frame-independent: both sides are epoch seconds, so the liveness
    # check is unaffected by which zone the card renders in.
    if vip_till is not None and vip_till > now.timestamp():
        till_dt = datetime.fromtimestamp(vip_till, tz)
        days_left = max(0, (till_dt.date() - now.date()).days)
        text = t(
            "h_vip_active",
            lang,
            till=till_dt.strftime("%d.%m.%Y"),
            days=days_left,
            msg_bonus=perks.message_bonus,
            daily=perks.daily_bonus_percent,
            tax=perks.tax_discount_percent,
        )
    else:
        text = t(
            "h_vip",
            lang,
            msg_bonus=perks.message_bonus,
            daily=perks.daily_bonus_percent,
            tax=perks.tax_discount_percent,
        )
    await message.reply(text)


def _plan_copy(item: ShopItemEntity, lang: str) -> tuple[str, str]:
    """Curated, localized ``(name, description)`` for a VIP plan row.

    RR-2 #21. The catalog stores Russian operator text in ``name`` /
    ``description``, so the port's "just escape the DB columns" render
    showed EN users a Russian card — and showed RU users the terse seed
    blurb instead of the perk list. Legacy solved this with a
    duration-keyed display table (``_shop_item_display``, bot.py:23640);
    we key off :func:`declared_vip_duration_days` — the same reader the
    effect planner starts from — so a card can never advertise a term
    the grant won't honour.

    A row whose term has no curated copy has no curated copy: RU
    falls back to the operator's own text — it is written for RU users
    and is the most accurate thing we have — while EN gets the generic
    plan blurb rather than a wall of Cyrillic, exactly the trade legacy
    made at bot.py:23663.

    Returns RAW text — no HTML escaping. The card escapes at render time
    (parse_mode is HTML); the inline keyboard must not, because button
    labels are plain text and would show a literal ``&amp;``. Curated
    copy therefore carries no tags of its own: the ``<b>`` wrapper lives
    in the surrounding ``h_vip_plan_head`` template. The 👑 DOES live in
    the name (not the template) so the card and the button — which
    cannot wrap anything — read identically, and so the operator-text
    fallback, which already carries its own crown, doesn't get a second.
    """
    days = declared_vip_duration_days(item)
    if days is None or days not in _CURATED_TERMS:
        if lang == "en":
            return t("h_vip_plan_name_other", lang), t("h_vip_plan_desc_other", lang)
        # Operator text, unbounded on the write side — clamp it so a
        # single verbose row cannot push the card past 4096. Curated
        # copy below is authored here and needs no clamp.
        return (
            clamp_utf16(item.name, _PLAN_NAME_MAX),
            clamp_utf16(item.description, _PLAN_DESC_MAX),
        )
    perks = DEFAULT_VIP
    return (
        t(f"h_vip_plan_name_{days}", lang),
        t(
            f"h_vip_plan_desc_{days}",
            lang,
            days=days,
            msg_bonus=perks.message_bonus,
            daily=perks.daily_bonus_percent,
            tax=perks.tax_discount_percent,
        ),
    )


def _saving_percent(item: ShopItemEntity, *, base_per_day: float | None) -> int:
    """How much cheaper per day this plan is than the yardstick, in %.

    The yardstick is whatever :func:`_base_per_day` picked — the
    SHORTEST plan the catalog actually offers, which is the monthly one
    only as long as the operator ships a monthly one. The old name and
    first line of this docstring both said "monthly" flatly (#1478),
    which is a promise about the catalog that the code never makes and
    the operator can withdraw from the shop panel at any moment.

    ``0`` when there is nothing to compare against (no shorter plan in
    the catalog), when the row declares no term of its own, or when the
    plan is not actually cheaper — the badge must never advertise a
    discount that isn't there. Rounded DOWN so the card never overstates
    the saving.
    """
    days = declared_vip_duration_days(item)
    if base_per_day is None or base_per_day <= 0 or days is None or days <= 0:
        return 0
    per_day = item.price / days
    if per_day >= base_per_day:
        return 0
    return int((1 - per_day / base_per_day) * 100)


def _base_per_day(items: list[ShopItemEntity]) -> float | None:
    """Price-per-day of the shortest plan on offer.

    That plan is the yardstick every longer one is discounted against.
    Derived from the rendered list rather than hardcoded so the
    yardstick is always a plan the user can actually buy: drop the
    30-day SKU from the catalog and the next-shortest term takes over
    as the base, rather than every card being discounted against a
    price nobody is offering (#1478 — this used to claim that a catalog
    without the 30-day SKU produces no badges, which is only true when
    it also leaves fewer than two dated plans). A catalog with exactly
    one plan (which is what production ships) compares it against
    itself and shows no badge at all. Rows that declare no term
    are left out entirely: their length is the planner's default, not
    the operator's statement, and a yardstick built on it would discount
    every real plan against a number nobody wrote down.
    """
    terms = [
        (days, item.price)
        for item in items
        if (days := declared_vip_duration_days(item)) is not None and days > 0
    ]
    if not terms:
        return None
    days, price = min(terms)
    return price / days if days > 0 else None


def _format_vip_shop(items: list[ShopItemEntity], lang: str, *, hidden: int = 0) -> str:
    """Render the VIP-plan cards under the perk header.

    Mirrors ``handlers/shop.py:_format_shop`` posture: the full plan
    list is rendered as text (so users on clients that hide inline
    keyboards still see every plan + a ``/buy <id>`` fallback) and every
    admin-supplied string that survives into the card is routed through
    :func:`html.escape` because parse_mode is HTML.

    RR-2 #21 restores what the port's one-line-per-row list dropped: the
    curated per-plan perk blurb (see :func:`_plan_copy`) and the
    remaining stock. The saving badge is new — legacy priced the long
    plans at a discount but never said so, which is the whole reason a
    12-month SKU exists.

    ``items`` is the already-windowed slice the caller also builds the
    keyboard from (see ``_MAX_PLANS``); ``hidden`` is how many plans
    that window left out, so the card can say so instead of pretending
    the catalog ends here.
    """
    perks = DEFAULT_VIP
    lines = [
        t(
            "h_vip_shop_header",
            lang,
            msg_bonus=perks.message_bonus,
            daily=perks.daily_bonus_percent,
            tax=perks.tax_discount_percent,
        ),
        "",
    ]
    base = _base_per_day(items)
    for item in items:
        name, desc = _plan_copy(item, lang)
        lines.append(t("h_vip_plan_head", lang, name=html.escape(name), price=item.price))
        if desc:
            lines.append(t("h_vip_plan_desc", lang, desc=html.escape(desc)))
        saving = _saving_percent(item, base_per_day=base)
        if saving > 0:
            lines.append(t("h_vip_plan_saving", lang, percent=saving))
        # ``-1`` is the infinite sentinel and ``0`` rows are filtered out
        # upstream, so a positive count is exactly the "limited batch"
        # case worth surfacing — legacy hid the stock line for unlimited
        # VIP rows for the same reason (bot.py:23726-23727).
        if item.stock > 0:
            lines.append(t("h_vip_plan_stock", lang, count=item.stock))
        lines.append(t("h_vip_shop_buy_line", lang, id=item.id))
        lines.append("")
    if hidden > 0:
        lines.append(t("h_vip_shop_more", lang, count=hidden))
    return "\n".join(lines).rstrip()


def _build_vip_shop_keyboard(items: list[ShopItemEntity], lang: str) -> InlineKeyboardMarkup:
    """One "Buy" button per VIP plan, carrying :class:`ShopBuyPrompt`.

    Reuses the exact callback the ``/shop`` keyboard emits so the
    purchase runs through the shop router's already-wired
    prompt → confirm → execute ladder (atomic debit via
    ``PurchaseService``). No VIP-specific callback is introduced — the
    button is the same shape ``/shop`` produces, just rendered from the
    type-filtered plan list.

    Sold-out rows never reach here (``list_by_type`` hides ``stock == 0``
    by default), but we skip ``stock == 0`` defensively for the same
    reason ``_build_shop_keyboard`` does: an admin toggling stock to 0
    between the repo read and this render would otherwise ship a button
    that only ever produces an out-of-stock toast.

    RR-2 #21: the label uses the same curated, localized plan name the
    card shows (:func:`_plan_copy`) instead of the raw Russian catalog
    column — an EN user was previously offered "👑 VIP (3 месяца)".
    Button labels are plain text, so the name goes in unescaped.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        if item.stock == 0:
            continue
        name, _desc = _plan_copy(item, lang)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{name[:28]} · {item.price} 🪙",
                    callback_data=ShopBuyPrompt(item_id=item.id).pack(),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def handle_vip_shop(message: Message, shop_items_repo: ShopItemsRepo, lang: str) -> None:
    """List buyable VIP plans and offer an inline buy per plan.

    Reads ``shop_items`` filtered to ``type='vip'`` (price-ASC, sold-out
    hidden) and renders the perk header + a card per plan (name, perk
    blurb, saving badge, remaining stock — RR-2 #21). The buy buttons
    reuse :class:`ShopBuyPrompt`, so the shop router's confirm / debit
    flow owns the actual purchase — see the module docstring.
    """
    items = await shop_items_repo.list_by_type(_VIP_ITEM_TYPE)
    if not items:
        await message.reply(t("h_vip_shop_empty", lang))
        return
    # ONE slice feeds both surfaces. Windowing the keyboard while the
    # body listed every row (or the reverse) is the failure /shop
    # already documents: plans you can read but not tap, or buttons for
    # plans you were never shown.
    shown = items[:_MAX_PLANS]
    await message.reply(
        _format_vip_shop(shown, lang, hidden=len(items) - len(shown)),
        reply_markup=_build_vip_shop_keyboard(shown, lang),
    )
    log.bind(
        uid=message.from_user.id if message.from_user else None,
        plans=len(items),
        shown=len(shown),
    ).info("/vip_shop rendered")


async def handle_voice_settings(message: Message, lang: str) -> None:
    await message.reply(t("h_voice_settings", lang))


async def handle_voice_vip(message: Message, lang: str) -> None:
    """``/voice_vip`` — static explainer for the VIP ``/voice`` (TTS) perk.

    Backlog L-69/L-73: legacy ``/voice_vip`` advertised VIP *speech-to-text*
    transcription, which the new pipeline does not yet port (STT is the
    deferred L-70 epic). Rather than advertise a feature that doesn't
    exist, this command describes what VIP voice actually buys today:
    text-to-speech via ``/voice``, with the top-tier free-voice gift.
    Discovery only — no VIP gate, so non-VIPs see what they'd unlock.
    """
    await message.reply(t("h_voice_vip_info", lang))


def _format_voice_stats(stats: VoiceUsageStats, lang: str) -> str:
    """Render the voice-usage card from the netted aggregate.

    All three fields are ints from the repo, so there's no admin-supplied
    text to HTML-escape here (unlike ``_format_vip_shop``) — but the card
    is still HTML (default parse_mode), hence the ``<b>`` markup. Refunds
    are already netted out in :meth:`TransactionsRepo.voice_usage_stats`,
    so ``total`` is "voices that actually produced audio".
    """
    return t(
        "h_voice_stats_card",
        lang,
        total=stats.total,
        today=stats.today,
        coins_spent=stats.coins_spent,
    )


async def handle_voice_stats(
    message: Message, transactions_repo: TransactionsRepo, lang: str
) -> None:
    """Render a data-backed ``/voice`` usage card from the ledger.

    Reads the user's ``type='tts'`` debits netted against any
    ``type='tts_refund'`` credits (see
    :meth:`TransactionsRepo.voice_usage_stats`) and shows lifetime count,
    today's count, and net coins spent. Falls back to a friendly
    zero-state when the user has never (net) used /voice. ``EconomyMiddleware``
    injects ``transactions_repo`` — same seam ``/vip_shop`` uses for
    ``shop_items_repo``. Private-only (router filter).
    """
    user = message.from_user
    if user is None:
        await message.reply(t("h_voice_stats_empty", lang))
        return
    stats = await transactions_repo.voice_usage_stats(user.id)
    if stats.total <= 0:
        await message.reply(t("h_voice_stats_empty", lang))
        return
    await message.reply(_format_voice_stats(stats, lang))
    log.bind(uid=user.id, total=stats.total).info("/voice_stats rendered")


def build_router(registry: EngineRegistry, stats_config: StatsConfig) -> Router:
    """Factory — fresh ``Router`` + middleware per call so tests can re-wire.

    Takes ``registry`` (Stage 15 took no args) so the ``/vip_shop``
    handler can receive ``shop_items_repo`` from :class:`EconomyMiddleware`
    — the same middleware ``handlers/shop.py`` mounts. The ``ShopBuyPrompt``
    callbacks the VIP keyboard emits are handled by the shop router's own
    callback observer (registered dispatcher-wide), so we do NOT re-register
    them here; mounting EconomyMiddleware on the message side alone is
    enough for the read-side ``/vip_shop`` render. The other five handlers
    are static-text stubs that ignore the injected ``data``.
    """
    router = Router(name="vip")
    # Private-chat-only feature — a group call gets the #123 refusal twin.
    # Router-level filter enforces this once for every handler below.
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.middleware(EconomyMiddleware(registry))
    # #1957: same shape as ``mygroups``' STATS_TIMEZONE capture — the
    # zone is config, not per-update state, so it is bound once here
    # rather than injected through the middleware chain.
    tz = ZoneInfo(stats_config.timezone)

    async def _vip(message: Message, vip_repo: VipRepo, lang: str) -> None:
        await handle_vip(message, vip_repo, lang, tz=tz)

    router.message.register(
        _vip,
        Command("vip", ignore_case=True),
    )
    router.message.register(
        handle_vip_shop,
        Command("vip_shop", ignore_case=True),
    )
    router.message.register(
        handle_voice_settings,
        # L-09: legacy registered the RU alias ``voice_settings_ru``
        # alongside the EN token (``command_aliases.py:596-598``,
        # aliases ['voice_settings', 'voice_settings_ru']); the new
        # surface only carried the EN one.
        Command("voice_settings", "voice_settings_ru", ignore_case=True),
    )
    router.message.register(
        handle_voice_stats,
        Command("voice_stats", ignore_case=True),
    )
    router.message.register(
        handle_voice_vip,
        Command("voice_vip", "голос_вип", ignore_case=True),
    )
    return with_chat_type_refusal(router, scope="private")
