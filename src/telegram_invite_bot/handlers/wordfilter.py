"""Per-group banned-word filter admin commands (L-52).

Ports the legacy per-group word filter (``word_filters`` table,
bot.py:5739, ``add_filter_word_for_group`` / ``remove_filter_word_for_group``
/ ``get_filter_words_for_group``) to the new aiogram pipeline.

Commands (group-only — a private chat gets the #123 refusal):

  /filter_add <word>     (alias /фильтр_добавить)  — add a banned word
  /filter_remove <word>  (alias /фильтр_удалить)   — remove a banned word
  /filter_list           (alias /фильтр_список)    — list banned words

Admin gate (CRITICAL)
---------------------
There is NO rank/role system in the new pipeline. Admin authorisation
is delegated wholesale to ``handlers.moderation._require_admin`` — the
exact live-Telegram-admin check (status ADMINISTRATOR/CREATOR, plus the
anonymous-admin + dev-bypass + fail-closed handling) used by every
/ban, /kick, /warn, … handler. We import and reuse it; we do NOT
reimplement an admin check here.

The automod check (delete group messages containing a banned word) is
:class:`WordFilterAutomodMiddleware` in this module — an **outer**
message middleware ``main_router`` mounts on the root router, so it
sees plain chatter no command handler claims and does not consume the
update (economy/stats/welcome still run). It lives here (not under
``middlewares/``) because it belongs to the word-filter feature; the
class is otherwise a standard aiogram ``BaseMiddleware``.
"""

from __future__ import annotations

import contextlib
import html
import re
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject, User
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.moderation import _require_admin, _resolve_lang
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.base import BaseSessionMiddleware
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.repositories.group_mod_config_repo import (
    GroupModConfigRepo,
    GroupModConfigView,
)
from telegram_invite_bot.repositories.moderation_repo import ModerationRepo
from telegram_invite_bot.repositories.word_filter_repo import (
    WordFilterRepo,
    normalize_word,
)
from telegram_invite_bot.utils.html import html_user_mention
from telegram_invite_bot.utils.render import paginate_lines
from telegram_invite_bot.utils.telegram_admin import is_chat_admin_any
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo

log = logger.bind(component="handlers.wordfilter")


# Cap on stored words per group (legacy was unbounded; a sane ceiling
# keeps the per-message automod scan O(small) and prevents abuse).
MAX_WORD_LENGTH: int = 100
MAX_WORDS_PER_GROUP: int = 500

#: Cap on the *combined* length of one group's words.
#:
#: The count ceiling alone does not bound the scan. Every message in
#: every group is searched with one alternation built from the whole
#: list (:meth:`WordFilterAutomodMiddleware._compile`), and the search
#: costs roughly ``len(text) x len(pattern)``: 500 entries of 100
#: characters each is an 81 kB pattern, and a 4 096-character message
#: whose text prefix-matches most branches takes **187 ms** to scan on
#: the machine this was measured on. That runs on the one shared event
#: loop, so it stalls every other group and every DM as well — a member
#: of one throwaway group could halt the bot for everybody at about five
#: messages a second.
#:
#: 4 000 characters costs ~15 ms in the same worst case and still admits
#: 500 ordinary words (an average entry is far under eight characters),
#: so the ceiling people actually reach is still the count one. It bites
#: only on the shape that makes the scan expensive: long multi-word
#: phrases, which ``validate_word`` permits by design.
#:
#: Legacy imports are deliberately *not* re-checked against this. The
#: monolith scanned the same alternation shape (bot.py:8297-8298) with
#: no ceiling at all for years, so an inherited list is a known quantity
#: rather than an attack; silently dropping words out of a live filter
#: to fit a budget would un-ban them without telling the admin, which is
#: worse than the scan it saves. The cap belongs on the way in.
MAX_FILTER_TOTAL_CHARS: int = 4000


