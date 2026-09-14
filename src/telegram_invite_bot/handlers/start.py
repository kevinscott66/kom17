"""``/start`` handler — bare welcome + ``ref_<id>`` referral onboarding.

Originally the Stage-4 pilot of the strangler migration; the legacy
bridge that previously absorbed unmatched ``/start`` variants was
removed in T-011 and those variants are now served by their own
dedicated handlers / routers.

Three entry points live here:

* **bare ``/start``** → the onboarding card for new users / the
  greeting + balance dashboard for returning ones. Both are rendered by
  the leaf module :mod:`~handlers.start_cards` (RR-6 #60), which
  ``handlers.main_menu`` also calls so a "⬅️ back" tap lands on exactly
  the card ``/start`` showed.
* **``/start ref_<id>``** (referral deep link, T-031) → the
  invite-funnel restoration. The legacy monolith
  (``bot.py:cmd_start`` around line 16292) recorded the inviter on a
  *new* user's first launch and pinged the inviter. The port restores
  exactly that: attribution is written to ``economy.users.referred_by``
  (first-attribution-wins) and the inviter gets a best-effort DM.

* **``/start grp_<chat_id>``** (#1926) → the same welcome card, plus
  the memory of which group the person came from. Every group→DM button
  in the bot carries this payload now
  (:mod:`~telegram_invite_bot.core.deep_links`), so a DM opened from a
  chat knows its chat instead of starting blank.

Other deep-link payloads (``check_…``) are matched by the routers that
own each domain — ``CommandStart(deep_link=True,
magic=F.args.startswith(...))`` keeps the namespaces from colliding.

Commission-on-purchase (legacy ``_apply_referral_commission``) is a
*separate* deferred gap (payments_service.py): this handler only owns
the attribution write + the signup ping, mirroring the split legacy
already had between ``cmd_start`` and the purchase path.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from telegram_invite_bot.core.deep_links import (
    GROUP_PREFIX,
    dm_start_url,
    parse_group_payload,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers.main_menu import welcome_keyboard
from telegram_invite_bot.handlers.start_cards import (
    display_name,
    owns_groups,
    render_group_welcome,
    render_welcome_back,
    render_welcome_new,
    role_icon,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.repositories.bot_groups_repo import BotGroupsRepo
from telegram_invite_bot.repositories.economy_repo import WELCOME_BALANCE
from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.daily import daily_cooldown_remaining
from telegram_invite_bot.utils.numbers import is_int_token
from telegram_invite_bot.utils.time import db_now, local_now

log = logger.bind(component="handlers.start")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.core.entities.user import User as UserEntity
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.services.user_service import UserService


async def _send_welcome(
    message: Message,
    user: UserEntity,
    bot: Bot,
    *,
    registry: EngineRegistry,
    settings: Settings,
    economy_repo: EconomyRepo,
    name: str,
    suffix: str = "",
    checkpoint: Checkpoint | None = None,
) -> None:
    """Send the new/returning welcome with the private main-menu keyboard.

    RR-6 #60 restores the two distinct cards legacy had:

    * **new user** → the feature tour (``get_welcome_text_new_user``).
      Legacy hung four buttons off it — *tour*, *features*, *bonus*,
      *add to group*. Only the last has an owner in the new pipeline, so
      instead of shipping three dead callbacks we attach the full live
      main menu (which already contains the add-to-group URL button and
      a real ``/daily`` hint) — strictly more reachable surface than
      legacy's four.
    * **returning user** → the greeting + balance widget dashboard
      (``get_greeting`` + ``get_balance_widget``).

    The wallet is fetched with ``get_or_create`` rather than ``get``: a
    first-ever ``/start`` must actually *seed* the signup balance the new
    user's card promises. Legacy did the same implicitly (its
    ``get_balance`` bootstrapped the row).
    """
    wallet = await economy_repo.get_or_create(user.user_id, language=user.language)
    # #1983: this is the last write of every ``/start`` path, and what
    # follows is the owner probe, the keyboard build (which asks Telegram
    # for the bot's username) and the send. Both ``users.db`` (the
    # ``touch`` above every caller) and ``economy.db`` (this seeding, plus
    # a referral attribution on the ``ref_`` path) were held across all of
    # it. The seeding is the signup credit the card is about to promise —
    # it must stand however the send goes — so ending the transaction here
    # is exactly what :class:`db.session.Checkpoint` is for. ``wallet``
    # stays readable: the sessionmakers use ``expire_on_commit=False``.
    if checkpoint is not None:
        await checkpoint()
    if user.is_new:
        body = render_welcome_new(user.language, name=name, bonus=WELCOME_BALANCE)
    else:
        body = render_welcome_back(
            user.language,
            name=name,
            role=role_icon(
                is_developer=settings.bot.is_developer(user.user_id),
                owns_groups=await owns_groups(registry, user.user_id),
            ),
            # Legacy bucketed on the *server's* local hour; honour the
            # user's own ``/timezone`` instead (UTC when unset).
            now=local_now(user.timezone),
            balance=wallet.balance,
            games_played=wallet.games_played,
            streak=wallet.daily_streak,
            # ``last_daily`` is stored naive-UTC (written at
            # ``economy_repo.mark_daily_claimed``), so the comparison
            # anchor must be ``db_now()``, not local time. Legacy wrote
            # the same column as naive *local* time (Moscow on this
            # host), so a row last touched by legacy reads three hours
            # early — self-healing after the first claim on this path.
            cooldown=daily_cooldown_remaining(wallet.last_daily, db_now()),
        )
    await message.answer(
        body + suffix,
        reply_markup=await welcome_keyboard(user.language, bot),
        disable_web_page_preview=True,
    )


async def handle_start(
    message: Message,
    user_service: UserService,
    economy_repo: EconomyRepo,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    checkpoint: Checkpoint | None = None,
) -> None:
    # Bare ``/start`` only — the ``ref_…`` payload variant is matched by
    # ``handle_start_ref`` below (``magic=F.args.is_(None)`` here excludes
    # every payload form).
    tg_user = require_from_user(message)
    user = await user_service.touch(tg_user)
    await _send_welcome(
        message,
        user,
        bot,
        registry=registry,
        settings=settings,
        economy_repo=economy_repo,
        name=display_name(tg_user, user.language),
        checkpoint=checkpoint,
    )
    log.bind(
        uid=user.user_id,
        is_new=user.is_new,
        lang=user.language,
    ).info("/start handled")


async def handle_start_ref(
    message: Message,
    command: CommandObject,
    user_service: UserService,
    economy_repo: EconomyRepo,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/start ref_<id>`` — record the inviter for a brand-new user.

    Attribution conditions mirror legacy ``cmd_start`` (bot.py:16305):

    * the launching user must have **no wallet yet** — an existing user
      clicking a fresh referral link keeps their original inviter (or
      none). Legacy asked exactly this (``_user_exists_in_economy``,
      bot.py:15892 / bot.py:16258); see the note at the gate for why
      ``user.is_new`` is not the same question;
    * ``referrer_id`` parses as a positive int and isn't the user
      themselves (self-referral guard);
    * the referrer has a wallet (``economy_repo.get`` not ``None``) — a
      ``ref_`` payload pointing at a stranger who never used the bot is
      ignored, matching legacy ``_user_exists_in_economy``.

    On a successful first attribution the inviter gets a best-effort DM
    (a delivery failure — inviter blocked the bot — must not break the
    new user's onboarding). The new user always gets the welcome.
    """
    tg_user = require_from_user(message)
    user = await user_service.touch(tg_user)
    name = display_name(tg_user, user.language)

    payload = command.args or ""
    ref_raw = payload[len("ref_") :].strip() if payload.startswith("ref_") else ""
    attributed = False
    # #240: ``user.is_new`` is the wrong gate here. It asks ``users.db
    # users`` — a bot-profile row (users_repo.py:248) — while legacy
    # asked ``economy.db users``, a WALLET (bot.py:15892, bot.py:16258).
    # The two tables drifted apart in the port: legacy wrote both on
    # every group message (bot.py:43825-43826), whereas the group
    # middleware here creates only the wallet
    # (``MessageActivityMiddleware._reward``, gated by ``coins_enabled``
    # plus throttling) and never creates the ``users`` row that
    # ``users_repo.py:248`` asks for. It does write ``users.db`` — the
    # ``user_group_joins`` stamp at :428-474 goes to ``DBName.USERS`` —
    # just not that row. So a long-standing group member who has never opened
    # a DM kept ``is_new=True`` forever and stayed attributable to any
    # inviter — legacy closed that the moment they said one word in the
    # group.
    #
    # Read the wallet BEFORE the ``get_or_create`` further down, which
    # would otherwise make this check trivially false and disable
    # referrals outright. ``user.is_new`` is left alone where it drives
    # the welcome card: that one IS a users.db question.
    had_wallet = await economy_repo.get(user.user_id) is not None
    if not had_wallet and is_int_token(ref_raw):
        referrer_id = int(ref_raw)
        referrer_wallet = await economy_repo.get(referrer_id)
        # The new user's wallet must exist for ``set_referrer``'s UPDATE
        # to land a row. Legacy bootstrapped it (with the welcome credit)
        # before recording the inviter; do the same here so attribution
        # isn't silently lost on a user whose first-ever action is the
        # referral deep link.
        if referrer_id != user.user_id and referrer_wallet is not None:
            await economy_repo.get_or_create(user.user_id, language=user.language)
        if (
            referrer_id != user.user_id
            and referrer_wallet is not None
            and await economy_repo.set_referrer(user.user_id, referrer_id)
        ):
            attributed = True
            wallet_lang = referrer_wallet.language
            ref_lang = wallet_lang if wallet_lang in ("ru", "en") else "ru"
            # #1410: end the write transaction before the DM. The
            # attribution above opened ``economy.db`` as ``BEGIN
            # IMMEDIATE`` (``db/engines.py``), so from ``get_or_create``
            # until the middleware commits this update is the DB's only
            # writer — and the next statement waits on Telegram, which
            # is allowed far longer than SQLite's 5 s ``busy_timeout``.
            # An inviter who has blocked the bot, or a Telegram stall,
            # therefore froze every other user's wallet write behind one
            # new user's ``/start``. See :class:`db.session.Checkpoint`.
            #
            # Correct here for the reason the checkpoint contract asks
            # for: the attribution must stand however the rest of this
            # update goes. The DM is explicitly best-effort (the except
            # below), and the welcome card that follows must not be able
            # to un-invite anybody by failing. ``referrer_wallet`` stays
            # readable across the commit — the sessionmakers use
            # ``expire_on_commit=False`` — and ``ref_lang`` is resolved
            # above regardless, so nothing here can trigger a refresh.
            if checkpoint is not None:
                await checkpoint()
            try:
                await bot.send_message(referrer_id, t("h_start_ref_notify", ref_lang))
            except TelegramAPIError as exc:
                log.warning("referral notify failed for {rid}: {e!r}", rid=referrer_id, e=exc)

    suffix = ("\n\n" + t("h_start_ref_applied", user.language)) if attributed else ""
    await _send_welcome(
        message,
        user,
        bot,
        registry=registry,
        settings=settings,
        economy_repo=economy_repo,
        name=name,
        suffix=suffix,
        checkpoint=checkpoint,
    )
    log.bind(
        uid=user.user_id,
        is_new=user.is_new,
        attributed=attributed,
    ).info("/start ref handled")


