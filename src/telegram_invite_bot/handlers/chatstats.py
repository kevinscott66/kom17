"""``/chatstats`` — group activity + economy card (A-08, enriched by RR-1 #6).

Legacy ``cmd_chatstats`` (bot.py:40980) answered with a **six-block**
card plus a PNG activity preview and a two-button keyboard. The A-08
port kept only the middle of that — members / activity / top-3 as plain
text — and explicitly deferred the rest. RR-1 #6 brings the deferred
blocks back, so the card is again:

* 👥 **Участники** — live member total, distinct posters over 7 days,
  and how many of them are new this week.
* 💬 **Активность** — today / week / daily average.
* 🎮 **Игры** — games played today and lifetime.
* 💰 **Экономика** — coins in circulation, mean balance, richest wallet.
* 💸 **Донаты** — group total + rating position (omitted when the group
  has never received one, exactly like legacy's falsy-stats branch).
* 💬 **Топ-3 по сообщениям** (за 7 дн.).

…under a PNG activity preview (:func:`~utils.stats_image.render_stats_card`,
the same generator ``/stats`` and ``/profile`` use) and a
🏆 rating / 💝 boost keyboard.

Behaviour parity & deltas:

* GROUPS/SUPERGROUPS only, but NOT admin-only — any non-banned member.
  Like ``/duel`` (bot.py:21227) the gate is in-handler (not a router
  filter) so a private invocation gets the localised "only in group"
  refusal instead of silently falling through. ``/groupstats`` routes
  its private spelling to the rating leaderboard; ``/chatstats`` has no
  such dual mode, so private simply refuses.
* No args (legacy uses fixed windows: today + last 7 days). ``/chatstats
  foo`` falls through, same convention as ``/stats``.
* Window boundary pinned to :pyattr:`StatsConfig.timezone` (same policy
  as ``/stats`` / ``/top``) so "today" lines up across handlers —
  including the games window, which legacy read with SQLite
  ``date(date) = date('now')``, i.e. a **UTC** calendar day, while every
  other block on the same card was already stats-TZ. On a
  ``Europe/Moscow`` deploy that made "Игры сегодня" name a window three
  hours off from "Сообщений сегодня" directly above it.
* READ-ONLY: legacy's ``donations_ensure_group`` upsert (it created the
  aggregate row as a side effect of *reading* the card) and the
  auto-delete side effects are intentionally skipped. A group with no
  aggregate row simply has no 💸 block.
* "New this week" is :meth:`MessageStatsRepo.newcomer_count`, not
  legacy's ``members_count - _count_notified_users()`` — see that
  method's docstring. Note the deliberate widening: legacy rendered the
  segment only in the configured main chat and omitted it everywhere
  else; the new counter is derived per-chat from data we own, so every
  group gets the line.
* Keyboard: legacy's first button opened ``donate_menu_{chat_id}``, a
  payment flow with no counterpart in the new pipeline. The card
  therefore reuses ``groupstats._keyboard`` verbatim — one shared row
  means the two cards can't drift, and both send a tapper to live
  destinations instead of one dead-linking a half-ported menu.
* Member total comes from ``bot.get_chat_member_count``; the whole
  members line degrades to the counters we own if that call fails, since
  one flaky API read must not cost the user the other five blocks.
* Top-user display name: legacy fetched the live
  ``bot.get_chat_member(chat_id, user_id).user.first_name`` with an
  ``ID{uid}`` fallback. Mirrored via ``message.bot.get_chat_member``
  wrapped in try/except; the name is HTML-escaped before embedding.
* Caption fitting: legacy truncated at 1000 chars with
  ``_truncate_preserving_emoji``, which can cut inside an ``<a href>``
  tag and make Telegram reject the *whole* message under
  ``parse_mode=HTML``. Here whole trailing blocks are dropped instead —
  see :func:`_fit`.

Bot exclusion: the spec's "exclude bot users" for the top list has no
counterpart in this schema — ``message_counts`` carries no is-bot flag
and the A-03 counter only records human senders (it skips
``message.from_user.is_bot``), so bot rows never enter the table. No
extra filter is therefore needed; noted for the reviewer. The 💰 block
inherits the wider, known gap documented on
:meth:`EconomyRepo.economy_snapshot`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.groupstats import _fetch_aggregate, _keyboard
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.message_stats import MessageStatsMiddleware
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.html import html_user_mention
from telegram_invite_bot.utils.numbers import format_number
from telegram_invite_bot.utils.stats_image import render_stats_card

log = logger.bind(component="handlers.chatstats")

if TYPE_CHECKING:
    from datetime import date as date_cls

    from aiogram import Bot
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import StatsConfig
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomySnapshot
    from telegram_invite_bot.repositories.message_stats_repo import MessageStatsRepo
    from telegram_invite_bot.services.user_service import UserService

# Command spellings — legacy registered ``/chatstats`` + ``/cstats``.
# ``/chatinfo`` is a legacy thin alias that called ``cmd_chatstats`` (so it
# renders the same activity card); folding it in here matches that
# behaviour (CMD-2). Plain-text aliases (``чат инфа`` …) route via the
# alias router (A-06).
_CHATSTATS_COMMANDS = ("chatstats", "cstats", "chatinfo")
_WEEK_DAYS = 7
_TOP_LIMIT = 3
_COIN = "🪙"
# Windows drawn as bars on the PNG preview. Legacy fed
# ``normalize_stats_periods(STATS_PERIOD_DAYS)`` here; the set is pinned
# instead so the picture is the same shape in every deploy — the
# configurable window still governs the text card's numbers.
_PREVIEW_DAYS: tuple[int, ...] = (1, 7, 30)
# Telegram's photo-caption ceiling. Legacy cut at 1000 to leave slack for
# its own truncation marker; :func:`_fit` drops whole blocks instead, so
# the real limit is usable directly.
_CAPTION_MAX = 1024
# Blocks :func:`_fit` will never drop: 👥 members + 💬 activity, the pair
# that makes the card a stats card at all. Both are built from our own
# integers, so they cannot be inflated by user input and a hard floor
# here can never turn into a runaway.
_MIN_BLOCKS = 2
# Display-name cap, same value and same reasoning as ``handlers/stats.py``.
_NAME_MAX = 48

# /topactive (CMD-2): a window-parametrised top-N message leaderboard.
# Legacy ``cmd_top_activity`` defaulted to 7 days, clamped 1..30, showed
# up to 10 non-bot users. ``message_counts`` carries no is-bot flag and
# the A-03 counter never records bots, so no extra exclusion is needed.
_TOPACTIVE_COMMANDS = ("topactive", "top_activity")
_TOPACTIVE_DEFAULT_DAYS = 7
_TOPACTIVE_MIN_DAYS = 1
_TOPACTIVE_MAX_DAYS = 30
_TOPACTIVE_LIMIT = 10
_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _parse_days(raw: str | None) -> int:
    """``/topactive [days]`` → clamped window (default 7, 1..30)."""
    if not raw:
        return _TOPACTIVE_DEFAULT_DAYS
    try:
        days = int(raw.split()[0])
    except (ValueError, IndexError):
        return _TOPACTIVE_DEFAULT_DAYS
    return max(_TOPACTIVE_MIN_DAYS, min(_TOPACTIVE_MAX_DAYS, days))


def _render_topactive(
    lang: str, *, days: int, top: list[tuple[int, int]], names: dict[int, str]
) -> str:
    """Build the HTML top-activity card (medals for the podium)."""
    if not top:
        return t("h_topactive_empty", lang, days=days)
    unit = t("h_chatstats_messages_unit", lang)
    lines = [f"📊 <b>{t('h_topactive_title', lang, days=days)}</b>", ""]
    for idx, (uid, count) in enumerate(top, start=1):
        rank = _MEDALS.get(idx, f"{idx}.")
        mention = html_user_mention(uid, names.get(uid) or f"ID{uid}")
        lines.append(f"{rank} {mention} — {format_number(count)} {unit}")
    return "\n".join(lines)


async def _resolve_name(bot: Bot, chat_id: int, user_id: int) -> str:
    """Live ``first_name`` via ``get_chat_member``; ``ID{uid}`` on failure.

    Mirrors legacy, which read the member's live profile name and fell
    back to ``ID{uid}`` when the lookup raised (user left, privacy, API
    error). The name is escaped by :func:`html_user_mention` at render
    time, so this returns the raw *unescaped* string — but truncated,
    like ``handlers/stats.py`` does at the same boundary.

    Truncation is not cosmetic. A 64-character ``first_name`` made
    entirely of ``'`` escapes to 384 characters, and both consumers
    embed several of these in one message: three rows on the
    ``/chatstats`` caption (1024-char ceiling) and ten on ``/topactive``
    (4096). Without a cap a couple of hostile display names could push
    the caption past the limit and silently cost every reader the whole
    💬 block — see :func:`_fit`, which is the last line of defence
    rather than the first.
    """
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:  # noqa: BLE001 — any API failure → deterministic fallback
        return f"ID{user_id}"
    name = (member.user.first_name or "").strip()
    return name[:_NAME_MAX] if name else f"ID{user_id}"


async def _resolve_member_count(bot: Bot | None, chat_id: int) -> int | None:
    """Live member total, or ``None`` when Telegram won't say.

    ``None`` is a real state, not an error: the bot can be mid-restart,
    rate-limited, or freshly demoted. It renders as a dropped "Всего"
    segment rather than a fake ``0``, because a zero-member group is a
    thing a reader would believe.
    """
    if bot is None:
        return None
    try:
        return int(await bot.get_chat_member_count(chat_id))
    except Exception as exc:  # noqa: BLE001 — decorative segment, never fatal
        log.bind(chat_id=chat_id).debug("member count unavailable: {e!r}", e=exc)
        return None


def _utc_day_bounds(day: date_cls, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """``tz``-local calendar day → the naive-UTC half-open range for it.

    ``economy.games.date`` is written by
    :meth:`EconomyRepo.record_game` as ``datetime.now(UTC).replace(
    tzinfo=None)`` — naive, UTC-valued. Comparing a tz-aware bound
    against that column matches nothing in SQLite, so the aware local
    midnight is converted and then stripped. Doing the conversion here
    (rather than inside the repo) keeps the timezone policy in the one
    layer that reads ``StatsConfig``.
    """
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )


def _members_block(lang: str, *, total: int | None, active: int, newcomers: int) -> list[str]:
    """👥 block. Segments are joined rather than templated because the
    "Всего" one disappears when Telegram won't report the count — a
    single template would have to render an empty ``Всего:`` label."""
    segments = []
    if total is not None:
        segments.append(f"{t('h_chatstats_members_total', lang)}: {format_number(total)}")
    segments.append(f"{t('h_chatstats_members_week', lang)}: {format_number(active)}")
    segments.append(f"{t('h_chatstats_members_new', lang)}: {format_number(newcomers)}")
    return [
        f"👥 <b>{t('h_chatstats_members', lang)}</b>",
        " • " + " | ".join(segments),
    ]


def _top_block(lang: str, *, top: list[tuple[int, int]], names: dict[int, str]) -> list[str]:
    """💬 Топ-3 block — the only one carrying user-controlled text."""
    lines = [f"💬 <b>{t('h_chatstats_top_title', lang)}</b>"]
    if not top:
        lines.append(f" • {t('h_chatstats_top_empty', lang)}")
        return lines
    unit = t("h_chatstats_messages_unit", lang)
    for idx, (uid, count) in enumerate(top, start=1):
        # ``html_user_mention`` escapes the display string itself.
        mention = html_user_mention(uid, names.get(uid) or f"ID{uid}")
        lines.append(f" {idx}. {mention} — {format_number(count)} {unit}")
    return lines


def _trim_markup_safe(caption: str, limit: int) -> str:
    """Cut ``caption`` to ``limit`` without leaving broken HTML behind.

    The only caller is :func:`_fit`, and only on the branch where every
    droppable block is already gone. A plain slice can land inside
    ``<a href="tg://user?id=…">``, inside the closing ``</a>``, or
    inside an ``&#x27;`` entity; Telegram rejects the whole message in
    all three cases, so the "slightly short card" the truncation was
    meant to produce becomes no card at all.

    Three retreats, applied in order, each one only ever shortening
    further:

    * a ``<`` with no ``>`` after it opens a tag the cut split — drop
      back to before it;
    * an ``&`` with no ``;`` after it opens an entity — same;
    * an ``<a `` with no matching ``</a>`` is a complete tag but an
      unbalanced element — drop back to before the anchor opened.

    Worst case every retreat fires and the result is short. That is the
    correct trade: this path is unreachable at today's constants, and
    when a future block does reach it a truncated card beats a rejected
    one.
    """
    if len(caption) <= limit:
        return caption
    head = caption[:limit]
    if head.rfind("<") > head.rfind(">"):
        head = head[: head.rfind("<")]
    if head.rfind("&") > head.rfind(";"):
        head = head[: head.rfind("&")]
    if head.count("<a ") > head.count("</a>"):
        head = head[: head.rfind("<a ")]
    return head.rstrip()


def _fit(blocks: list[list[str]], limit: int = _CAPTION_MAX) -> str:
    """Join ``blocks`` with blank lines, dropping trailing ones to fit.

    Legacy truncated the finished string mid-character
    (``_truncate_preserving_emoji``). Under ``parse_mode=HTML`` that can
    land inside ``<a href="tg://user?id=…">`` and Telegram then rejects
    the entire message — the user gets nothing instead of a slightly
    short card. Dropping whole blocks keeps every surviving line valid
    markup, and because ``pop()`` takes from the end, the block that
    goes first is the last one appended: the 💬 top-3, which is both the
    least load-bearing and the only one carrying user-controlled text.

    ``len`` here counts raw HTML source, while Telegram bills the
    *parsed* text (tags and ``&#x27;`` entities are free, emoji cost two
    UTF-16 units). The measurement is therefore pessimistic by roughly
    the weight of the ``<a href>`` wrappers — deliberately so: erring
    towards dropping a block one row early is recoverable, erring the
    other way is a rejected message.

    With :data:`_NAME_MAX` capping the only unbounded input, this is now
    a genuine backstop rather than a live path — six one-line blocks
    plus three capped top rows run to about a third of the budget. It
    still must not be an ``assert``: a future block (or a longer locale)
    should degrade the card, not kill it.

    Once ``_MIN_BLOCKS`` is reached there is nothing left to drop, and
    the final cut used to be a bare ``[:limit]`` — the very mid-tag
    split the paragraph above says the whole design exists to avoid
    (#762). :func:`_trim_markup_safe` does that last cut instead.
    """
    kept = list(blocks)
    while len(kept) > _MIN_BLOCKS:
        caption = "\n\n".join("\n".join(block) for block in kept)
        if len(caption) <= limit:
            return caption
        kept.pop()
    return _trim_markup_safe("\n\n".join("\n".join(block) for block in kept), limit)


def _render(
    lang: str,
    *,
    members: int | None,
    active: int,
    newcomers: int,
    today: int,
    week: int,
    avg: int,
    games_today: int,
    economy: EconomySnapshot,
    donations: tuple[int, int | None] | None,
    top: list[tuple[int, int]],
    names: dict[int, str],
) -> str:
    """Build the HTML card (parse_mode=HTML bot-wide), legacy block order."""
    blocks: list[list[str]] = [
        _members_block(lang, total=members, active=active, newcomers=newcomers),
        [
            f"💬 <b>{t('h_chatstats_activity', lang)}</b>",
            # Substitution goes through ``t`` rather than ``.format`` on
            # its result: ``t`` uses a forgiving formatter, so a
            # placeholder that gets renamed in the YAML renders as
            # literal ``{today}`` instead of raising ``KeyError`` and
            # taking the whole card down with it.
            " • "
            + t(
                "h_chatstats_activity_line",
                lang,
                today=format_number(today),
                week=format_number(week),
                avg=format_number(avg),
            ),
        ],
        [
            f"🎮 <b>{t('h_chatstats_games', lang)}</b>",
            " • "
            + t(
                "h_chatstats_games_line",
                lang,
                today=format_number(games_today),
                total=format_number(economy.total_games),
            ),
        ],
        [
            f"💰 <b>{t('h_chatstats_economy', lang)}</b>",
            " • "
            + t(
                "h_chatstats_economy_line",
                lang,
                # Legacy printed the raw float mean, so a busy economy
                # rendered "1 234.5678901". Rounded to whole coins: the
                # balances it averages are integers anyway.
                coins=f"{format_number(economy.total_coins)} {_COIN}",
                avg=f"{format_number(round(economy.avg_balance))} {_COIN}",
                max=f"{format_number(economy.max_balance)} {_COIN}",
            ),
        ],
    ]
    if donations is not None:
        donated_xp, position = donations
        blocks.append(
            [
                f"💸 <b>{t('h_chatstats_donations', lang)}</b>",
                " • "
                + t(
                    "h_chatstats_donations_line",
                    lang,
                    total=f"{format_number(donated_xp)} {_COIN}",
                    position=str(position) if position else "—",
                ),
            ]
        )
    blocks.append(_top_block(lang, top=top, names=names))
    return _fit(blocks)


def build_router(registry: EngineRegistry, stats_config: StatsConfig) -> Router:
    """Factory — fresh ``Router`` + ``MessageStatsMiddleware`` per call.

    ``stats_config.timezone`` is captured for the calendar boundary, the
    same policy ``/stats`` and ``/top`` use so "today" lines up across
    the activity handlers. Router-level chat-type filtering is *not*
    applied: the group gate lives in the handler so a private invocation
    gets the localised refusal rather than falling through to a deleted
    legacy bridge.

    ``economy.db`` is reached through a handler-local session rather than
    :class:`EconomyMiddleware`: only ``/chatstats`` needs it, and the
    middleware is router-scoped — attaching it would open an economy
    session for every ``/topactive`` too, which is exactly the cost that
    middleware's own docstring says to avoid.
    """
    tz = ZoneInfo(stats_config.timezone)
    economy_sessionmaker = registry.session(DBName.ECONOMY)

    async def _economy_blocks(day: date_cls) -> tuple[EconomySnapshot, int]:
        """The 💰 + 🎮 numbers, both off one read-only economy session."""
        start, end = _utc_day_bounds(day, tz)
        async with economy_sessionmaker() as session:
            repo = EconomyRepo(session)
            return (
                await repo.economy_snapshot(),
                await repo.count_games_between(start=start, end=end),
            )

    async def handle_chatstats(
        message: Message,
        user_service: UserService,
        message_stats_repo: MessageStatsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        user = await user_service.touch(require_from_user(message))
        lang = user.language
        # #220: the render below resolves up to three display names
        # (``_TOP_LIMIT``) and the member count, one Telegram call each,
        # and then uploads a chart.
        # The ``touch`` above is bookkeeping that stands either way, so
        # end its transaction before the fan-out instead of holding
        # ``users.db``'s single writer slot through it. See
        # :class:`db.session.Checkpoint`.
        if checkpoint is not None:
            await checkpoint()
        if message.chat.type not in GROUP_TYPES:
            await message.reply(t("h_chatstats_group_only", lang))
            log.bind(chat_id=message.chat.id, user_id=user.user_id).debug(
                "/chatstats refused outside group"
            )
            return

        chat_id = message.chat.id
        now = datetime.now(tz)
        today = now.date()
        today_count = await message_stats_repo.chat_total_for_days(chat_id, days=1, today=today)
        week_count = await message_stats_repo.chat_total_for_days(
            chat_id, days=_WEEK_DAYS, today=today
        )
        active = await message_stats_repo.active_user_count(chat_id, days=_WEEK_DAYS, today=today)
        newcomers = await message_stats_repo.newcomer_count(chat_id, days=_WEEK_DAYS, today=today)
        top = await message_stats_repo.top_users_by_messages(
            chat_id, days=_WEEK_DAYS, today=today, limit=_TOP_LIMIT
        )
        avg = week_count // _WEEK_DAYS
        economy, games_today = await _economy_blocks(today)
        # A group that never received a donation has no aggregate row and
        # simply loses the 💸 block — legacy rendered the same nothing via
        # its ``if group_don and group_don.get("group")`` guard. Unlike
        # legacy we do NOT create the row as a side effect of reading.
        aggregate = await _fetch_aggregate(registry, chat_id)
        donations: tuple[int, int | None] | None = None
        if aggregate is not None:
            # The printed total is ``group_xp``, NOT ``total_donations``.
            # Legacy's dict key ``total_donations`` (read at
            # bot.py:41053) was built from ``COALESCE(group_xp, 0)``
            # (bot.py:10812, :10838-10839), so legacy printed XP under
            # that label. The real ``total_donations`` column is the
            # group *treasury*: ``treasury_repo.py:96-99`` debits it when
            # the owner withdraws, and ``donations_rating_repo.py:147-151``
            # deliberately never credits it on a donation. Printing that
            # column would make a lifetime counter go *down* after a
            # withdrawal and read 0 for every group whose donations
            # arrived through the new pipeline (#475). ``group_name`` is
            # dropped on purpose — the card already sits inside the group
            # it describes.
            _group_name, _treasury, group_xp, rating_position = aggregate
            donations = (group_xp, rating_position)

        bot = message.bot
        names: dict[int, str] = {}
        if bot is not None:
            for uid, _count in top:
                names[uid] = await _resolve_name(bot, chat_id, uid)
        members = await _resolve_member_count(bot, chat_id)

        caption = _render(
            lang,
            members=members,
            active=active,
            newcomers=newcomers,
            today=today_count,
            week=week_count,
            avg=avg,
            games_today=games_today,
            economy=economy,
            donations=donations,
            top=top,
            names=names,
        )
        keyboard = _keyboard(lang)
        # The 1- and 7-day bars are the numbers the 💬 block already
        # printed; re-querying them would spend two round-trips to
        # re-derive values that must agree with the caption anyway.
        known: dict[int, int] = {1: today_count, _WEEK_DAYS: week_count}
        preview_rows = [
            (
                t("h_chatstats_period_today", lang)
                if days == 1
                else t("h_chatstats_period_days", lang, days=days),
                known[days]
                if days in known
                else await message_stats_repo.chat_total_for_days(chat_id, days=days, today=today),
            )
            for days in _PREVIEW_DAYS
        ]

        mode = "png"
        try:
            # Pure-CPU rasterisation — off the event loop.
            png = await asyncio.to_thread(
                render_stats_card,
                t("h_chatstats_title", lang),
                preview_rows,
                subtitle=t("h_chatstats_card_subtitle", lang),
                generated_at=now,
            )
        except Exception as exc:  # noqa: BLE001 - the preview is decorative
            # Same posture as ``/stats``: Pillow's failure family here is
            # scattered (OSError, ValueError, struct.error on a truncated
            # font) and none of it should cost the user six blocks of
            # numbers the caption already carries in full.
            mode = "text"
            log.bind(chat_id=chat_id).warning("chatstats preview render failed: {e!r}", e=exc)
            await message.reply(caption, reply_markup=keyboard)
        else:
            try:
                await message.reply_photo(
                    BufferedInputFile(png, filename="chat_stats.png"),
                    caption=caption,
                    reply_markup=keyboard,
                )
            except TelegramBadRequest as exc:
                # 400 only — "not enough rights to send photos" and friends,
                # where plain text is still permitted so the retry actually
                # rescues the card. A network error is deliberately NOT
                # caught: it can land *after* Telegram accepted the photo,
                # and retrying would double-post the whole card.
                mode = "text"
                log.bind(chat_id=chat_id).warning("chatstats preview refused: {e!r}", e=exc)
                await message.reply(caption, reply_markup=keyboard)
        log.bind(chat_id=chat_id, user_id=user.user_id, top=len(top), mode=mode).info(
            "/chatstats rendered"
        )

    async def handle_topactive(
        message: Message,
        command: CommandObject,
        user_service: UserService,
        message_stats_repo: MessageStatsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        user = await user_service.touch(require_from_user(message))
        lang = user.language
        # #220: up to ten sequential ``_resolve_name`` round-trips below.
        if checkpoint is not None:
            await checkpoint()
        if message.chat.type not in GROUP_TYPES:
            await message.reply(t("h_chatstats_group_only", lang))
            return

        chat_id = message.chat.id
        days = _parse_days(command.args)
        today = datetime.now(tz).date()
        top = await message_stats_repo.top_users_by_messages(
            chat_id, days=days, today=today, limit=_TOPACTIVE_LIMIT
        )
        bot = message.bot
        names: dict[int, str] = {}
        if bot is not None:
            for uid, _count in top:
                names[uid] = await _resolve_name(bot, chat_id, uid)

        await message.reply(_render_topactive(lang, days=days, top=top, names=names))
        log.bind(chat_id=chat_id, user_id=user.user_id, days=days, top=len(top)).info(
            "/topactive rendered"
        )

    router = Router(name="chatstats")
    router.message.middleware(MessageStatsMiddleware(registry))
    router.message.register(
        handle_chatstats,
        Command(*_CHATSTATS_COMMANDS, ignore_case=True, magic=F.args.is_(None)),
        F.from_user,
    )
    # /topactive takes an optional ``[days]`` arg, so it is NOT gated on
    # ``F.args.is_(None)`` (that's the chatstats family's no-args rule).
    router.message.register(
        handle_topactive,
        Command(*_TOPACTIVE_COMMANDS, ignore_case=True),
        F.from_user,
    )
    return router