def _arg(message: Message) -> str:
    """Return the command argument (everything after the first token)."""
    text = message.text or message.caption or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def validate_word(raw: str, *, empty_key: str) -> tuple[str, str | None]:
    """Normalise ``raw`` and decide whether it may enter a filter list.

    Returns ``(normalised word, refusal key)`` — the refusal key is
    ``None`` when the word is acceptable. Everything here is decidable
    without a DB read; the per-group ceiling
    (:data:`MAX_WORDS_PER_GROUP`) is checked by the caller, which is
    also the only place that already knows the current count.

    One function because the /groupadmin Words panel (RR-4 #43) adds
    words too, and a panel that validated more loosely than the command
    would just be the way around the command's rules.

    ``empty_key`` is the caller's own "you sent nothing" copy: the
    command answers with its usage line, the panel with its prompt.
    """
    word = normalize_word(raw)
    if not word:
        return word, empty_key
    if len(word) > MAX_WORD_LENGTH:
        return word, "h_wf_too_long"
    # Legacy refuses to filter command-looking tokens (translations.py
    # ``filter_cmd_ignored``) so an admin can't brick the bot's own
    # commands by banning "/start".
    if word.startswith("/"):
        return word, "h_wf_command_rejected"
    return word, None


def add_refusal(existing: list[str], word: str) -> str | None:
    """Which per-group ceiling ``word`` would break, if any.

    Both ceilings live here so the two writers cannot drift apart: the
    ``/groupadmin`` Words panel adds words too (groupadmin.py:1902), and
    a panel that enforced only the count would just be the way around
    the character budget.

    ``existing`` is the list the caller already read to count with, so
    this costs nothing extra.
    """
    if len(existing) >= MAX_WORDS_PER_GROUP:
        return "h_wf_limit_reached"
    if sum(len(w) for w in existing) + len(word) > MAX_FILTER_TOTAL_CHARS:
        return "h_wf_limit_chars"
    return None


async def handle_filter_add(
    message: Message,
    bot: Bot,
    word_filter_repo: WordFilterRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    checkpoint: Checkpoint | None = None,
) -> None:
    lang = await _resolve_lang(message, user_settings_repo)
    if not await _require_admin(message, bot, settings, lang):
        return

    word, refusal = validate_word(_arg(message), empty_key="h_wf_usage_add")
    if refusal is not None:
        await message.reply(t(refusal, lang))
        return

    existing = await word_filter_repo.list(group_id=message.chat.id)
    over = add_refusal(existing, word)
    if over is not None:
        await message.reply(t(over, lang))
        return

    added = await word_filter_repo.add(
        group_id=message.chat.id,
        word=word,
        added_by=message.from_user.id if message.from_user else None,
    )
    # #1878: ``add`` flushes its INSERT (word_filter_repo.py:91) rather
    # than leaving it queued, so the lock is real by the time we get
    # here. The duplicate-word path returns before the INSERT and so
    # commits nothing — a free no-op, not a branch worth splitting.
    # Commit before the reply so the banned word survives a
    # confirmation that never lands.
    if checkpoint is not None:
        await checkpoint()
    safe = html.escape(word)
    if added:
        await message.reply(t("h_wf_added", lang, word=safe))
    else:
        await message.reply(t("h_wf_already", lang, word=safe))


async def handle_filter_remove(
    message: Message,
    bot: Bot,
    word_filter_repo: WordFilterRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
    checkpoint: Checkpoint | None = None,
) -> None:
    lang = await _resolve_lang(message, user_settings_repo)
    if not await _require_admin(message, bot, settings, lang):
        return

    word = normalize_word(_arg(message))
    if not word:
        await message.reply(t("h_wf_usage_remove", lang))
        return

    removed = await word_filter_repo.remove(group_id=message.chat.id, word=word)
    # #1878: same as the alias delete — the guarded DELETE holds the
    # write lock whether or not it matched, so commit on both outcomes.
    if checkpoint is not None:
        await checkpoint()
    safe = html.escape(word)
    if removed:
        await message.reply(t("h_wf_removed", lang, word=safe))
    else:
        await message.reply(t("h_wf_not_found", lang, word=safe))


def _list_pages(words: list[str], lang: str) -> list[str]:
    """Split the filter list into messages Telegram will accept.

    Before this the whole list went out as ONE message. With the
    per-group ceiling at :data:`MAX_WORDS_PER_GROUP` (500) that is an
    ordinary-usage crash, not an abuse case: ~270 short words already
    pass 4096 characters, and the reply then failed with a 400 the admin
    never saw — the one surface that shows the full list, and the one
    the Words panel points at ("показать все: /filter_list"), simply
    stopped answering once the list grew.

    ``words`` must be non-empty — the caller answers the empty list with
    its own copy.
    """
    return paginate_lines(
        t("h_wf_list_header", lang, count=len(words)),
        [f"• <code>{html.escape(word)}</code>" for word in words],
        more_line=lambda count: t("h_wf_list_more", lang, count=count),
    )


