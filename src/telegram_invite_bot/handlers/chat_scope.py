"""Pairing a chat-type-gated router with the refusal it owes (#123).

#122 fixed the per-registration half of this: a command registered
only with ``F.chat.type.in_(GROUP_TYPES)`` does not *refuse* a private
invocation, it never matches one, so the dispatcher drops the update
and the user hears nothing. There the fix was one extra
``register(...)`` per family.

That shape does not work for the ~180 commands whose gate lives on the
router itself (``router.message.filter(F.chat.type == PRIVATE)`` in
``shop``, ``checks``, ``withdraw``, … or a ``group_filter`` local
threaded through every registration in ``moderation``, ``marriage``,
``wordfilter``, …). A router-level filter applies to every handler in
that router *and to every child router it contains*, so a refusal
twin cannot simply be registered alongside — it would inherit the very
gate it exists to answer.

So the fix is structural. :func:`with_chat_type_refusal` returns an
unfiltered wrapper Router holding two children:

    wrapper (no filters)
    ├── worker    — the original router, gate and middlewares intact
    └── refusal   — the same command words, opposite chat type

Include order matters: the worker is first, so in the chat type where
the command actually works nothing changes — the refusal router is
only ever reached by an invocation the worker declined to match.

**The refusal's alias list is read off the worker itself.** Walking the
worker's ``message`` handlers for their :class:`~aiogram.filters.Command`
filters means the two lists cannot drift: a command renamed, added or
retired in the module changes the refusal in the same edit, and nobody
has to remember a second place. ``ignore_case`` and ``prefix`` are
carried over per group of words rather than normalised, so the refusal
matches exactly the spellings the worker would have.
"""

from __future__ import annotations

import html
from collections import defaultdict
from typing import TYPE_CHECKING, Final, Literal

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.core.deep_links import dm_start_url
from telegram_invite_bot.core.ranks import RankLevel, command_key_for, default_min_rank
from telegram_invite_bot.handlers.group_only import handle_group_only
from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram import Bot
    from aiogram.filters import CommandObject
    from aiogram.types import Message

#: Which chat type the wrapped router actually serves. The refusal is
#: registered for the other one.
ChatScope = Literal["group", "private"]

#: Where the wrapper stashes the router it wraps, for
#: :func:`scoped_worker`. An attribute rather than a naming convention
#: so the lookup can't be fooled by a module that happens to name its
#: router the same way.
_WORKER_ATTR: Final = "_chat_scope_worker"

#: Only used if Telegram ever hands back a bot with no username — the
#: button would otherwise carry a broken link. Mirrors the fallback in
#: ``handlers/group_events.py``.
_FALLBACK_USERNAME = "kom17bot"

#: Commands at or above this catalog rank stay silent in the wrong chat
#: type. ``handlers/admin/*`` is PRIVATE-only on purpose — a group
#: ``/admin_cpu`` that answered "this only works in a DM" would confirm
#: the command exists to whoever typed it — and the same reasoning
#: covers the owner-tier commands that live inside otherwise ordinary
#: modules (``/broadcast`` in ``broadcast.py``, ``/promo_create`` in
#: ``promo.py``, ``/admin_tickets`` and friends in ``support.py``,
#: ``/modcfg`` in ``modcfg.py``). Reading the tier off the catalog
#: rather than listing those words here means a command promoted to the
#: owner tier stops being advertised in the same edit.
_SILENT_FROM_RANK: Final[int] = RankLevel.OWNER

#: Words that look one-sided from where the refusal is assembled but
#: are in fact served in both chat types, so no module may refuse them.
#: Two shapes are in here, and the constant covers both:
#:
#: * *Across modules* — ``/voice_settings``: the group panel lives in
#:   ``handlers/voice_settings.py``, the VIP personal settings under the
#:   same word in ``handlers/vip.py``. Neither module can see the other.
#: * *Within one module* — ``/fine`` (#252): a single registration in
#:   ``handlers/moderation.py`` deliberately carries no chat filter,
#:   inside a router that is otherwise group-only and wrapped as such.
#:   :func:`command_specs` walks registrations, not their filters, so it
#:   cannot tell that one apart from its nine group-only neighbours.
#:
#: The list is not maintained by hand-auditing:
#: ``test_chat_scope_coverage.py::test_no_refusal_shadows_a_real_handler``
#: resolves the assembled tree and fails if any refusal lands in the
#: same chat type as a working handler, so a future overlap surfaces as
#: a red test rather than as a command that stopped answering.
_TWO_SIDED_COMMANDS: Final[frozenset[str]] = frozenset(
    {"voice_settings", "voice_settings_ru", "fine", "штраф", "penalty"}
)


async def handle_private_only(
    message: Message,
    command: CommandObject,
    lang: str,
    bot: Bot,
) -> None:
    """Tell a group-chat caller the command lives in a private chat.

    The mirror of :func:`~handlers.group_only.handle_group_only`, with
    one addition: getting to a DM is a navigation step, not just a
    fact, so the answer carries the same deep-link button the group
    welcome card uses — payload and all, so the DM opens knowing which
    group the caller was standing in.

    ``bot.me()``, not ``bot.get_me()``: only the former memoises on the
    Bot instance. This handler fires whenever anyone types a DM-only
    command in a group, which is exactly the mistake people repeat, and
    a ``getMe`` round-trip per mistake would buy nothing — the username
    cannot change under a running process without a restart.

    The alias is echoed as typed and HTML-escaped for the same reasons
    documented on the group-only twin.
    """
    me = await bot.me()
    username = (me.username or "").strip() or _FALLBACK_USERNAME
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=t("h_private_only_btn", lang),
            # #1926: carry the chat the command was typed in. This is
            # the button somebody presses mid-task — they reached for a
            # group's panel and were sent to a DM — so arriving there
            # with the group already attached is the whole point.
            url=dm_start_url(username, group_chat_id=message.chat.id),
        )
    )
    await message.answer(
        t("h_private_only_command", lang, command=html.escape(command.command)),
        reply_markup=builder.as_markup(),
    )