async def _is_group_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Is ``user_id`` actually in ``chat_id`` right now?

    Fail-closed: any probe error reads as "no". The answer gates a
    write nobody asked for out loud — the deep link is a URL, and a URL
    can be retyped with somebody else's group id in it — so the cost of
    a false negative is that one person's DM does not remember its
    group, while the cost of a false positive is a stranger pointing
    their DM at a chat they have never been in.

    ``KICKED``/``LEFT`` are the two statuses that mean "not here";
    ``RESTRICTED`` carries the difference in ``is_member`` rather than
    in the status, same subtlety ``group_events._is_member`` documents.
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError as exc:
        log.warning(
            "group membership probe failed (chat={c}, user={u}): {e!r}",
            c=chat_id,
            u=user_id,
            e=exc,
        )
        return False
    if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        return False
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return True


async def _resolve_source_group(
    command: CommandObject,
    bot: Bot,
    registry: EngineRegistry,
    user_id: int,
) -> tuple[int, str] | None:
    """Validate a ``grp_`` payload into ``(chat_id, label)``, or ``None``.

    ``None`` whenever nothing should be remembered — a payload that
    doesn't parse, a chat the bot has left, or a caller who isn't in it.
    All three are silent by design: the deep link is a navigation aid,
    and a card explaining why it declined to remember a group would be
    answering a question nobody asked.

    Read-only, and that is the point: the caller does the write on the
    session the middleware already owns. Both the ``bot_groups`` lookup
    and the Telegram probe happen BEFORE the first write of the update,
    so no ``users.db`` write lock is held across the network call.
    ``users.db`` opens as ``BEGIN IMMEDIATE`` (``db/engines.py``), so a
    probe inside that window would park every other user's settings
    write behind one stranger's ``/start`` — the same trap
    ``handle_start_ref`` documents at its checkpoint.

    ``label`` is the stored title, falling back to the chat id: the
    confirmation line exists so the person can tell WHICH chat was
    picked, and a group registered before the bot could read its title
    would otherwise be announced as an empty name.
    """
    chat_id = parse_group_payload(command.args)
    if chat_id is None:
        return None
    async with session_for(registry, DBName.USERS) as session:
        row = await BotGroupsRepo(session).get_active(chat_id)
    if row is None:
        return None
    if not await _is_group_member(bot, chat_id, user_id):
        return None
    title = (row[1] or "").strip()
    return chat_id, title or str(chat_id)


