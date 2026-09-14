"""``/stats`` — per-user message activity card (+ ``/group_stats`` chat-wide).

RR-1 #5 restores what the monolith→split lost. Legacy ``cmd_user_stats``
(bot.py:41092) answered ``/stats`` with a **per-user** PNG card plus a
today/7d/30d/total summary, a 7-day breakdown table and a *reply-target*
mode (``/stats`` in reply to someone = their numbers). The split
collapsed all of that into a flat chat-wide per-day list. Both surfaces
are worth keeping, so they now live side by side:

* ``/stats`` / ``/статистика`` → the per-user card (legacy parity).
* ``/group_stats`` → the chat-wide per-day totals shipped at Stage 11.

Legacy parity notes for the per-user path:

* Target is ``message.reply_to_message.from_user`` when replying, else
  the sender. Bots are refused outright ("боты не копят статистику").
* The card is a real PNG (:func:`~utils.stats_image.render_stats_card`),
  the same generator ``/profile`` uses, so the visual language of the two
  activity surfaces stays consistent. Generation is pure-CPU and runs in
  ``asyncio.to_thread``.
* Rendering **and delivery** are guarded: a Pillow failure *or* a
  ``sendPhoto`` refusal (group with media disabled, flood wait) degrades
  to a text card carrying the same numbers. Legacy wrapped its send in
  the same try (bot.py:41170-41190) — a broken rasteriser or a picky
  chat must never eat the user's stats.
* The 7-day breakdown legacy only showed in its text fallback is
  promoted into the caption here, densified to a full week and drawn
  with block-glyph bars (bounded: exactly 7 short rows, well inside
  Telegram's 1024-char caption limit).
* Counters only exist for groups (``MessageActivityMiddleware`` records
  nothing in private chats), so a DM ``/stats`` answers with a short
  "groups only" line instead of a full-size card of zeros.

Chat-wide parity notes (unchanged from Stage 11):

* Window is :pyattr:`StatsConfig.period_days` (default 7) ending on
  "today" in :pyattr:`StatsConfig.timezone`. Legacy used SQLite
  ``date('now')`` (server-local, undocumented TZ); pinning it via
  config makes the calendar boundary deterministic across deploys.
* Zero-row case reuses legacy's exact "сообщений нет" copy.
* No args on either command — ``/stats foo`` falls through, same
  convention as ``/balance @other`` (Stage 7).
"""

from __future__ import annotations

import asyncio
import html
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.message_stats import MessageStatsMiddleware
from telegram_invite_bot.repositories.message_stats_repo import DailyCount
from telegram_invite_bot.utils.render import paginate_lines
from telegram_invite_bot.utils.stats_image import render_stats_card

log = logger.bind(component="handlers.stats")

if TYPE_CHECKING:
    from datetime import date as date_cls

    from aiogram.types import Message
    from aiogram.types import User as TgUser

    from telegram_invite_bot.config.settings import StatsConfig
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.message_stats_repo import MessageStatsRepo

# Period windows on the card, in days, paired with the label key. The
# labels are deliberately borrowed from the ``/profile`` namespace
# (``h_profile_msg_*``): both surfaces show the same four numbers, and
# sharing the keys is what keeps them worded identically — a rename lands
# in one place instead of drifting between two cards.
_PERIODS: tuple[tuple[int, str], ...] = (
    (1, "h_profile_msg_today"),
    (7, "h_profile_msg_7d"),
    (30, "h_profile_msg_30d"),
)
# Display names go into a one-line caption header and into a card title
# that ``stats_image.render_stats_card`` truncates to 60 *characters*.
# Cap here so a 64-char Telegram name doesn't push the header onto three
# wrapped lines and shove the numbers below the fold. Note the two
# constants are coupled with no slack: the ru title prefix
# "Активность: " is 12 chars, and 12 + 48 lands exactly on the card's 60.
_NAME_MAX = 48
# How many days the caption breakdown table shows. Legacy's fallback used
# ``date('now','-6 days')`` — today + 6 = 7 rows.
_BREAKDOWN_DAYS = 7
# Bar width for the densified day table, in block glyphs. Monospace
# ``<pre>`` + a proportional bar is what turns a column of numbers into a
# shape you can read at a glance.
_BAR_WIDTH = 8


def _display_name(user: TgUser) -> str:
    """Best-effort human label for the stats target (never empty)."""
    name = (user.first_name or user.username or "").strip()
    return name[:_NAME_MAX] if name else f"ID {user.id}"


def _densify(rows: list[DailyCount], *, today: date_cls, days: int) -> list[DailyCount]:
    """Fill the window's missing days with explicit zeros, newest first.

    :meth:`MessageStatsRepo.last_n_days` omits days with no rows by
    design ("the caller can densify if needed"). For a *table* the gaps
    are actively misleading — a user with three active days sees three
    rows and can't tell a quiet day from missing data. Filling them makes
    the window honest and gives the bars a common baseline.
    """
    seen = {row.date: row.count for row in rows}
    return [
        DailyCount(date=iso, count=seen.get(iso, 0))
        for iso in ((today - timedelta(days=offset)).isoformat() for offset in range(days))
    ]