def _stays_silent(word: str) -> bool:
    """Should ``word`` be left out of the refusal list?

    Two reasons, both documented on the constants: the command is
    owner-tier and must not be advertised, or it is already served in
    the chat type this router would refuse.

    A rank *override* can raise or lower a command at runtime, but the
    refusal list is built once at router-assembly time and an override
    is per-chat anyway. The catalog default is the right source here:
    it says what the command *is*, not who may run it today.
    """
    if word.lower() in _TWO_SIDED_COMMANDS:
        return True
    return default_min_rank(command_key_for(word)) >= _SILENT_FROM_RANK


def command_specs(router: Router) -> dict[tuple[bool, str], set[str]]:
    """Literal command words the router registers, keyed by match style.

    Grouped by ``(ignore_case, prefix)`` so the refusal reproduces the
    worker's own matching rather than imposing a house style: a module
    that registers ``/nick`` case-sensitively should not have its
    refusal answer ``/NICK`` that the worker itself would ignore.

    Public because :mod:`~handlers.unknown_form` (#158) needs the same
    walk over the whole assembled tree: "which words does this bot
    answer to, and which of those may be named out loud" is one
    question, and two answers to it would drift.

    Regex entries in ``Command.commands`` are skipped — a pattern has
    no single word to echo back, and no module here registers one — and
    so are owner-tier words, which :data:`_SILENT_FROM_RANK` explains.

    :class:`~aiogram.filters.CommandStart` registrations are skipped
    too, and that one is easy to miss: ``CommandStart`` *subclasses*
    ``Command`` with ``commands=("start",)``, so a deep-link entry point
    such as ``checks.py``'s ``CommandStart(deep_link=True,
    magic=F.args.startswith("check_"))`` would otherwise hand ``start``
    to the refusal list — and a private-only refusal for ``/start``
    shadows the real group welcome in ``handlers/start.py``. ``/start``
    is never a chat-scoped word anyway: it is an entry point Telegram
    itself puts in front of the user in both chat types.
    """
    specs: dict[tuple[bool, str], set[str]] = defaultdict(set)
    stack = [router]
    while stack:
        current = stack.pop()
        stack.extend(current.sub_routers)
        for handler in current.message.handlers:
            for filter_object in handler.filters or ():
                callback = filter_object.callback
                if not isinstance(callback, Command) or isinstance(callback, CommandStart):
                    continue
                words = {
                    c for c in callback.commands if isinstance(c, str) and not _stays_silent(c)
                }
                if words:
                    specs[(callback.ignore_case, callback.prefix)] |= words
    return specs


def with_chat_type_refusal(worker: Router, *, scope: ChatScope) -> Router:
    """Wrap ``worker`` so the other chat type gets an answer, not silence.

    ``scope`` names where the commands actually work. Returns the
    wrapper to include in place of ``worker``; if the worker registers
    no literal command words there is nothing to refuse and the worker
    is returned unchanged, so a caller can apply this unconditionally.
    """
    specs = command_specs(worker)
    if not specs:
        return worker

    refusal = Router(name=f"{worker.name}:refusal")
    # The two refusals take different injections (the private-only one
    # needs ``bot`` for the deep-link button), which is exactly what
    # aiogram's signature-based injection exists to absorb.
    handler: Callable[..., Awaitable[None]]
    if scope == "group":
        handler = handle_group_only
        chat_filter = F.chat.type == ChatType.PRIVATE
    else:
        handler = handle_private_only
        chat_filter = F.chat.type.in_(GROUP_TYPES)

    for (ignore_case, prefix), words in sorted(specs.items()):
        refusal.message.register(
            handler,
            Command(*sorted(words), ignore_case=ignore_case, prefix=prefix),
            # An anonymous group admin (and a channel post) has no
            # ``from_user``. Those already fall through silently today;
            # refusing them would be a behaviour change this fix has no
            # business making, and the worker halves guard the same way.
            F.from_user,
            chat_filter,
        )

    wrapper = Router(name=f"{worker.name}:scoped")
    wrapper.include_router(worker)
    wrapper.include_router(refusal)
    setattr(wrapper, _WORKER_ATTR, worker)
    return wrapper


def scoped_worker(router: Router) -> Router:
    """The module's own router inside a wrapper, or ``router`` itself.

    A wrapper is a new *ancestor*, and that changes one thing for
    callers: aiogram resolves a handler's inner middlewares by walking
    the router chain from the head down
    (``TelegramEventObserver._resolve_middlewares``), so a middleware
    attached to the wrapper runs BEFORE the ones the module registered
    on itself — the opposite of what a caller appending to "the
    module's router" would expect.

    Nothing in ``main_router`` attaches middlewares after
    ``build_router`` returns, so production is unaffected; this exists
    for the tests that stand in for a production middleware and must
    slot into the module's own chain rather than in front of it.
    """
    worker = getattr(router, _WORKER_ATTR, router)
    return worker if isinstance(worker, Router) else router