async def handle_start_group_ctx(
    message: Message,
    command: CommandObject,
    user_service: UserService,
    user_settings_repo: UserSettingsRepo,
    economy_repo: EconomyRepo,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/start grp_<chat_id>`` — the ordinary welcome, with a chat attached.

    The card is the bare-``/start`` card: someone who taps "open in
    private" wants the bot, not a different screen. The payload's whole
    job is the write behind it, and the one visible difference is a
    confirmation line — so the person can see which group the DM is
    speaking for, and notice when it is the wrong one.

    Order is load-bearing. The payload is resolved first, while nothing
    is holding a write lock, because that step calls Telegram; the
    write then goes on ``user_settings_repo`` — the middleware's OWN
    ``users.db`` session, which :meth:`UserService.touch` has already
    made a writer. A second session here would be a second connection
    asking for a lock the first one holds until this handler returns:
    a self-deadlock that ends in ``database is locked``, not a slowdown.
    """
    tg_user = require_from_user(message)
    resolved = await _resolve_source_group(command, bot, registry, tg_user.id)
    user = await user_service.touch(tg_user)
    suffix = ""
    if resolved is not None:
        chat_id, label = resolved
        await user_settings_repo.set_current_group(tg_user.id, chat_id)
        suffix = "\n\n" + t("h_start_group_ctx_applied", user.language, title=html.escape(label))
    await _send_welcome(
        message,
        user,
        bot,
        registry=registry,
        settings=settings,
        economy_repo=economy_repo,
        name=display_name(tg_user, user.language),
        suffix=suffix,
        checkpoint=checkpoint,
    )
    log.bind(
        uid=user.user_id,
        is_new=user.is_new,
        remembered=resolved is not None,
    ).info("/start grp handled")


_FALLBACK_USERNAME = "this_bot"


async def _bot_username(bot: Bot) -> str:
    """Resolve the bot's @username for the deep-link button.

    ``get_me`` is cached by aiogram per-bot, so the group welcome doesn't
    pay a network round-trip on every call. Falls back to a placeholder
    if Telegram ever returns a bot with no username (shouldn't happen).
    """
    me = await bot.get_me()
    return (me.username or "").strip() or _FALLBACK_USERNAME


async def handle_start_group(
    message: Message,
    user_service: UserService,
    bot: Bot,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Group ``/start`` — greet the chat and point to the private menu.

    Legacy answered ``/start`` in groups too (``bot.py:cmd_start`` has no
    chat-type guard); dropping the response silently was a regression
    introduced when the legacy bridge was removed. We still register the
    user (so a first-ever interaction in a group seeds the row + language)
    and reply with the group welcome plus a deep-link button into the
    private chat where the full menu lives.

    RR-6 #61 restores legacy's three-section shape (``send_start_panel``,
    ``bot.py:16384``): hello → quick-start command row → full list. The
    quick row points only at commands this pipeline actually serves
    (legacy's ``/kom_balance`` alias has no owner here, so the row uses
    the short ``/balance`` form).
    """
    tg_user = require_from_user(message)
    user = await user_service.touch(tg_user)
    # #1983: ``touch`` is bookkeeping that stands either way, so end its
    # transaction before ``get_me`` and the send rather than hold
    # ``users.db``'s single writer slot across both. See
    # :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    username = await _bot_username(bot)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=t("h_start_group_dm_btn", user.language),
            # #1926: the button carries the chat it was tapped in, so
            # the DM it opens knows which group it is speaking for.
            url=dm_start_url(username, group_chat_id=message.chat.id),
        )
    )
    await message.answer(
        render_group_welcome(user.language, name=display_name(tg_user, user.language)),
        reply_markup=builder.as_markup(),
        disable_web_page_preview=True,
    )
    log.bind(
        uid=user.user_id,
        chat_id=message.chat.id,
        is_new=user.is_new,
    ).info("/start (group) handled")


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — fresh ``Router`` per call so tests can re-wire dispatchers.

    ``registry`` builds the :class:`EconomyMiddleware` that injects
    ``economy_repo`` into both private handlers, and is closed over for
    the 👑 group-owner probe (``users.db``, outside the economy session).
    ``settings`` is closed over for the developer-glyph check — same
    pattern as :mod:`~handlers.main_menu`, since neither is available
    through aiogram's DI.

    Since RR-6 #60 the bare ``/start`` path *does* use the economy
    session: the returning-user card reads the wallet, and a first-ever
    ``/start`` seeds it.
    """
    router = Router(name="start")
    router.message.middleware(EconomyMiddleware(registry))
    _private = F.chat.type == ChatType.PRIVATE
    _group = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})

    async def _bare_entry(
        message: Message,
        user_service: UserService,
        economy_repo: EconomyRepo,
        bot: Bot,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_start(message, user_service, economy_repo, bot, registry, settings, checkpoint)

    async def _ref_entry(
        message: Message,
        command: CommandObject,
        user_service: UserService,
        economy_repo: EconomyRepo,
        bot: Bot,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_start_ref(
            message, command, user_service, economy_repo, bot, registry, settings, checkpoint
        )

    async def _grp_entry(
        message: Message,
        command: CommandObject,
        user_service: UserService,
        user_settings_repo: UserSettingsRepo,
        economy_repo: EconomyRepo,
        bot: Bot,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_start_group_ctx(
            message,
            command,
            user_service,
            user_settings_repo,
            economy_repo,
            bot,
            registry,
            settings,
            checkpoint,
        )

    # Private-chat handlers carry the ``_private`` filter per-registration
    # rather than as a router-level filter, so the group ``/start`` branch
    # below can share the router (and its EconomyMiddleware session).
    # ``ref_<id>`` deep link first — a payload-bearing ``/start`` must not
    # be swallowed by the bare handler. ``deep_link=True`` + the magic
    # prefix narrow this to our namespace so ``check_…`` etc. still route
    # to their own owners. Deep links always open a private chat.
    router.message.register(
        _ref_entry,
        CommandStart(deep_link=True, magic=F.args.startswith("ref_"), ignore_case=True),
        _private,
        F.from_user,
    )
    # ``grp_<chat_id>`` — same shape, its own namespace (#1926). Sits
    # beside ``ref_`` rather than in a module of its own because it
    # renders the very same welcome card and would otherwise need the
    # economy session twice.
    router.message.register(
        _grp_entry,
        CommandStart(
            deep_link=True,
            magic=F.args.startswith(GROUP_PREFIX),
            ignore_case=True,
        ),
        _private,
        F.from_user,
    )
    # ``kom_start``: the multi-bot spelling legacy registered and the
    # catalog still advertises. Only the *bare* forms take it — deep
    # links arrive as ``/start <payload>`` from Telegram itself and are
    # never spelled ``/kom_start``.
    # ``ignore_case=True`` on both private registrations matches the
    # group twin below: mobile keyboards auto-capitalise the first
    # character, so ``/Start`` is the spelling a first-ever user
    # routinely sends, and this is the only path that seeds the wallet
    # and applies referral attribution (#972).
    router.message.register(
        _bare_entry,
        Command("start", "kom_start", magic=F.args.is_(None), ignore_case=True),
        _private,
        # Channel posts / anonymous senders — extremely rare in private chat.
        F.from_user,
    )
    # Group ``/start`` — legacy answered here too (ungated); restore it.
    # ``magic=F.args.is_(None)`` keeps this to *bare* ``/start``: a
    # payload-bearing group deep link (``/start check_…`` / ``ref_…``)
    # is private-only and must still fall through to its owner / UNHANDLED.
    router.message.register(
        handle_start_group,
        Command("start", "kom_start", magic=F.args.is_(None), ignore_case=True),
        _group,
        F.from_user,
    )
    return router