def _breakdown_block(rows: list[DailyCount], lang: str) -> str:
    """The ``<pre>`` day table + best-day highlight appended to the caption.

    Dates are shortened to ``MM-DD`` exactly like legacy
    (``str(row[0])[5:]``). Values are ints and dates are generated here,
    so nothing is user-controlled — but the block is escaped anyway
    rather than re-deriving that fact at every future call site.
    """
    title = t("h_stats_breakdown_title", lang)
    peak = max((row.count for row in rows), default=0)
    if peak <= 0:
        return f"\n\n<b>{title}</b>\n<i>{t('h_stats_no_data', lang)}</i>"
    lines = []
    for row in rows:
        bar = "▇" * max(1, round(row.count / peak * _BAR_WIDTH)) if row.count else ""
        lines.append(f"{row.date[5:]} {row.count:>5} {bar}".rstrip())
    body = html.escape("\n".join(lines))
    best = max(rows, key=lambda row: row.count)
    highlight = t("h_stats_best_day", lang, date=best.date[5:], count=best.count)
    return f"\n\n<b>{title}</b>\n<pre>{body}</pre>{highlight}"


def _summary_lines(lang: str, counts: dict[int, int], total: int) -> str:
    """The ``• label: <code>N</code>`` bullets shared by the PNG caption
    and the text fallback (they must always carry identical numbers)."""
    lines = [f"• {t(key, lang)}: <code>{counts[days]}</code>" for days, key in _PERIODS]
    lines.append(f"• {t('h_profile_msg_total', lang)}: <code>{total}</code>")
    return "\n".join(lines)


def _user_caption(
    name: str,
    lang: str,
    *,
    counts: dict[int, int],
    total: int,
    breakdown: list[DailyCount],
) -> str:
    """Full HTML caption for the per-user card (PNG *and* text fallback).

    ``name`` is user-controlled (Telegram ``first_name``/``username``) and
    the bot sends with ``parse_mode=HTML``, so it MUST be escaped —
    otherwise a name containing markup renders as live HTML (formatting /
    phishing injection, same seam guarded in ``/start``).
    """
    header = t("h_stats_user_header", lang, name=html.escape(name))
    return f"{header}\n\n{_summary_lines(lang, counts, total)}{_breakdown_block(breakdown, lang)}"


def _format(rows: list[DailyCount], *, days: int, lang: str) -> list[str]:
    """Chat-wide ``/group_stats`` body.

    Legacy emits Markdown-V1 bold (``**text**``); we re-render the
    bullets in HTML to match the bot-wide
    ``parse_mode=ParseMode.HTML`` set in ``app.py`` — no per-message
    parser switch.

    One line per day, and the window is operator config:
    ``STATS_PERIOD_DAYS`` is declared ``ge=1, le=365``. A day line is
    ~20 characters, so anything past ~200 days is a message Telegram
    refuses — ``answer`` comes back 400 and ``/group_stats`` silently
    answers nothing. Rather than narrow a range the operator was told
    they could set, the card pages: the validator's whole span is now
    renderable.
    """
    if not rows:
        return [t("h_group_stats_empty", lang, days=days)]
    total = sum(row.count for row in rows)
    # Trailing newline stays in code, not in the copy: ``paginate_lines``
    # joins header and body with a single "\n", and the blank line
    # between them is layout, not something a translator should have to
    # preserve.
    header = t("h_group_stats_header", lang, days=days, total=total) + "\n"
    lines = [t("h_group_stats_day_row", lang, date=row.date, count=row.count) for row in rows]
    return paginate_lines(
        header,
        lines,
        more_line=lambda left: t("h_group_stats_more", lang, left=left),
    )


