"""Private ``/start`` main-menu navigation (A-01 Part B).

The bare private ``/start`` welcome carries an inline keyboard
(:func:`main_menu_keyboard`) with the core read surfaces. Tapping a
button routes here: a single :func:`handle_menu` callback dispatches on
:class:`MainMenu.action` and **edits the welcome message in place**, so
one message becomes the whole menu. Every destination shows a "⬅️ back"
button that returns to the menu.

Why re-render helpers instead of the command handlers? In a callback,
``callback.message.from_user`` is the *bot*, not the tapper — the
command handlers (which read ``message.from_user`` via
``require_from_user``) would resolve the wrong user. So we call the
pure render helpers (``economy._format_balance``,
``profile._format_text``, ``referral._format``) directly with
``callback.from_user``. The menu is public-safe: every destination
renders the *tapping* user's own data, so a bystander tap on another
member's menu only ever shows the bystander their own card.

Destinations are deliberately limited to zero/low-write reads that have
a clean reusable helper. ``/daily`` is excluded (it's a mutating claim —
a menu tap must never silently claim the reward); group leaderboards and
the shop FSM stay command-only for now.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from loguru import logger

from telegram_invite_bot.handlers.commission import _fetch_earnings
from telegram_invite_bot.handlers.commission import _render as _render_commission
from telegram_invite_bot.handlers.economy import _format_balance
from telegram_invite_bot.handlers.help_catalog import render_help_pages
from telegram_invite_bot.handlers.profile import _format_text
from telegram_invite_bot.handlers.referral import _bot_username, _format
from telegram_invite_bot.handlers.shop import _format_shop, _wallet_balance
from telegram_invite_bot.handlers.start_cards import (
    display_name,
    owns_groups,
    render_welcome_back,
    role_icon,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu
from telegram_invite_bot.keyboards.builders.rating import RatingNav
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.utils.aiogram import edit_card
from telegram_invite_bot.utils.daily import daily_cooldown_remaining
from telegram_invite_bot.utils.numbers import is_int_token
from telegram_invite_bot.utils.time import db_now, local_now

log = logger.bind(component="handlers.main_menu")

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo
    from telegram_invite_bot.services.user_service import UserService


def main_menu_keyboard(lang: str, *, add_group_url: str | None = None) -> InlineKeyboardMarkup:
    """The navigation keyboard attached to the private ``/start`` welcome.

    A-01 Part B shipped 3 buttons (profile/balance/referral); RR-59
    restores the legacy ``build_main_menu_keyboard`` breadth — every core
    read surface reachable with one tap. Each destination is live: the
    shop/help/commission taps edit the welcome in place via a reusable
    render helper and games/daily via a static i18n card, the 🏆 rating
    tap reuses the rating router's
    own :class:`RatingNav` leaderboard (no duplicate query), and — when a
    deep link is resolved (``add_group_url``) — an "add me to a group"
    URL button closes the acquisition loop. No dead buttons: the mutating
    ``/daily`` claim is deliberately a read-only *hint* here, never an
    auto-claim on tap.
    """

    def _cb(action: str) -> str:
        return MainMenu(action=action).pack()

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=t("h_menu_btn_profile", lang), callback_data=_cb("profile")),
            InlineKeyboardButton(text=t("h_menu_btn_balance", lang), callback_data=_cb("balance")),
        ],
        [
            InlineKeyboardButton(text=t("h_menu_btn_shop", lang), callback_data=_cb("shop")),
            InlineKeyboardButton(text=t("h_menu_btn_games", lang), callback_data=_cb("games")),
        ],
        [
            # 🏆 rating reuses the rating router's leaderboard callback —
            # same pattern as /groupstats (#7): no duplicate render code.
            InlineKeyboardButton(
                text=t("h_menu_btn_rating", lang),
                callback_data=RatingNav(page=1).pack(),
            ),
            InlineKeyboardButton(text=t("h_menu_btn_help", lang), callback_data=_cb("help")),
        ],
        [
            InlineKeyboardButton(
                text=t("h_menu_btn_referral", lang), callback_data=_cb("referral")
            ),
            InlineKeyboardButton(
                text=t("h_menu_btn_commission", lang), callback_data=_cb("commission")
            ),
        ],
        [
            InlineKeyboardButton(text=t("h_menu_btn_daily", lang), callback_data=_cb("daily")),
        ],
    ]
    if add_group_url:
        rows.append([InlineKeyboardButton(text=t("h_menu_btn_addgroup", lang), url=add_group_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def welcome_keyboard(lang: str, bot: Bot) -> InlineKeyboardMarkup:
    """Resolve the bot @username and build the full welcome menu.

    Split from :func:`main_menu_keyboard` so the sync keyboard builder
    stays pure (used by the in-place ``home`` re-render, which already
    has the username via the callback ``bot``) while the two welcome
    entry points can pay the one cached ``get_me`` for the URL button.
    """
    username = await _bot_username(bot)
    add_group_url = f"https://t.me/{username}?startgroup=true"
    return main_menu_keyboard(lang, add_group_url=add_group_url)


def _back_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Single "⬅️ back" button returning to the main menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_menu_btn_back", lang),
                    callback_data=MainMenu(action="home").pack(),
                )
            ]
        ]
    )


#: Page number encoded *into* the action string rather than added as a
#: second :class:`MainMenu` field. A new field changes the packed shape
#: of **every** menu button (``menu:profile`` → ``menu:profile:1``), and
#: the welcome messages already sitting in users' chats carry the old
#: shape — after a deploy their taps would fail to unpack and silently
#: do nothing. Page 1 keeps the bare ``"help"`` action for exactly that
#: reason: an old button still lands on the card it always did.
_HELP_ACTION: Final[str] = "help"


def _help_action(page: int) -> str:
    """``help`` for page 1, ``help2``/``help3``/… beyond it."""
    return _HELP_ACTION if page <= 1 else f"{_HELP_ACTION}{page}"


def _help_page(action: str, total: int) -> int:
    """Parse a help action back into a 1-based page, clamped to ``total``.

    Callback data is user-supplied — ``menu:help999`` is one edited
    button away — so the parse never trusts the number: anything that
    isn't a plain positive integer inside the current page count folds
    back onto the nearest real page instead of raising into the global
    error router. ``is_int_token`` rather than ``str.isdigit`` because
    128 code points pass the latter and then raise inside ``int()``
    (#102).
    """
    suffix = action[len(_HELP_ACTION) :]
    if not is_int_token(suffix):
        return 1
    return max(1, min(int(suffix), total))


def _help_keyboard(lang: str, *, page: int, total: int) -> InlineKeyboardMarkup:
    """ "back", plus prev/next when the catalog spans several pages.

    The nav row is omitted entirely on a single-page card — the same
    call made for the rating board (#49): a lone disabled-looking arrow
    that goes nowhere reads as a broken button. The ``📄 n/N`` marker
    the renderer already puts in the body carries the position, so the
    row needs no counter button of its own.

    Paging uses ``◀️``/``▶️`` rather than the ``⬅️``/``➡️`` most other
    pagers in this codebase use, because this card is the one that also
    carries a ``⬅️ Назад`` row: from page 2 onward the two identical
    left arrows sat one above the other and nothing told the user which
    of them left the help view. ``handlers/admin/withdrawals.py`` uses
    the same pair.
    """
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(
            InlineKeyboardButton(
                text="◀️", callback_data=MainMenu(action=_help_action(page - 1)).pack()
            )
        )
    if page < total:
        nav.append(
            InlineKeyboardButton(
                text="▶️", callback_data=MainMenu(action=_help_action(page + 1)).pack()
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text=t("h_menu_btn_back", lang),
                callback_data=MainMenu(action="home").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def handle_menu(
    callback: CallbackQuery,
    callback_data: MainMenu,
    user_service: UserService,
    economy_repo: EconomyRepo,
    shop_items_repo: ShopItemsRepo,
    bot: Bot,
    settings: Settings,
    registry: EngineRegistry,
) -> None:
    """Dispatch a main-menu tap → edit the message into the chosen card.

    The acting user is always ``callback.from_user`` — never
    ``callback.message.from_user`` (the bot). We ``touch`` to refresh
    ``last_seen`` and pick up the user's language, mirroring the
    side effect every legacy handler had.

    Every destination renders the *tapping* user's own data (public-safe:
    a bystander tap on someone else's menu only ever shows the bystander
    their own card). All reads are zero/low-write — the ``/daily`` claim
    stays a read-only hint (RR-59), never a silent claim-on-tap.
    """
    tapper = callback.from_user
    user = await user_service.touch(tapper)
    lang = user.language
    action = callback_data.action

    if action == "home":
        # One renderer for both entry points (RR-6 #60): a "back" tap must
        # land on exactly the card ``/start`` showed, refreshed. Lives in
        # the leaf module ``start_cards`` because ``handlers.start``
        # already imports this module — a direct import would cycle.
        # ``start_cards.display_name`` HTML-escapes: the dispatcher
        # sends with parse_mode=HTML, so a first_name/username
        # carrying markup would otherwise render as live HTML (SEC
        # audit). The module prefix is load-bearing — there is a
        # second ``display_name`` in ``utils/names.py`` that returns
        # the raw string on purpose, for callers that escape it
        # themselves or render it as button text.
        wallet = await economy_repo.get_or_create(user.user_id, language=lang)
        text = render_welcome_back(
            lang,
            name=display_name(tapper, lang),
            role=role_icon(
                is_developer=settings.bot.is_developer(user.user_id),
                owns_groups=await owns_groups(registry, user.user_id),
            ),
            now=local_now(user.timezone),
            balance=wallet.balance,
            games_played=wallet.games_played,
            streak=wallet.daily_streak,
            cooldown=daily_cooldown_remaining(wallet.last_daily, db_now()),
        )
        # The ``home`` re-render carries the full welcome menu (URL button
        # included) — the callback ``bot`` resolves the @username cheaply
        # (aiogram caches ``get_me``).
        markup = await welcome_keyboard(lang, bot)
    elif action == "profile":
        text = _format_text(user)
        markup = _back_keyboard(lang)
    elif action == "balance":
        wallet = await economy_repo.get_or_create(user.user_id)
        text = _format_balance(wallet, lang)
        markup = _back_keyboard(lang)
    elif action == "referral":
        username = await _bot_username(bot)
        percent = settings.economy.referral_commission_percent
        text = _format(lang, username=username, user_id=user.user_id, percent=percent)
        markup = _back_keyboard(lang)
    elif action == "shop":
        items = await shop_items_repo.list_all()
        if items:
            balance = await _wallet_balance(economy_repo, user.user_id)
            text = _format_shop(items, lang, page=0, balance=balance)
        else:
            text = t("h_shop_empty", lang)
        markup = _back_keyboard(lang)
    elif action == "games":
        text = t("h_games_menu_card", lang)
        markup = _back_keyboard(lang)
    elif action.startswith(_HELP_ACTION):
        # No footer button in the in-place card — our own "back" is the
        # only control, so ``has_button=False`` keeps the copy honest.
        # Plain-user view only: a menu tap carries no chat-admin context,
        # and a member must not read the moderation thresholds off it.
        pages = render_help_pages(lang, has_button=False)
        # ``edit_text`` holds exactly one message, and the plain-user
        # catalog outgrew one message the moment the 74 uncatalogued
        # commands were added (#114). So the card pages *in place*: the
        # arrows re-enter this branch with the next page number and the
        # menu stays one message, which is the whole point of it. The
        # earlier fallback — page 1 plus "type /help for the rest" —
        # was cheaper but hid a full category (``/ai``, ``/support``,
        # ``/marry`` …) behind a command the button exists to replace.
        page = _help_page(action, len(pages))
        text = pages[page - 1]
        markup = _help_keyboard(lang, page=page, total=len(pages))
    elif action == "commission":
        total = await _fetch_earnings(registry, user.user_id)
        percent = settings.economy.referral_commission_percent
        text = _render_commission(lang, total=total, percent=percent)
        markup = _back_keyboard(lang)
    elif action == "daily":
        # Read-only hint — a menu tap must NEVER claim the daily reward
        # (that stays an explicit ``/daily`` command). Just point the way.
        text = t("h_menu_daily_hint", lang)
        markup = _back_keyboard(lang)
    else:  # pragma: no cover - closed vocabulary, defensive only
        await callback.answer()
        return

    message = callback.message
    if isinstance(message, Message):
        # ``edit_card`` rather than a raw edit. Every branch above renders
        # a card that is a pure function of ``action`` plus the user's
        # current state, so tapping the same button twice — the most
        # common thing that happens to a menu — produces identical text
        # and Telegram answers "message is not modified". Raw, that
        # bubbles to the global error router and the user is shown
        # "⚠️ Произошла ошибка" for a tap that had nothing to change.
        await edit_card(message, text, reply_markup=markup, disable_web_page_preview=True)
    await callback.answer()
    log.bind(uid=user.user_id, action=action).info("main-menu tap")


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — the menu callbacks live on a dedicated router.

    ``EconomyMiddleware`` is mounted on ``callback_query`` (not
    ``message``) because the only entry points here are callback taps
    and the ``balance`` destination needs ``economy_repo``. ``settings``
    is closed over for the referral commission percent (same pattern as
    :mod:`~handlers.referral`). The PRIVATE filter mirrors where the
    menu is shown — the welcome keyboard is only attached to private
    ``/start``.
    """
    router = Router(name="main_menu")
    router.callback_query.middleware(EconomyMiddleware(registry))
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    async def _entry(
        callback: CallbackQuery,
        callback_data: MainMenu,
        user_service: UserService,
        economy_repo: EconomyRepo,
        shop_items_repo: ShopItemsRepo,
        bot: Bot,
    ) -> None:
        await handle_menu(
            callback,
            callback_data,
            user_service,
            economy_repo,
            shop_items_repo,
            bot,
            settings,
            registry,
        )

    router.callback_query.register(_entry, MainMenu.filter())
    return router
