"""``/top`` — multi-mode leaderboard (Stages 20 + 21).

Legacy ``/top`` (bot.py:18716) is a multi-mode command:

* ``/top messages [days]`` — group leaderboard by message count
  (group-only, default 30 days).
* ``/top balance`` / ``games`` / ``wins`` / ``streak`` — economy-side
  leaderboards (private or group).
* Bare ``/top`` — defaults to ``messages`` in groups and ``balance``
  in private.

Stage 20 ported the **messages** slice. Stage 21 added the **balance**
slice, which closes the bare-``/top``-in-private hole that previously
fell through to legacy. T-016 adds the remaining three economy modes
(``games`` / ``wins`` / ``streak``) reading from the same
``economy.users`` columns the legacy aggregates pull from (``games_played``,
``games_won``, ``daily_streak``).

All four read-side modes are now owned by the new pipeline. The only
``/top`` form still falling through to legacy is ``/top winrate`` (a
ratio aggregate not yet ported) and any unknown subcommand.

Scope owned by this handler:

* ``/top`` / ``/kom_top`` with **no args** in a **group** → 30-day
  messages leaderboard, top 50.
* ``/top messages [days]`` in a group, days ∈ [1..365] (Stage 20).
* ``/top`` / ``/top balance`` (any ``ignore_case``) in **private** → top 10
  by ``balance``, descending. Bare ``/top`` in private uses ``balance``
  because that's the legacy default (bot.py:18727).
* ``/top balance`` in **groups** when the user typed it explicitly —
  legacy supports it there too (bot.py:18766 is mode-agnostic about
  chat type), and a group member calling ``/top balance`` to brag
  about their stash is a legitimate use case.
* Any other subcommand (``games``/``wins``/``streak`` or unknown) →
  falls through. The new pipeline only owns what it can render.

Cross-DB rendering (both modes): the data repo returns
``(user_id, value)``; display names resolve via
:class:`UsersRepo.first_names_by_ids` in one batched query against
``users.db``. Falling back to a copy-side ``h_top_fallback_name`` for
ids not yet in ``users`` matches legacy (bot.py:29917-29919 returns
the same shape for the ``get_chat_member`` miss path).

Bot exclusion: legacy filters via :func:`is_excluded_bot_user`, which
needs a Telegram ``get_chat_member`` call to lazily flag unknown bots.
That requires runtime API access + a process-wide cache. We don't
port the cache here — bots that already have rows in
``message_counts`` / ``economy.users`` will keep showing up until the
activity-write port (a later stage) replaces both the writer and the
exclusion list together. Mentioning explicitly so the gap doesn't get
re-discovered as a regression.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.middlewares.message_stats import MessageStatsMiddleware
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.utils.aiogram import command_args
from telegram_invite_bot.utils.html import (
    TELEGRAM_TEXT_LIMIT,
    html_user_mention,
    visible_len,
)
from telegram_invite_bot.utils.numbers import format_number
from telegram_invite_bot.utils.plural import plural

log = logger.bind(component="handlers.top")

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import StatsConfig
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.emoji_badge_repo import EmojiBadgeRepo
    from telegram_invite_bot.repositories.message_stats_repo import MessageStatsRepo
    from telegram_invite_bot.repositories.users_repo import UsersRepo


_DEFAULT_DAYS = 30
# Legacy renders **every** non-excluded user (bot.py:29903 — no
# ``LIMIT`` when ``limit=None``). For a 200-member chat that produces
# a ~10 KB message; Telegram's send-message body cap is 4096 chars,
# which the legacy renderer silently exceeds on busy groups (visible
# in legacy as a "message too long" 400 from the API). The new
# pipeline caps at 50 — comfortably within Telegram's limit, still
# more useful than the screenful most leaderboards actually show, and
# the deliberate divergence from legacy that's documented here so a
# future reader doesn't re-set it to ``None`` thinking it was a
# parity oversight.
_DEFAULT_LIMIT = 50
# Balance ladder shows the top 10. Legacy hard-codes 10
# (bot.py:18768) — keeping parity here matters because the balance
# leaderboard is a brag-list, not an audit table, and showing 50
# entries on a busy server would dilute the "1st-place" social signal
# the command exists to provide.
_BALANCE_LIMIT = 10
_MEDALS: dict[int, str] = {1: "🥇", 2: "🥈", 3: "🥉"}

# Telegram rejects a sendMessage whose *text* exceeds 4096 chars with a
# 400 "message is too long" — and the count is over the rendered text,
# NOT the HTML markup (the ``<a href…>`` wrapper parses into an entity
# and contributes nothing). A 50-row leaderboard whose members each set
# a 100-char /nick can blow past that, so the message silently fails to
# send. We truncate on a row boundary to stay under the cap, mirroring
# what legacy *should* have done (it emitted the 400 instead).
#
# The cap and the "measure the *parsed* text" helper now live in
# ``utils.html`` — ``/help``'s catalog pagination needs exactly the same
# arithmetic, and two private copies of a Telegram limit is one copy too
# many for a number that must never drift.
_TRUNCATION_MARKER = "…"


def _join_within_limit(header: list[str], rows: list[str]) -> str:
    """Join ``header`` + as many ``rows`` as fit under Telegram's cap.

    ``header`` lines are always kept (they're tiny and bounded). Row
    lines are appended until the next one would push the rendered length
    over :data:`TELEGRAM_TEXT_LIMIT`; if any row is dropped a single
    ``…`` line is appended so the user can tell the list was clipped.
    """
    out = list(header)
    used = sum(visible_len(line) + 1 for line in out)  # +1 per newline
    # The marker is appended AFTER the loop, so budgeting the rows
    # against the full cap made the finished message
    # ``visible_len(marker) + 1`` units too long — 4097, i.e. the very
    # 400 this helper exists to prevent, in exactly the case it was
    # written for. Reserve the marker's cost while there is still a row
    # that could be dropped.
    reserved = visible_len(_TRUNCATION_MARKER) + 1
    last = len(rows) - 1
    truncated = False
    for position, line in enumerate(rows):
        cost = visible_len(line) + 1
        # The final row spends no marker if it fits, so it is measured
        # against the full cap: reserving unconditionally would clip a
        # leaderboard that fits exactly and then label it truncated.
        cap = TELEGRAM_TEXT_LIMIT - (0 if position == last else reserved)
        if used + cost > cap:
            truncated = True
            break
        out.append(line)
        used += cost
    if truncated:
        out.append(_TRUNCATION_MARKER)
    return "\n".join(out)


def _is_top_messages(message: Message, command: CommandObject) -> bool:
    """Tight filter: only own ``/top`` (bare) or ``/top messages [days]``.

    Returns ``True`` when the messages-mode handler should claim the
    update. Group-only — private chats are routed to the balance
    handler instead (see :func:`_is_top_balance`).
    """
    if message.chat is None or message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return False
    args = command_args(command).split()
    if not args:
        return True  # bare /top in a group → messages mode
    if args[0].lower() != "messages":
        return False
    if len(args) == 1:
        return True
    if len(args) > 2:
        return False
    try:
        days = int(args[1])
    except ValueError:
        return False
    return 1 <= days <= 365


def _is_top_balance(message: Message, command: CommandObject) -> bool:
    """Filter for the balance-mode handler (Stage 21).

    Claims:

    * Bare ``/top`` in a **private** chat — legacy default
      (bot.py:18727 picks ``balance`` when ``chat.type`` is private).
    * Explicit ``/top balance`` (case-insensitive) in **either** chat
      type — a power user can demand the balance ladder in a group
      and legacy honours it (bot.py:18766 is mode-only-dispatched, no
      chat-type guard).

    Anything else (``games``/``wins``/``streak``/unknown subcommand,
    extra args after ``balance``) falls through. The new pipeline
    only claims what it renders end-to-end.
    """
    if message.chat is None:
        return False
    args = command_args(command).split()
    if not args:
        # Bare ``/top`` — own it only in private chats (groups route
        # to the messages handler via :func:`_is_top_messages`).
        return message.chat.type == ChatType.PRIVATE
    if args[0].lower() != "balance":
        return False
    # ``/top balance`` with no extra arg — legacy ignores trailing
    # tokens (bot.py:18766 doesn't look past ``args[1]``), but we
    # reject them to keep the filter contract tight. A user typing
    # ``/top balance 7`` probably meant ``/top messages 7`` and is
    # better served by the falling-through legacy error than a
    # silently-truncated read.
    return len(args) == 1


def _make_mode_filter(mode: str):  # type: ignore[no-untyped-def]
    """Filter factory for the three economy-mode arms (games/wins/streak).

    Same contract as :func:`_is_top_balance` minus the bare-``/top``
    default: only an explicit subcommand match wins. Bare ``/top`` in
    private is still routed to the balance arm (legacy default at
    bot.py:18727) — these modes claim only their named keyword.
    """

    def _is(_message: Message, command: CommandObject) -> bool:
        args = command_args(command).split()
        if len(args) != 1:
            return False
        return args[0].lower() == mode

    _is.__name__ = f"_is_top_{mode}"
    return _is


_is_top_games = _make_mode_filter("games")
_is_top_wins = _make_mode_filter("wins")
_is_top_streak = _make_mode_filter("streak")


def _parse_days(command: CommandObject) -> int:
    """Pull the optional days arg, already validated by :func:`_is_top_messages`.

    Defaults to :data:`_DEFAULT_DAYS` when missing. ``int()`` can't
    raise here because the filter rejected non-int args upstream.
    """
    args = command_args(command).split()
    if len(args) == 2:
        return int(args[1])
    return _DEFAULT_DAYS


def _now_utc() -> datetime:
    """AWARE UTC ``now`` for the render-time VIP gate.

    ``EmojiBadgeRepo.active_badges`` compares this against ``vip_till``
    (a legacy unix timestamp) via ``.timestamp()``, which reads a
    *naive* value in the HOST's zone. This used to strip the tzinfo,
    so on the MSK production host the leaderboard kept rendering badges
    for VIPs that had lapsed up to three hours earlier. Same frame, and
    same reason, as ``handlers/emoji.py:_utcnow``.
    """
    return datetime.now(UTC)


async def _decorate_names(
    names: dict[int, str],
    rows: list[tuple[int, int]],
    badge_repo: EmojiBadgeRepo,
    *,
    lang: str,
) -> dict[int, str]:
    """Prefix VIP cosmetic badges (#25) onto the resolved display names.

    One batched JOIN against ``economy.users.vip_till`` resolves the
    currently-VIP subset; a lapsed grant simply omits the badge. The
    badge is a trusted ``VIP_BADGE_SET`` member so it needs no escaping
    even though the name it prefixes is HTML-escaped downstream by
    :func:`html_user_mention`. Returns a NEW dict — the input is left
    untouched so the empty-rows render path stays a pure read.

    ``lang`` is needed only for the missing-name fallback: a badge on a
    user whose row hasn't reached ``users.db`` yet still has to say
    *something*, and that something is copy.
    """
    badges = await badge_repo.active_badges([uid for uid, _ in rows], now=_now_utc())
    if not badges:
        return names
    decorated = dict(names)
    for uid, badge in badges.items():
        base = decorated.get(uid, "") or t("h_top_fallback_name", lang)
        decorated[uid] = f"{badge} {base}"
    return decorated


def _render_messages(
    rows: list[tuple[int, int]],
    names: dict[int, str],
    *,
    days: int,
    lang: str,
) -> str:
    """Build the messages leaderboard text. Empty → translated copy.

    Same i18n contract as its balance/games/wins/streak siblings —
    this arm was the last one still rendering Russian literals, so an
    English user got the whole ladder in Russian while every other
    ``/top`` mode answered in their language.

    The period label is its own pair of keys rather than an inline
    ternary: "today" vs "N days" is exactly the kind of phrase a
    locale reshapes (plural forms, word order), and ``days`` is an int
    from a validated filter, so nothing here needs escaping.
    """
    if not rows:
        return t("h_top_messages_empty", lang)

    period = (
        t("h_top_messages_period_today", lang)
        if days == 1
        else t("h_top_messages_period_days", lang, days=days)
    )
    header = [t("h_top_messages_header", lang, period=period), ""]
    body: list[str] = []
    for idx, (user_id, count) in enumerate(rows, start=1):
        medal = _MEDALS.get(idx, f"{idx}.")
        raw_name = names.get(user_id, "") or t("h_top_fallback_name", lang)
        mention = html_user_mention(user_id, raw_name)
        body.append(t("h_top_messages_row", lang, medal=medal, mention=mention, count=count))
    return _join_within_limit(header, body)


def _render_balance(
    rows: list[tuple[int, int]],
    names: dict[int, str],
    *,
    lang: str,
) -> str:
    """Build the balance leaderboard text. Empty → translated copy.

    Row template lives in i18n (``h_top_balance_row``) so translators
    can reorder ``{mention}`` / ``{balance}`` without a code change —
    Russian and English happen to agree on order today but future
    locales (Japanese, Arabic) may not.
    """
    if not rows:
        return t("h_top_balance_empty", lang)
    header = [t("h_top_balance_header", lang), ""]
    body: list[str] = []
    for idx, (user_id, balance) in enumerate(rows, start=1):
        medal = _MEDALS.get(idx, f"{idx}.")
        raw_name = names.get(user_id, "") or t("h_top_fallback_name", lang)
        mention = html_user_mention(user_id, raw_name)
        body.append(
            t(
                "h_top_balance_row",
                lang,
                medal=medal,
                mention=mention,
                balance=format_number(balance),
            )
        )
    return _join_within_limit(header, body)


def _render_mode(
    rows: list[tuple[int, int]],
    names: dict[int, str],
    *,
    lang: str,
    mode: str,
) -> str:
    """Generic renderer for the games/wins/streak ladders.

    Pulls the three keys ``h_top_{mode}_header / _empty / _row`` from
    i18n. Same row template contract as balance: ``{medal} {mention}
    {value}``. Kept generic instead of three copy-paste functions so a
    future locale tweak edits one call site rather than three.

    Plural forms ride the same generic key shape: the row template
    carries ``{noun}`` and the form is picked from ``h_plural_{mode}``
    by :func:`~telegram_invite_bot.utils.plural.plural`. Three modes,
    three noun families, no branch here. Before that the templates each
    froze one form, so the games ladder greeted a first-time player with
    "1 игр" — and since row 1 is the top of the board, the wrong form
    was the most-read string on the screen.

    ``value`` is passed twice on purpose: ``format_number`` for display,
    raw for the picker. A thousands separator would break the modulo the
    Russian rule runs on.
    """
    if not rows:
        return t(f"h_top_{mode}_empty", lang)
    header = [t(f"h_top_{mode}_header", lang), ""]
    body: list[str] = []
    for idx, (user_id, value) in enumerate(rows, start=1):
        medal = _MEDALS.get(idx, f"{idx}.")
        raw_name = names.get(user_id, "") or t("h_top_fallback_name", lang)
        mention = html_user_mention(user_id, raw_name)
        body.append(
            t(
                f"h_top_{mode}_row",
                lang,
                medal=medal,
                mention=mention,
                value=format_number(value),
                noun=plural(value, f"h_plural_{mode}", lang),
            )
        )
    return _join_within_limit(header, body)


def build_router(registry: EngineRegistry, stats_config: StatsConfig) -> Router:
    """Wire the router with message-stats + users + economy sessions.

    ``stats_config.timezone`` is captured for the calendar-boundary —
    same TZ policy as ``/stats`` (Stage 11) so "today" lines up across
    handlers and "yesterday's last messages" don't appear in one and
    not the other.

    Three outer middlewares (message-stats / users / economy) because
    each handler arm reads from a different DB. The cost of attaching
    a middleware to a router-level event is one no-op session open
    when the other arm fires — the trade-off is one shared router
    surface for ``/top`` (vs. splitting into two routers and forcing
    callers to include both). Stage 21 chooses the shared surface.
    """
    tz = ZoneInfo(stats_config.timezone)

    async def handle_top_messages(
        message: Message,
        command: CommandObject,
        message_stats_repo: MessageStatsRepo,
        users_repo: UsersRepo,
        emoji_badge_repo: EmojiBadgeRepo,
        lang: str,
    ) -> None:
        if message.chat is None:
            return
        days = _parse_days(command)
        today = datetime.now(tz).date()
        rows = await message_stats_repo.top_users_by_messages(
            message.chat.id,
            days=days,
            today=today,
            limit=_DEFAULT_LIMIT,
        )
        names = await users_repo.first_names_by_ids([uid for uid, _ in rows])
        names = await _decorate_names(names, rows, emoji_badge_repo, lang=lang)
        await message.answer(_render_messages(rows, names, days=days, lang=lang))
        log.bind(
            chat_id=message.chat.id,
            days=days,
            rows=len(rows),
            lang=lang,
        ).info("/top messages rendered")

    async def handle_top_balance(
        message: Message,
        economy_repo: EconomyRepo,
        users_repo: UsersRepo,
        emoji_badge_repo: EmojiBadgeRepo,
        lang: str,
    ) -> None:
        if message.chat is None:
            return
        rows = await economy_repo.top_by_balance(limit=_BALANCE_LIMIT)
        names = await users_repo.first_names_by_ids([uid for uid, _ in rows])
        names = await _decorate_names(names, rows, emoji_badge_repo, lang=lang)
        await message.answer(_render_balance(rows, names, lang=lang))
        log.bind(
            chat_id=message.chat.id,
            rows=len(rows),
            lang=lang,
        ).info("/top balance rendered")

    async def _handle_mode(
        message: Message,
        economy_repo: EconomyRepo,
        users_repo: UsersRepo,
        emoji_badge_repo: EmojiBadgeRepo,
        lang: str,
        *,
        mode: str,
    ) -> None:
        if message.chat is None:
            return
        fetchers = {
            "games": economy_repo.top_by_games,
            "wins": economy_repo.top_by_wins,
            "streak": economy_repo.top_by_streak,
        }
        rows = await fetchers[mode](limit=_BALANCE_LIMIT)
        names = await users_repo.first_names_by_ids([uid for uid, _ in rows])
        names = await _decorate_names(names, rows, emoji_badge_repo, lang=lang)
        await message.answer(_render_mode(rows, names, lang=lang, mode=mode))
        log.bind(
            chat_id=message.chat.id,
            rows=len(rows),
            lang=lang,
            mode=mode,
        ).info(f"/top {mode} rendered")

    async def handle_top_games(
        message: Message,
        economy_repo: EconomyRepo,
        users_repo: UsersRepo,
        emoji_badge_repo: EmojiBadgeRepo,
        lang: str,
    ) -> None:
        await _handle_mode(message, economy_repo, users_repo, emoji_badge_repo, lang, mode="games")

    async def handle_top_wins(
        message: Message,
        economy_repo: EconomyRepo,
        users_repo: UsersRepo,
        emoji_badge_repo: EmojiBadgeRepo,
        lang: str,
    ) -> None:
        await _handle_mode(message, economy_repo, users_repo, emoji_badge_repo, lang, mode="wins")

    async def handle_top_streak(
        message: Message,
        economy_repo: EconomyRepo,
        users_repo: UsersRepo,
        emoji_badge_repo: EmojiBadgeRepo,
        lang: str,
    ) -> None:
        await _handle_mode(message, economy_repo, users_repo, emoji_badge_repo, lang, mode="streak")

    router = Router(name="top")
    # Three outer middlewares — read-only, order doesn't matter.
    # Each handler arm picks the repos it needs from ``data``;
    # unused sessions cost one no-op open + close per dispatch (cheap
    # enough that splitting the router would be the wrong trade).
    router.message.middleware(MessageStatsMiddleware(registry))
    router.message.middleware(SessionMiddleware(registry))
    router.message.middleware(EconomyMiddleware(registry))
    # Register balance BEFORE messages so the bare-``/top``-in-private
    # case is claimed by the balance arm (the messages filter would
    # have rejected it anyway — both arms are mutually exclusive by
    # chat-type — but registration order is the explicit contract).
    router.message.register(
        handle_top_balance,
        Command("top", "kom_top", ignore_case=True),
        _is_top_balance,
    )
    router.message.register(
        handle_top_messages,
        Command("top", "kom_top", ignore_case=True),
        _is_top_messages,
    )
    router.message.register(
        handle_top_games,
        Command("top", "kom_top", ignore_case=True),
        _is_top_games,
    )
    router.message.register(
        handle_top_wins,
        Command("top", "kom_top", ignore_case=True),
        _is_top_wins,
    )
    router.message.register(
        handle_top_streak,
        Command("top", "kom_top", ignore_case=True),
        _is_top_streak,
    )
    return router