def build_router(registry: EngineRegistry, stats_config: StatsConfig) -> Router:
    """Factory — fresh ``Router`` + middleware per call so tests can re-wire.

    ``stats_config`` is captured by the inner handlers' closures so the
    router signature stays compatible with the rest of ``main_router``
    (which threads only ``registry`` + ``Settings`` sub-configs, never
    free positional kwargs).
    """
    tz = ZoneInfo(stats_config.timezone)

    async def handle_user_stats(
        message: Message,
        lang: str,
        message_stats_repo: MessageStatsRepo,
    ) -> None:
        """Per-user activity card, reply-target aware (RR-1 #5)."""
        chat = message.chat
        sender = message.from_user
        if chat is None or sender is None:  # defensive — aiogram sets both
            return
        if chat.type == ChatType.PRIVATE:
            # ``MessageActivityMiddleware`` only records group traffic, so
            # a DM card would be four guaranteed zeros — which reads like
            # a bug. Say where the numbers live instead.
            await message.answer(t("h_stats_group_only", lang))
            return
        reply = message.reply_to_message
        target = reply.from_user if reply is not None and reply.from_user else sender
        if target.is_bot:
            # Legacy refused bot targets outright: their counters are
            # meaningless (the bot's own messages aren't tracked) and a
            # zeroed card reads like a bug. An admin posting anonymously
            # arrives as the shared ``GroupAnonymousBot`` and would be
            # self-targeting, so they'd read "you are a bot" — same
            # refusal, but phrased for the human behind the mask.
            key = "h_stats_anonymous" if target.id == sender.id else "h_stats_bot_excluded"
            await message.answer(t(key, lang))
            return

        now = datetime.now(tz)
        today = now.date()
        breakdown = _densify(
            await message_stats_repo.last_n_days(
                target.id, chat.id, days=_BREAKDOWN_DAYS, today=today
            ),
            today=today,
            days=_BREAKDOWN_DAYS,
        )
        # Today + week are derived from the densified week rather than
        # re-queried: the numbers in the bullets and the numbers in the
        # table below them then physically cannot disagree, and it saves
        # two round-trips on the shared session. 30d still needs its own
        # query — it is wider than the breakdown window.
        counts = {
            1: next((row.count for row in breakdown if row.date == today.isoformat()), 0),
            7: sum(row.count for row in breakdown),
            30: await message_stats_repo.count_for_days(target.id, chat.id, days=30, today=today),
        }
        total = await message_stats_repo.total_for_user(target.id, chat.id)

        name = _display_name(target)
        caption = _user_caption(name, lang, counts=counts, total=total, breakdown=breakdown)
        card_rows: list[tuple[str, int]] = [(t(key, lang), counts[days]) for days, key in _PERIODS]
        card_rows.append((t("h_profile_msg_total", lang), total))

        mode = "png"
        try:
            # Pure-CPU rasterisation — off the event loop.
            png = await asyncio.to_thread(
                render_stats_card,
                t("h_stats_card_title", lang, name=name),
                card_rows,
                subtitle=t("h_stats_card_subtitle", lang),
                generated_at=now,
            )
        except Exception as exc:  # noqa: BLE001 - the card is decorative
            # Broad on purpose: rendering is best-effort garnish on top of
            # a caption that already carries every number. Pillow raises a
            # scattered family here (OSError on a bad save target, ValueError
            # on an unencodable glyph, ``struct.error`` on a truncated TTF
            # mid font-package upgrade) and there is no version of "the
            # picture failed" that should cost the user their stats.
            mode = "text"
            log.bind(target_id=target.id, chat_id=chat.id).warning(
                "stats card render failed: {e!r}", e=exc
            )
            await message.answer(caption)
        else:
            try:
                await message.answer_photo(
                    BufferedInputFile(png, filename="user_stats.png"),
                    caption=caption,
                )
            except TelegramBadRequest as exc:
                # 400 only — "not enough rights to send photos", bad photo
                # dimensions: photo-specific refusals where plain text is
                # still allowed, so the text retry genuinely rescues the
                # user's numbers (legacy did the same at bot.py:41190).
                # Deliberately NOT catching the rest of TelegramAPIError:
                # a network timeout can land *after* Telegram accepted and
                # delivered the photo, and 403/flood would reject the text
                # too — retrying there double-posts or just fails twice.
                mode = "text"
                log.bind(target_id=target.id, chat_id=chat.id).warning(
                    "stats card refused: {e!r}", e=exc
                )
                await message.answer(caption)
        log.bind(
            uid=sender.id,
            target_id=target.id,
            chat_id=chat.id,
            total=total,
            mode=mode,
        ).info("/stats rendered")

    async def handle_chat_stats(
        message: Message, message_stats_repo: MessageStatsRepo, lang: str
    ) -> None:
        """Chat-wide per-day totals (``/group_stats``, Stage 11 surface)."""
        if message.chat is None:  # defensive — aiogram always sets it
            return
        if message.chat.type == ChatType.PRIVATE:
            # #1253: the same reason ``handle_user_stats`` refuses above.
            # ``MessageActivityMiddleware`` records group traffic only, so
            # ``chat_totals_by_date`` on a DM is guaranteed empty and the
            # card would read "0 messages in the last N days" — which
            # looks like a broken counter rather than like the wrong place
            # to ask.
            #
            # The gate is inline rather than
            # :func:`chat_scope.with_chat_type_refusal` on purpose: that
            # wrapper registers one refusal for EVERY command word in the
            # router it wraps, so applying it here would also displace the
            # ``/stats`` wording above with a generic one.
            await message.answer(t("h_group_stats_group_only", lang))
            return
        today = datetime.now(tz).date()
        rows = await message_stats_repo.chat_totals_by_date(
            message.chat.id,
            days=stats_config.period_days,
            today=today,
        )
        for page in _format(rows, days=stats_config.period_days, lang=lang):
            await message.answer(page)
        log.bind(
            chat_id=message.chat.id,
            days=stats_config.period_days,
            rows=len(rows),
            lang=lang,
        ).info("/group_stats rendered")

    router = Router(name="stats")
    router.message.middleware(MessageStatsMiddleware(registry))
    router.message.register(
        handle_user_stats,
        Command("stats", "статистика", ignore_case=True, magic=F.args.is_(None)),
    )
    router.message.register(
        handle_chat_stats,
        Command("group_stats", ignore_case=True, magic=F.args.is_(None)),
    )
    return router