async def handle_filter_list(
    message: Message,
    bot: Bot,
    word_filter_repo: WordFilterRepo,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
) -> None:
    lang = await _resolve_lang(message, user_settings_repo)
    if not await _require_admin(message, bot, settings, lang):
        return

    words = await word_filter_repo.list(group_id=message.chat.id)
    if not words:
        await message.reply(t("h_wf_list_empty", lang))
        return
    pages = _list_pages(words, lang)
    await message.reply(pages[0])
    # Only the first page quotes the command; the continuations are plain
    # messages so the chat doesn't grow a column of identical quote blocks.
    for page in pages[1:]:
        await message.answer(page)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Build the word-filter admin-command router.

    Middlewares:
    * :class:`_WordFilterRepoMiddleware` — opens this router's
      ``moderation.db`` session and binds ``word_filter_repo``. aiogram
      injects only what a middleware puts in ``data``, which is the
      whole reason this tiny module-private middleware exists.
    * :class:`SessionMiddleware` — injects ``user_settings_repo`` for
      language resolution.

    :class:`ModerationMiddleware` used to be mounted here too (#2025).
    It binds ``moderation_repo``, which no handler in this module takes,
    so every ``/filter_add`` checked out a second ``moderation.db``
    connection and closed it untouched. Harmless only for as long as it
    stayed untouched: two sessions on one file in one update, committed
    separately by ``middlewares/base.py``'s unordered exit, become two
    ``BEGIN IMMEDIATE`` writers the moment a handler here writes through
    the second one. The automod middleware below is unaffected — it is
    self-contained and opens its own session.

    Group-only filter: private-chat invocations get the #123 refusal twin.

    Handlers are inner closures capturing ``settings`` — the same pattern
    used by ``moderation.py`` and every other handler module here.
    """
    router = Router(name="wordfilter")
    router.message.middleware(_WordFilterRepoMiddleware(registry))
    router.message.middleware(SessionMiddleware(registry))

    group_filter = F.chat.type.in_(GROUP_TYPES)

    async def _add(
        message: Message,
        bot: Bot,
        word_filter_repo: WordFilterRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_filter_add(
            message, bot, word_filter_repo, user_settings_repo, settings, checkpoint
        )

    router.message.register(
        _add,
        Command("filter_add", "f_add", "фильтр_добавить", ignore_case=True),
        F.from_user,
        group_filter,
    )

    async def _remove(
        message: Message,
        bot: Bot,
        word_filter_repo: WordFilterRepo,
        user_settings_repo: UserSettingsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_filter_remove(
            message, bot, word_filter_repo, user_settings_repo, settings, checkpoint
        )

    router.message.register(
        _remove,
        Command("filter_remove", "f_del", "фильтр_удалить", ignore_case=True),
        F.from_user,
        group_filter,
    )

    async def _list(
        message: Message,
        bot: Bot,
        word_filter_repo: WordFilterRepo,
        user_settings_repo: UserSettingsRepo,
    ) -> None:
        await handle_filter_list(message, bot, word_filter_repo, user_settings_repo, settings)

    router.message.register(
        _list,
        Command("filter_list", "f_list", "фильтр_список", ignore_case=True),
        F.from_user,
        group_filter,
    )

    return with_chat_type_refusal(router, scope="group")


# ---------------------------------------------------------------------------
# Repo-binding middleware (word_filter_repo on the moderation.db session)
# ---------------------------------------------------------------------------


class _WordFilterRepoMiddleware(BaseSessionMiddleware):
    """Open one ``moderation`` session per update; expose :class:`WordFilterRepo`.

    Kept module-private: it serves only this router, and it is this
    router's ONLY ``moderation.db`` session (#2025) — the word-filter
    commands never touch ``moderation_repo``, so nothing else needs to
    be open over that file while they run.
    """

    def __init__(self, registry: EngineRegistry) -> None:
        super().__init__(registry, DBName.MODERATION)

    def _bind(self, session: AsyncSession, data: dict[str, Any]) -> None:
        data["word_filter_repo"] = WordFilterRepo(session)


# ---------------------------------------------------------------------------
# Automod outer middleware (delete group messages containing a banned word)
# ---------------------------------------------------------------------------

# How long a per-group word list is trusted before a DB re-read. Short
# enough that a freshly-added word starts filtering almost immediately,
# long enough that a busy chat is not one-DB-read-per-message.
_CACHE_TTL_SECONDS: float = 30.0

# Bound the cache so a bot in thousands of groups cannot grow it without
# limit (LRU eviction by insertion order).
_MAX_CACHED_GROUPS: int = 2000

# How long a staff/non-staff verdict about one author is trusted before
# the bot re-asks Telegram. Only consulted when a message actually trips
# the filter, so this bounds API chatter during a burst of banned words,
# not during normal traffic.
_ADMIN_TTL_SECONDS: float = 60.0
_MAX_CACHED_ADMIN_VERDICTS: int = 4000

# What one cache entry holds: the compiled matcher (``None`` = nothing to
# enforce) and the group's moderation config, which the #265 escalation
# reads right after a deletion.
_Policy = tuple[re.Pattern[str] | None, GroupModConfigView]


class WordFilterAutomodMiddleware(BaseMiddleware):
    """Delete group messages containing a per-group banned word (L-52).

    Deletion is gated on ``profanity_enabled``; the warn / auto-ban chain
    that follows it (see ``_escalate``) is gated on ``automod_enabled``
    — two separate toggles, exactly as legacy split them
    (bot.py:43853 vs :43868).

    Mounted by ``main_router`` as an **outer** message middleware on the
    root router so it runs for every message — including plain chatter no
    command handler claims — without consuming the update (economy /
    stats / welcome middlewares still run).

    Self-contained: opens its own short-lived ``moderation.db`` session
    per cache-miss; depends on no repo another middleware binds.

    Best-effort: a failed list-read or ``message.delete`` is logged and
    swallowed — automod must NEVER raise into the dispatch pipeline.

    Admin messages are NOT exempted from the DELETION (legacy deleted
    matching messages from everyone, and re-checking admin status would
    cost a Telegram API call per message). Admins discussing a banned
    word can /filter_remove it. #1848/#1781 split the sanction off from
    the deletion: staff still lose the message, but are never warned and
    never auto-banned — see :meth:`_is_exempt_from_sanction`, whose probe
    runs only on an actual hit and is TTL-cached, so the "one call per
    message" objection above still holds for the deletion path.

    The other exemption is structural, not a rank check: posts with no
    warnable author — bots, anonymous admins, channel posters and the
    linked channel's automatic forwards — are skipped outright (#253).
    """

    def __init__(self, registry: EngineRegistry, settings: Settings) -> None:
        self._registry = registry
        # #1848: only ``bot.is_developer`` is read, but the whole
        # Settings object is held rather than the id set, so the owner
        # list this middleware honours is the same object every other
        # gate reads — one source of truth, no snapshot to drift.
        self._settings = settings
        # Cache the group's whole automod policy: the compiled matcher
        # (or ``None`` = nothing to enforce) AND the moderation config
        # the escalation reads. Tupled so a cached ``None`` pattern is
        # distinguishable from a cache miss (the TTL cache returns
        # ``None`` on miss).
        self._cache: TTLLRUCache[int, _Policy] = TTLLRUCache(_CACHE_TTL_SECONDS, _MAX_CACHED_GROUPS)
        # (chat_id, user_id) -> "exempt from the sanction chain".
        self._admin_cache: TTLLRUCache[tuple[int, int], bool] = TTLLRUCache(
            _ADMIN_TTL_SECONDS, _MAX_CACHED_ADMIN_VERDICTS
        )

    @staticmethod
    def _compile(words: list[str]) -> re.Pattern[str] | None:
        """Compile the group's banned words into one boundary-anchored,
        case-insensitive alternation — or ``None`` if the list is empty.

        ``(?<!\\w)…(?!\\w)`` anchors each word at Unicode word boundaries
        (Python ``re`` ``\\w`` is Unicode-aware for ``str``, so this works
        for Cyrillic AND Latin). Legacy anchored the same way, with
        ``\\b`` (bot.py:8297-8298) — this is parity, not a fix; the
        lookaround spelling additionally survives entries that start or
        end with a non-``\\w`` character, where ``\\b`` would flip meaning.
        Multi-word phrase entries keep their internal spaces
        (``re.escape``) and are still boundary-anchored at the ends.
        """
        cleaned = [w.strip() for w in words if w and w.strip()]
        if not cleaned:
            return None
        alternation = "|".join(rf"(?<!\w){re.escape(w)}(?!\w)" for w in cleaned)
        return re.compile(alternation, re.IGNORECASE)

    async def _policy_for(self, group_id: int) -> _Policy:
        """The group's matcher plus the config the escalation needs.

        One read, one cache entry: ``profanity_enabled`` decides whether
        a matcher exists at all, and ``automod_enabled`` / ``max_warns``
        / ``autoban_enabled`` decide what happens after a hit. Splitting
        them into two caches would double the per-miss DB work for a
        single row.
        """
        now = time.monotonic()
        cached = self._cache.get(group_id, now)
        if cached is not None:
            return cached
        sessionmaker = self._registry.session(DBName.MODERATION)
        async with sessionmaker() as session:
            # Honour the per-group profanity toggle. Legacy had the same
            # switch but only GLOBALLY — ``profanity_enabled``
            # (bot.py:2527 default, :3117 module global, read at :8323
            # and :43853); per-group is a deliberate widening, not
            # parity. When off, an admin can keep a word list "for
            # reference" without live deletions.
            cfg = await GroupModConfigRepo(session).get_or_default(group_id)
            words = (
                await WordFilterRepo(session).list(group_id=group_id)
                if cfg.profanity_enabled
                else []
            )
        policy = (self._compile(words), cfg)
        self._cache.put(group_id, policy, now)
        return policy

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            await self._maybe_moderate(event, data)
        except Exception as exc:  # noqa: BLE001 — automod must never raise
            chat_id = event.chat.id if isinstance(event, Message) else None
            # Not "passing the message through" in the sense an
            # operator would read it: by every reachable failure point
            # inside ``_escalate`` the message is already deleted and the
            # public notice already sent. Only the UPDATE continues down
            # the chain, so say that instead.
            log.bind(chat_id=chat_id, exc=repr(exc)).warning(
                "word-filter automod failed mid-way; any deletion or notice "
                "already done stands, update dispatched anyway",
            )
        return await handler(event, data)

    async def _maybe_moderate(self, event: TelegramObject, data: dict[str, Any]) -> None:
        if not isinstance(event, Message):
            return
        if event.chat.type not in GROUP_TYPES:
            return
        # #253: bots, anonymous admins and channel posters have no
        # warnable, bannable author behind them, and legacy never even
        # looked at their text — ``handle_all_messages`` returned on
        # ``from_user.is_bot`` (bot.py:43796) long before the profanity
        # check at :43853. Deleting what we can neither warn nor ban for
        # is half a sanction: the poster simply reposts, and an
        # anonymous ADMIN gets auto-moderated by their own bot.
        #
        # ``sender_chat`` additionally covers the linked channel's
        # automatic forwards, whose deletion breaks the discussion
        # thread for that post. Legacy DID delete those (777000 is not a
        # bot, and ``is_sender_chat_message`` gated only the bookkeeping
        # at bot.py:43824-43850, never the check at :43853) — that is a
        # deliberate improvement, and it puts this module in line with
        # ``handlers.antiflood`` (:220), which already skips the same
        # three cases.
        user = event.from_user
        if user is None or user.is_bot or event.sender_chat is not None:
            return
        text = event.text or event.caption
        if not text:
            return
        try:
            pattern, cfg = await self._policy_for(event.chat.id)
        except Exception as exc:  # noqa: BLE001 — automod must never raise
            log.bind(chat_id=event.chat.id, exc=repr(exc)).warning(
                "word-filter list read failed; skipping check",
            )
            return
        if pattern is None:
            return

        match = pattern.search(text)
        if match is None:
            return
        hit = match.group(0)

        try:
            await event.delete()
        except Exception as exc:  # noqa: BLE001 — best-effort delete
            log.bind(chat_id=event.chat.id, msg_id=event.message_id, exc=repr(exc)).info(
                "word-filter delete failed (no rights / already gone)"
            )
            return
        log.bind(chat_id=event.chat.id, msg_id=event.message_id, word=hit).info(
            "word-filter: deleted message containing banned word",
        )
        await self._escalate(event, data, cfg, hit, user)

    async def _is_exempt_from_sanction(self, bot: Bot, chat_id: int, user_id: int) -> bool:
        """True when this author may be filtered but must not be sanctioned.

        #1848/#1781. The escalation below is the only place in the bot
        that hands out a warning and a PERMANENT ban with no human in
        the loop, and until now it did so with none of /warn's four
        gates. Two people must never reach it:

        * a live group admin. ``handle_unwarn`` refuses every admin
          target (``moderation._check_target_ok``), so a row written
          here against an admin could not be lifted by anyone — it
          simply sat out its 30 days while every further hit tried
          another ban;
        * the bot owner, who is let through every other moderation gate
          (``moderation._require_admin``) and is not necessarily an
          admin of a group their bot serves.

        The owner check is local and comes first, so the common case
        costs no round-trip at all.

        The probe is :func:`is_chat_admin_any` — the WIDE one. A
        title-only administrator holds no moderation authority (#337)
        but is still staff, and this is a courtesy exemption, not an
        authority verdict, which is exactly the distinction
        ``utils/telegram_admin`` draws.

        Fail-SAFE on an API error, the same direction
        ``antiflood._is_exempt_admin`` takes and the OPPOSITE of
        ``_require_admin``'s fail-closed posture — deliberately. There,
        uncertainty means refusing to act on someone's command, and the
        cost is one retry. Here, uncertainty would mean acting anyway,
        and the cost is a permanent ban nothing in this codebase lifts.

        Verdicts are TTL-cached (including the ``None`` one) so a
        Telegram outage during a burst of banned words cannot turn into
        a ``get_chat_member`` storm.
        """
        if self._settings.bot.is_developer(user_id):
            return True
        now = time.monotonic()
        key = (chat_id, user_id)
        cached = self._admin_cache.get(key, now)
        if cached is not None:
            return cached
        verdict = await is_chat_admin_any(bot, chat_id, user_id)
        exempt = verdict is None or verdict
        self._admin_cache.put(key, exempt, now)
        return exempt

    async def _escalate(
        self,
        event: Message,
        data: dict[str, Any],
        cfg: GroupModConfigView,
        hit: str,
        user: User,
    ) -> None:
        """#265: the warn/auto-ban chain legacy ran after every delete.

        Legacy ``bot.py:43852-43908``, in order:

        * ``:43859`` delete the message — done by the caller;
        * ``:43864-43865`` send a chat warning naming the author. Gated
          on ``PROFANITY_ENABLED`` alone (``:43853``), NOT on
          ``AUTO_MODERATE`` — so it belongs with the deletion, not with
          the escalation below;
        * ``:43868`` everything from here is gated on ``AUTO_MODERATE``
          (``group_mod_config.automod_enabled`` here). Until #265 that
          toggle had no effect on anything at all;
        * ``:43871`` **pre**-increment check ``warnings < limit`` — the
          warning is added only while the user is still under it. This
          is deliberately NOT ``moderation.py``'s post-increment ``>=``:
          the two differ by one warning at the boundary;
        * ``:43894`` the ``elif`` twin — a user already at or over the
          limit is re-banned WITHOUT a new warning row.

        The ban is permanent because legacy's was: ``add_ban`` passed
        ``duration_minutes=24 * 7 * 60`` (``bot.py:43887``, and its
        ``elif`` twin ``:43902``) but called
        ``bot.ban_chat_member(chat_id, user_id)`` with no ``until_date``
        (``bot.py:8970``), and its only expiry sweeper is never
        scheduled — the same finding that settled the /warn auto-ban
        (see ``handlers.moderation.handle_warn``, #282).

        Legacy pre-checked ``bot_has_ban_rights`` (``:43880``); we let
        the API call fail and log it instead — one fewer round-trip per
        deletion, and the outcome an operator reads is the real one.

        Every step is best-effort: automod must never raise into the
        dispatch pipeline, and a failed notice must not cost the user
        their warning row.

        ``user`` is the message author, already narrowed by the caller —
        until #253 this method carried its own bot / ``sender_chat``
        guard while the deletion above ran unguarded, so those messages
        were deleted and then silently not escalated.
        """
        chat_id = event.chat.id
        # LanguageMiddleware (root outer, registered before us) has
        # already stamped the effective language for this update.
        lang = data.get("lang") or "ru"
        mention = html_user_mention(user.id, user.full_name or str(user.id))

        with contextlib.suppress(Exception):
            await event.answer(t("h_wf_automod_warning", lang, name=mention))

        if not cfg.automod_enabled:
            return

        # Duck-typed on purpose (anything with ``ban_chat_member`` and
        # ``id``) so tests can inject a lightweight fake.
        bot: Bot | None = data.get("bot")
        if bot is None:
            log.warning("word-filter: no Bot in middleware data; skipping escalation")
            return

        if await self._is_exempt_from_sanction(bot, chat_id, user.id):
            log.bind(chat_id=chat_id, user_id=user.id, word=hit).info(
                "word-filter: message deleted, escalation skipped (staff author)",
            )
            return

        # Legacy stored the offending word in the reason
        # (``bot.py:43875`` ``f"Мат: {word}"``); it is shown back to
        # admins by /warnings, so the wording is parity, not a message
        # this code composes.
        reason = f"Мат: {hit}"
        sessionmaker = self._registry.session(DBName.MODERATION)
        async with sessionmaker() as session:
            repo = ModerationRepo(session)
            current = await repo.get_warning_count(user_id=user.id, chat_id=chat_id)
            if current < cfg.max_warns:
                _wid, count = await repo.add_warning(
                    user_id=user.id,
                    chat_id=chat_id,
                    admin_id=bot.id,
                    reason=reason,
                )
                await session.commit()
                warned = True
                # #1857: ``==``, not ``>=``. The read above is a bare
                # SELECT, and the ``before_cursor_execute`` hook in
                # ``db/engines.py`` opens BEGIN IMMEDIATE only on a
                # write-headed statement — so it holds no lock, and two
                # messages arriving together both see the pre-race
                # count. ``add_warning`` then counts inside its own
                # write transaction and hands back 3 to one and 4 to the
                # other. Under ``>=`` both were "at the limit": two
                # ban_chat_member calls, two audit rows, two public
                # announcements. Only the caller that actually reached
                # the threshold gets to act on it. /warn closes the same
                # race the same way — ``handle_warn``'s M-M-1 branch
                # compares with ``==`` for exactly this reason.
                reached_limit = count == cfg.max_warns
            else:
                # bot.py:43894 — already at/over the limit: re-ban, but
                # do not stack another warning on top.
                count = current
                warned = False
                # ``>=`` on purpose here: nothing was written, so there
                # is no second writer to collide with, and a target
                # sitting ABOVE the limit must still be re-banned.
                reached_limit = count >= cfg.max_warns
            should_ban = reached_limit and cfg.autoban_enabled

        log.bind(chat_id=chat_id, user_id=user.id, warned=warned, count=count, word=hit).info(
            "word-filter: automod escalation for a deleted message"
        )
        if not should_ban:
            return

        # #220: the session above is closed before the network call, so
        # a slow Telegram round-trip never holds moderation.db's writer.
        try:
            await bot.ban_chat_member(chat_id, user.id)
        except Exception as exc:  # noqa: BLE001 — best-effort ban
            log.bind(chat_id=chat_id, user_id=user.id, exc=repr(exc)).info(
                "word-filter: auto-ban failed (no rights / left chat)",
            )
            return

        # #1858: the ban has already happened and cannot be taken back
        # from here — there is no ``bans`` table in this pipeline
        # (``db/models/moderation.py``), so it lives only inside
        # Telegram. Letting a locked or broken moderation.db escape here
        # cost the chat its only remaining explanation: the announcement
        # below never fired, and the target simply vanished. Losing the
        # row is bad; losing the row AND the notice is worse.
        try:
            async with sessionmaker() as session:
                await ModerationRepo(session).record_action(
                    action="ban",
                    user_id=user.id,
                    admin_id=bot.id,
                    chat_id=chat_id,
                    reason=reason,
                    details=f"automod_profanity_warns={count}",
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — the ban stands regardless
            log.bind(chat_id=chat_id, user_id=user.id, exc=repr(exc)).warning(
                "word-filter: auto-ban applied but its audit row was not written",
            )

        with contextlib.suppress(Exception):
            await event.answer(t("automod_ban_message", lang, name=mention))
