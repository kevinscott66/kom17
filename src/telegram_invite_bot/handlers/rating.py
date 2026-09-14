"""Donations rating leaderboard — ``/rating`` / ``/top_groups`` (A-04).

Legacy ``cmd_rating`` / ``cmd_top_groups`` (bot.py:24950 / 25181) plus
the private-DM branch of ``cmd_groupstats`` (bot.py:25019) all routed
into ``send_rating_page`` (bot.py:25131): a paginated leaderboard of
groups ranked by donation XP ("Гром"/"Thunder"), where each row drills
into that group's stats card. Deleting the legacy bridge dropped every
entry point — ``/rating`` and friends became silent dead-ends, and
``/groupstats`` in a private chat (which the new groupstats router
rejects at the router level) did too.

This restores the whole surface against ``economy.db``:

* **Commands** — ``/rating`` (private only; group calls get the
  ``rating_private_only`` refusal, matching legacy), ``/top_groups`` /
  ``/rating_groups`` (any chat), and the private-DM
  spelling of the ``/groupstats`` aliases (:data:`GROUPSTATS_COMMANDS`).
  ``/top`` is deliberately NOT aliased here — it's already owned by the
  messages/balance/wins ``/top`` router; legacy overloaded the word but
  the new pipeline keeps the two ``/top`` meanings on separate commands.
* **Pagination** — :class:`RatingNav` edits the message in place to the
  prev/next page (legacy sent a fresh message + deleted the old one;
  editing is cleaner and avoids the delete round-trip).
* **Drill-down** — :class:`RatingGroupStats` edits to a single group's
  stats card (reusing the ``/groupstats`` render) with a "back to
  rating" button. The legacy card's donate/chart/inventory buttons are
  intentionally omitted: those flows aren't ported, so rendering them
  would dead-link.

Parity & deltas:

* Ranking is ``ORDER BY COALESCE(group_xp, 0) DESC, group_id ASC`` over
  groups with ``group_xp > 0`` **and** ``in_rating != 0``. The tiebreak is
  new and not cosmetic: without it SQLite is free to order equal-XP rows
  differently per query, which on a paginated board can show one group
  twice and another never. It matches ``recalc_positions``, so the stored
  ``rating_position`` and the rendered order agree.

  The ``in_rating`` half is new too — legacy had no opt-out, but
  ``0009_donations_rating_writeside`` added one for ``/rating_exclude``
  and this SELECT is the read side that makes the toggle mean anything.
  Until RR-1 #9 it was missing, so an excluded group kept its place on
  the board and only lost its stored ``rating_position``. The same filter
  guards the drill-down card, which is reachable from any old leaderboard
  message long after the group opted out. The displayed rank number is
  computed from ``offset + index`` rather than the stored
  ``rating_position`` column (which the donate writer — not yet ported —
  maintains and which would otherwise show stale ``0``s).
* ``group_link`` is rendered as an HTML anchor when present, and RR-1 #9
  restores legacy's on-the-fly backfill for the rows that have none — see
  :func:`_backfill_identity`. Without it the top of prod's leaderboard is
  a bare chat id with no way to join the group it is advertising. Unlike
  legacy the backfill never revokes a group's primary link and never
  hands out unconditional entry to a private group: it prefers the link
  the admins already published, and anything it has to mint itself is a
  join-*request* link.
* HTML throughout (bot-wide ``parse_mode=HTML``); legacy used Markdown.
  Group names + links are ``html.escape``d — both are operator-set free
  text.
"""

from __future__ import annotations

import asyncio
import html as _html
from dataclasses import dataclass, replace
from math import ceil
from time import monotonic
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
from sqlalchemy import func, select

from telegram_invite_bot.db.models.economy import GroupDonationsAggregate
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers.group_only import handle_group_only
from telegram_invite_bot.handlers.groupstats import (
    GROUPSTATS_COMMANDS,
    _fetch_aggregate,
    _fetch_names,
    _fetch_top,
)
from telegram_invite_bot.handlers.groupstats import _render as _render_group_card
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu
from telegram_invite_bot.keyboards.builders.rating import RatingGroupStats, RatingNav
from telegram_invite_bot.repositories.donations_rating_repo import DonationsRatingRepo
from telegram_invite_bot.utils.aiogram import edit_card, require_from_user
from telegram_invite_bot.utils.numbers import page_offset
from telegram_invite_bot.utils.time import rating_history_date

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, InlineKeyboardMarkup
    from sqlalchemy import ColumnElement

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.services.user_service import UserService


# Telegram chat-member statuses that count as group admin for the
# rating write toggles. Mirrors the moderation module's set.
_ADMIN_STATUSES = {"administrator", "creator"}


log = logger.bind(component="handlers.rating")

# Mirrors legacy ``RATING_PAGE_SIZE`` (bot.py:10571).
_PAGE_SIZE = 10
_NAME_TRUNC = 40
_BTN_NAME_TRUNC = 35

# RR-1 #9 invite-link backfill. The cap of 2 per render is legacy's
# number (bot.py:25140) applied to linkless rows rather than to the first
# two rows; the cooldown and the lock are new — legacy re-tried a group
# that had just refused on every single page view, from every render at
# once.
_BACKFILL_MAX_PER_RENDER = 2
_BACKFILL_RETRY_AFTER = 6 * 60 * 60.0
_BACKFILL_LOCK = asyncio.Lock()
# Named so a group owner auditing their invite links can see where this
# one came from instead of finding an anonymous link they never made.
_INVITE_LINK_NAME = "Kom rating"
# Hosts an invite link may point at. ``telegram.me`` / ``telegram.dog`` are
# Telegram's own historical aliases and still appear in old stored rows.
_INVITE_HOSTS = frozenset({"t.me", "telegram.me", "telegram.dog"})
# group_id -> monotonic deadline before which we won't ask Telegram again.
_BACKFILL_BLOCKED: dict[int, float] = {}


@dataclass(frozen=True, slots=True)
class _RatingRow:
    group_id: int
    group_name: str | None
    group_link: str | None
    xp: int


_XP = func.coalesce(GroupDonationsAggregate.group_xp, 0)


def _ranked() -> ColumnElement[bool]:
    """ "Does this group belong on the public board?" — one definition.

    Shared by the count, the page and the drill-down so the three can't
    drift: a group the count includes but the page filters out shows an
    arrow to an empty page, and one the page hides but the drill-down
    serves makes ``/rating_exclude`` half a no-op.

    ``in_rating IS NULL`` counts as included. SQLite evaluates
    ``NULL != 0`` to NULL (not true), so the explicit branch is required,
    and it is not theoretical: the legacy monolith inserted
    ``groups_donations`` rows with raw SQL that never named the column,
    and a database carried over from before the cutover still holds them.
    """
    return (_XP > 0) & (
        (GroupDonationsAggregate.in_rating.is_(None)) | (GroupDonationsAggregate.in_rating != 0)
    )


async def _is_ranked(registry: EngineRegistry, group_id: int) -> bool:
    """Is ``group_id`` currently on the public board? (drill-down guard)."""
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        found = (
            await conn.execute(
                select(GroupDonationsAggregate.group_id)
                .where(GroupDonationsAggregate.group_id == group_id)
                .where(_ranked())
            )
        ).first()
    return found is not None


async def _fetch_page(registry: EngineRegistry, page: int) -> tuple[list[_RatingRow], int]:
    """Page of ranked groups ordered by XP DESC, + the total row count.

    Mirrors legacy ``get_rating_groups_page``: the count and the page
    both filter on ``COALESCE(group_xp, 0) > 0`` so groups that exist in
    ``groups_donations`` but never earned XP (vanishingly rare) don't
    pad the leaderboard or inflate the page count used for the next-page
    arrow. Both use :func:`_ranked`, so they cannot disagree.
    """
    xp = _XP
    ranked = _ranked()
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        total = int(
            (
                await conn.execute(
                    select(func.count()).select_from(GroupDonationsAggregate).where(ranked)
                )
            ).scalar_one()
        )
        # #1984: not ``(page - 1) * _PAGE_SIZE``. ``RatingNav.page`` is
        # bounded by what SQLite can BIND, not by what the board has, so
        # the ceiling value reached this line and the product left the
        # 64-bit range before the bind did.
        offset = page_offset(page - 1, _PAGE_SIZE)
        rows = (
            await conn.execute(
                select(
                    GroupDonationsAggregate.group_id,
                    GroupDonationsAggregate.group_name,
                    GroupDonationsAggregate.group_link,
                    xp,
                )
                .where(ranked)
                .order_by(xp.desc(), GroupDonationsAggregate.group_id.asc())
                .limit(_PAGE_SIZE)
                .offset(offset)
            )
        ).all()
    return (
        [
            _RatingRow(
                group_id=int(gid),
                group_name=name,
                group_link=link,
                xp=int(xp_val or 0),
            )
            for gid, name, link, xp_val in rows
        ],
        total,
    )


def _invite_link(raw: str | None) -> str | None:
    """``raw`` if it is a Telegram invite URL we're willing to publish.

    Every row on this board becomes a clickable anchor shown to whoever
    ran ``/rating``, and the href comes out of a column the legacy
    monolith also writes. Restricting it to ``t.me`` (and its two
    historical aliases) over http/https means a bad value in that column
    degrades to an unlinked name instead of turning the leaderboard into
    a redirect to wherever the value points. A rejected value is also
    treated as *missing* by :func:`_backfill_identity`, so the junk gets
    replaced with a real link rather than sitting there forever.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"}:
        return None
    # ``hostname`` (not ``netloc``) is what makes the check hold up:
    # it lowercases, and it resolves the userinfo trick — the host of
    # ``https://t.me@evil.example/x`` is ``evil.example``, so that value
    # is rejected here rather than published. Userinfo is then refused
    # outright: a real invite link never carries credentials, and there
    # is no reason to be the thing that forwards a password.
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.hostname not in _INVITE_HOSTS:
        return None
    return candidate


def _name_display(row: _RatingRow, *, trunc: int) -> str:
    """Escaped group name, rendered as an anchor when a link is present."""
    name = _html.escape((row.group_name or str(row.group_id))[:trunc])
    link = _invite_link(row.group_link)
    if link:
        return f'<a href="{_html.escape(link, quote=True)}">{name}</a>'
    return name


def _blocked_until(group_id: int, *, now: float) -> bool:
    """Has ``group_id`` been tried recently? Prunes expired entries."""
    for gid, deadline in list(_BACKFILL_BLOCKED.items()):
        if deadline <= now:
            del _BACKFILL_BLOCKED[gid]
    return group_id in _BACKFILL_BLOCKED


async def _fetch_identity(bot: Bot, group_id: int) -> tuple[str | None, str | None]:
    """``(invite_link, title)`` for ``group_id``, either half possibly ``None``.

    Raises :exc:`TelegramRetryAfter` / :exc:`TelegramNetworkError` through
    to the caller; every other failure degrades to a ``None`` half.

    Two things legacy did here are deliberately not repeated:

    * **No ``export_chat_invite_link``** (bot.py:10608). That method
      *revokes* the group's primary invite link and mints a replacement —
      every link the owner had previously pasted anywhere stops working.
      Doing that as a side effect of a stranger opening a leaderboard is
      not a trade the group agreed to.
    * **No open-door minting.** ``/top_groups`` answers any user in any
      chat, so whatever this function produces is a link a stranger can
      cause to be created inside a third-party private group and have
      published. A plain ``create_chat_invite_link`` is permanent and
      unlimited-use, i.e. that stranger would be handing out entry to a
      group whose admins never agreed to it.

    So the order is:

    1. ``get_chat`` — a pure read. It already carries the primary
       ``invite_link`` whenever the bot is an admin who can invite, which
       covers the ordinary case with no writes at all and grants nothing
       new: that link is one the group's own admins created and are
       already circulating. It also carries the ``title`` even when there
       is no link (prod's #1 group sits on the board under a bare chat id
       because nobody ever stored its name).
    2. ``create_chat_invite_link`` only if step 1 had no link, and always
       with ``creates_join_request=True``. The row becomes clickable — the
       regression this whole function exists to fix — but following the
       link only *asks* to join, and the group's own admins still decide.
       Access is never granted by a leaderboard render.

    Both calls are best-effort otherwise: the bot is frequently not an
    admin of a group that merely received a donation, and that is not an
    error worth surfacing to the person reading the board.
    """
    title: str | None = None
    try:
        chat = await bot.get_chat(group_id)
    except (TelegramRetryAfter, TelegramNetworkError):
        raise
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment
        log.bind(group=group_id, exc=repr(exc)).debug("rating backfill: get_chat failed")
    else:
        title = chat.title
        link = (chat.invite_link or "").strip()
        if link:
            return link, title
    try:
        created = await bot.create_chat_invite_link(
            chat_id=group_id, name=_INVITE_LINK_NAME, creates_join_request=True
        )
    except (TelegramRetryAfter, TelegramNetworkError):
        raise
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment
        log.bind(group=group_id, exc=repr(exc)).debug(
            "rating backfill: create_chat_invite_link failed"
        )
        return None, title
    return created.invite_link, title


async def _backfill_identity(
    bot: Bot, registry: EngineRegistry, rows: list[_RatingRow]
) -> list[_RatingRow]:
    """Fill in missing links/names for the first few unlinked rows.

    Restores legacy ``send_rating_page``'s pre-render pass (bot.py:25140),
    which walked the page and asked Telegram for an invite link for any
    row that had none. Without it a group only ever becomes clickable if
    some *other* code path happened to store a link — and on prod nothing
    does, so the highest-ranked group has sat there as an unnamed,
    unjoinable chat id.

    Four guards keep a render path from turning into an API storm — and
    they have to, because ``/top_groups`` is unauthenticated and the
    dispatcher's throttle allows a couple of updates per second per user:

    * **At most** :data:`_BACKFILL_MAX_PER_RENDER` groups per page. Legacy
      used the same number, though it capped the first two *rows* of the
      page; we cap the first two *linkless* rows, so a page whose top is
      already clickable still makes progress further down. A full page is
      ten rows; touching all of them on every tap of the pagination arrows
      would be a flood-wait waiting to happen.
    * Every target is marked *before the first* ``await``, in a separate
      pass. Selection contains no suspension point, so marking the whole
      batch up front is what actually makes it atomic — marking inside the
      fetch loop would leave targets 2..n unclaimed across target 1's two
      round-trips, and two concurrent renders would both work on them.
    * :data:`_BACKFILL_LOCK` serialises the API half across renders, so
      the fan-out is bounded no matter how many people open the board at
      once. It is taken only when there is something to fetch, so the
      overwhelmingly common no-op render never queues behind anyone.
    * The mark lasts :data:`_BACKFILL_RETRY_AFTER` seconds for *refusals*
      — the bot isn't an admin there, the chat is gone — which are
      permanent-ish, and without the cooldown every render would pay two
      doomed round-trips for them. A flood-wait or a network blip is the
      opposite: transient, and specifically the moment when re-asking is
      worst. Those unmark the group and abandon the rest of the batch
      rather than poisoning it for six hours.

    The mark is process-local: a restart retries, and a second bot process
    would keep its own. Both are acceptable for what is an optimisation of
    an idempotent write.

    Returns ``rows`` with the enriched entries substituted in, so the page
    renders the new values immediately instead of re-querying the way
    legacy did.
    """
    now = monotonic()
    targets = [
        row
        for row in rows
        # Groups only. Legacy skipped non-negative ids here too
        # (bot.py:25143), and for a good reason: a non-negative id is a
        # user chat, where ``get_chat`` happily succeeds and the mint that
        # follows is guaranteed to fail.
        if row.group_id < 0
        and _invite_link(row.group_link) is None
        and not _blocked_until(row.group_id, now=now)
    ][:_BACKFILL_MAX_PER_RENDER]
    if not targets:
        return rows
    for row in targets:
        _BACKFILL_BLOCKED[row.group_id] = now + _BACKFILL_RETRY_AFTER

    resolved: dict[int, _RatingRow] = {}
    async with _BACKFILL_LOCK:
        for index, row in enumerate(targets):
            try:
                link, title = await _fetch_identity(bot, row.group_id)
            except (TelegramRetryAfter, TelegramNetworkError) as exc:
                # Transient. Drop the cooldown for everything still
                # unattempted so the next render picks it up, and stop
                # pushing on an API that just asked us to back off.
                for pending in targets[index:]:
                    _BACKFILL_BLOCKED.pop(pending.group_id, None)
                log.bind(group=row.group_id, exc=repr(exc)).warning("rating backfill: backing off")
                break
            if link is None and not title:
                continue
            resolved[row.group_id] = replace(
                row,
                group_link=link or row.group_link,
                group_name=title or row.group_name,
            )
    if not resolved:
        return rows

    try:
        async with session_for(registry, DBName.ECONOMY) as session:
            repo = DonationsRatingRepo(session)
            for gid, row in resolved.items():
                await repo.save_group_identity(gid, link=row.group_link, title=row.group_name)
    except Exception as exc:  # noqa: BLE001 — a failed write must not eat the page
        # The page still renders the values we just learned, but nothing
        # stored them — so let the next render try again instead of
        # sitting on a cooldown for work that didn't land.
        for gid in resolved:
            _BACKFILL_BLOCKED.pop(gid, None)
        log.bind(exc=repr(exc)).warning("rating backfill: persist failed")
    else:
        log.bind(groups=sorted(resolved)).info("rating backfill: identities refreshed")
    return [resolved.get(row.group_id, row) for row in rows]


def _render_page(
    lang: str, *, page: int, rows: list[_RatingRow], total: int, private: bool
) -> tuple[str, InlineKeyboardMarkup]:
    """Leaderboard text + keyboard for ``page``.

    The back-to-menu button is only attached in a private chat, where
    the ``MainMenu`` callback router (private-only) actually handles it;
    rendering it in a group would dead-link.
    """
    xp_unit = t("group_xp_unit", lang)
    title = t("rating_page_title", lang)
    short = t("rating_page_short", lang)
    pages = max(1, ceil(total / _PAGE_SIZE))
    # A board that fits on one page has no page to be on: the counter
    # and the nav row below are both noise there (and the nav row was a
    # lone self-referential button that did nothing when tapped).
    header = f"🏆 <b>{title}</b>"
    if pages > 1:
        header += f" ({short} {page}/{pages})"
    header += "\n\n"
    offset = (page - 1) * _PAGE_SIZE
    lines: list[str] = []
    builder = InlineKeyboardBuilder()
    for idx, row in enumerate(rows, start=1):
        rank = offset + idx
        lines.append(f"{rank}. {_name_display(row, trunc=_NAME_TRUNC)} — {row.xp} {xp_unit}")
        btn_name = (row.group_name or str(row.group_id))[:_BTN_NAME_TRUNC]
        builder.row(
            _button(
                f"{rank}. {btn_name} — {row.xp} {xp_unit}",
                RatingGroupStats(group_id=row.group_id).pack(),
            )
        )

    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 1:
            nav.append(_button("⬅️", RatingNav(page=page - 1).pack()))
        nav.append(_button(f"{page}/{pages}", RatingNav(page=page).pack()))
        if page < pages:
            nav.append(_button("➡️", RatingNav(page=page + 1).pack()))
        builder.row(*nav)
    if private:
        builder.row(_button(t("back_to_menu", lang), MainMenu(action="home").pack()))
    return header + "\n".join(lines), builder.as_markup()


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _render_drilldown(
    registry: EngineRegistry, lang: str, group_id: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    """One group's stats card (reusing the ``/groupstats`` renderer) plus
    a "back to rating" button. ``None`` when the group has no aggregate
    row, or has opted out — the caller turns that into an alert.

    The opt-out check is not redundant with :func:`_fetch_page`. A
    leaderboard message keeps working after it is sent, so a card for a
    group that ran ``/rating_exclude`` an hour ago is still one tap away
    in anyone's history; without this, "hide us from the rating" would
    hide the row and keep serving the numbers. ``/groupstats`` run inside
    the group itself is unaffected — the toggle is about the public board,
    not about the group's own members.
    """
    if not await _is_ranked(registry, group_id):
        return None
    aggregate = await _fetch_aggregate(registry, group_id)
    if aggregate is None:
        return None
    group_name, _treasury_total, group_xp, rating_position = aggregate
    top = await _fetch_top(registry, group_id)
    names = await _fetch_names(registry, [uid for uid, _ in top])
    text = _render_group_card(
        lang,
        group_name=group_name,
        group_xp=group_xp,
        rating_position=rating_position,
        top=top,
        names=names,
    )
    builder = InlineKeyboardBuilder()
    builder.row(_button(t("btn_back_to_rating", lang), RatingNav(page=1).pack()))
    return text, builder.as_markup()


async def handle_rating_command(
    message: Message,
    bot: Bot,
    user_service: UserService,
    registry: EngineRegistry,
    *,
    private: bool,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Render page 1 of the leaderboard (or the empty-state refusal)."""
    user = await user_service.touch(require_from_user(message))
    # #220: ``_backfill_identity`` below asks Telegram for one chat title
    # per row with no name cached — up to a page of sequential
    # ``getChat`` calls. Holding ``users.db``'s writer slot through that
    # is what turns a slow Telegram into ``database is locked`` for every
    # other update in the process. See :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    rows, total = await _fetch_page(registry, 1)
    if not rows:
        await message.answer(t("no_groups_with_donates", user.language))
        return
    rows = await _backfill_identity(bot, registry, rows)
    text, markup = _render_page(user.language, page=1, rows=rows, total=total, private=private)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)
    log.bind(uid=user.user_id, total=total, private=private).info("/rating rendered")


async def handle_rating_nav(
    callback: CallbackQuery,
    callback_data: RatingNav,
    bot: Bot,
    user_service: UserService,
    registry: EngineRegistry,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Edit the leaderboard message to ``callback_data.page``."""
    user = await user_service.touch(callback.from_user)
    # #220: same fan-out as the command twin, one page further in.
    if checkpoint is not None:
        await checkpoint()
    page = max(1, callback_data.page)
    message = callback.message
    private = isinstance(message, Message) and message.chat.type == ChatType.PRIVATE
    rows, total = await _fetch_page(registry, page)
    if not rows and page > 1:
        # Past the last page (e.g. a group dropped out between taps) —
        # snap back to page 1 rather than show an empty card.
        page = 1
        rows, total = await _fetch_page(registry, 1)
    if not rows:
        await callback.answer(t("no_groups_with_donates", user.language), show_alert=True)
        return
    rows = await _backfill_identity(bot, registry, rows)
    text, markup = _render_page(user.language, page=page, rows=rows, total=total, private=private)
    if isinstance(message, Message):
        # Via ``edit_card``, not a raw edit: the page number is in the
        # callback data, so a double-tap on the current page re-renders
        # byte-identical content and Telegram rejects it with "message
        # is not modified". Prod logged exactly that twice on 12.08 —
        # and because it reached the global error router, the user was
        # told "⚠️ Произошла ошибка" for tapping a button that had
        # nothing to change. The same swallow also covers a card the
        # user kept open past the edit window.
        await edit_card(message, text, reply_markup=markup, disable_web_page_preview=True)
    await callback.answer()


async def handle_rating_group_stats(
    callback: CallbackQuery,
    callback_data: RatingGroupStats,
    user_service: UserService,
    registry: EngineRegistry,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Edit the leaderboard message to one group's stats card."""
    user = await user_service.touch(callback.from_user)
    # #220: ``touch`` above took ``users.db``'s writer slot, and what
    # follows it here is four ``economy.db`` reads inside
    # ``_render_drilldown`` plus an ``editMessageText`` round-trip.
    # Every sibling in this module hands the slot back first; this one
    # was the hole. See :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    rendered = await _render_drilldown(registry, user.language, callback_data.group_id)
    if rendered is None:
        await callback.answer(t("rating_group_not_found", user.language), show_alert=True)
        return
    text, markup = rendered
    message = callback.message
    if isinstance(message, Message):
        # Same reasoning as the nav handler above: re-tapping the group
        # already on screen is a no-op edit, not an error.
        await edit_card(message, text, reply_markup=markup, disable_web_page_preview=True)
    await callback.answer()


# ── L-38 write-side: admin rating-membership toggles ──────────────────────


async def _caller_is_rating_admin(
    bot: Bot, settings: Settings, *, chat_id: int, user_id: int
) -> bool | None:
    """Authorise a rating write toggle. ``None`` on Telegram-API error.

    A developer (global) is always allowed. Otherwise the caller must be a
    confirmed administrator/creator of the group whose rating slot they're
    changing. Fail-closed: an API error returns ``None`` so the caller
    refuses with a "try again" rather than silently allowing the write.
    Reuses the same auth shape as moderation (``get_chat_member`` status).
    """
    if settings.bot.is_developer(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:  # noqa: BLE001 — re-surface via None contract
        log.bind(chat=chat_id, user=user_id, exc=repr(exc)).warning(
            "rating-admin get_chat_member failed",
        )
        return None
    return member.status in _ADMIN_STATUSES


async def _apply_rating_toggle(
    message: Message,
    bot: Bot,
    settings: Settings,
    registry: EngineRegistry,
    user_service: UserService,
    *,
    included: bool,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/rating_include`` / ``/rating_exclude`` for the current group.

    Flips ``groups_donations.in_rating`` for the chat the command runs in,
    then recomputes positions and snapshots today's history — the same
    write-trio legacy ran after every donation (bot.py:10707) so the
    leaderboard and the per-group ``rating_position`` stay consistent the
    instant a group is hidden or restored.
    """
    user = await user_service.touch(require_from_user(message))
    # #234: ``_caller_is_rating_admin`` below is a ``getChatMember``
    # round-trip, and every exit from here on replies over the network.
    # ``touch`` has already opened this update's ``users.db``
    # transaction and ``BEGIN IMMEDIATE`` means one writer per DB until
    # the middleware commits — release it before we start waiting on
    # Telegram. Same shape as ``handle_rating_command`` above (#220).
    if checkpoint is not None:
        await checkpoint()
    lang = user.language
    chat_id = message.chat.id
    allowed = await _caller_is_rating_admin(bot, settings, chat_id=chat_id, user_id=user.user_id)
    if allowed is None:
        await message.reply(t("h_rating_admin_retry_later", lang))
        return
    if not allowed:
        await message.reply(t("h_rating_admin_no_permission", lang))
        return

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        repo = DonationsRatingRepo(session)
        exists = await repo.set_in_rating(chat_id, included=included)
        if not exists:
            # No aggregate row → the group never received a donation, so
            # there is nothing to include/exclude. Roll back the (no-op)
            # session implicitly by not committing.
            await session.rollback()
            await message.reply(t("h_rating_toggle_no_donations", lang))
            return
        await repo.recalc_positions()
        await repo.save_history_snapshot(chat_id, today=rating_history_date())
        await session.commit()

    key = "h_rating_included_ok" if included else "h_rating_excluded_ok"
    await message.reply(t(key, lang))
    log.bind(chat_id=chat_id, uid=user.user_id, included=included).info(
        "rating membership toggled",
    )


async def handle_rating_recalc(
    message: Message,
    bot: Bot,
    settings: Settings,
    registry: EngineRegistry,
    user_service: UserService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """``/rating_recalc`` — recompute every group's position + snapshot.

    A maintenance command (developer-only, any chat) that re-runs the dense
    ranking over all included groups and writes a fresh history snapshot for
    the current group. Useful after a bulk data fix or to repair drift if a
    donation write ever skipped the recalc.
    """
    user = await user_service.touch(require_from_user(message))
    lang = user.language
    if not settings.bot.is_developer(user.user_id):
        # Recalc is global (touches every group's slot), so it's gated to
        # developers only — a single group admin shouldn't reorder the whole
        # leaderboard. Silent-drop matches the other dev-only commands.
        log.bind(uid=user.user_id).debug("non-dev /rating_recalc; dropped")
        return
    # #234: the recalc below walks every included group (one UPDATE per
    # group) and then replies — all of it while ``touch`` still holds
    # this update's ``users.db`` writer slot. The checkpoint sits after
    # the developer gate on purpose: a non-dev caller is dropped without
    # ever reaching the slow part, so there is nothing to release.
    if checkpoint is not None:
        await checkpoint()
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        repo = DonationsRatingRepo(session)
        ranked = await repo.recalc_positions()
        await repo.save_history_snapshot(message.chat.id, today=rating_history_date())
        await session.commit()
    await message.reply(t("h_rating_recalc_ok", lang, count=ranked))
    log.bind(uid=user.user_id, ranked=ranked).info("/rating_recalc done")


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Rating leaderboard commands + pagination/drill-down callbacks.

    Reads ``economy.db`` directly via ``registry`` (no economy repo on
    this path) and ``user_service`` for the caller's language — both
    injected by the dispatcher-wide ``SessionMiddleware``, so this
    router mounts no scoped middleware of its own.
    """
    router = Router(name="rating")

    _private = F.chat.type == ChatType.PRIVATE
    _group = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})

    async def _entry_private(
        message: Message,
        bot: Bot,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_rating_command(
            message, bot, user_service, registry, private=True, checkpoint=checkpoint
        )

    async def _entry_any(
        message: Message,
        bot: Bot,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        is_private = message.chat.type == ChatType.PRIVATE
        await handle_rating_command(
            message,
            bot,
            user_service,
            registry,
            private=is_private,
            checkpoint=checkpoint,
        )

    async def _refuse_group(message: Message, user_service: UserService) -> None:
        user = await user_service.touch(require_from_user(message))
        await message.reply(t("rating_private_only", user.language))

    # ``/rating`` — private only (page); group → localised refusal.
    router.message.register(
        _entry_private,
        Command("rating", "рейтинг", ignore_case=True, magic=F.args.is_(None)),
        _private,
        F.from_user,
    )
    router.message.register(
        _refuse_group,
        Command("rating", "рейтинг", ignore_case=True, magic=F.args.is_(None)),
        _group,
        F.from_user,
    )
    # ``/top_groups`` / ``/rating_groups`` — any chat. ``/top`` and
    # ``/kom_top`` are intentionally NOT aliased here — the new pipeline
    # gives those to the messages/balance ladder router, diverging from
    # legacy where they pointed at the groups leaderboard.
    router.message.register(
        _entry_any,
        Command(
            "top_groups",
            "rating_groups",
            ignore_case=True,
            magic=F.args.is_(None),
        ),
        F.from_user,
    )
    # Private-DM spelling of the ``/groupstats`` aliases → leaderboard
    # (the groupstats router owns the group branch).
    router.message.register(
        _entry_private,
        Command(*GROUPSTATS_COMMANDS, ignore_case=True, magic=F.args.is_(None)),
        _private,
        F.from_user,
    )

    # L-38 write-side admin toggles. ``/rating_include`` / ``/rating_exclude``
    # are group-only (they act on the current chat's rating slot) and gated
    # to that group's admins or a developer; ``/rating_recalc`` is a
    # developer-only maintenance command (any chat).
    async def _exclude(
        message: Message,
        bot: Bot,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await _apply_rating_toggle(
            message,
            bot,
            settings,
            registry,
            user_service,
            included=False,
            checkpoint=checkpoint,
        )

    async def _include(
        message: Message,
        bot: Bot,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await _apply_rating_toggle(
            message,
            bot,
            settings,
            registry,
            user_service,
            included=True,
            checkpoint=checkpoint,
        )

    async def _recalc(
        message: Message,
        bot: Bot,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_rating_recalc(
            message, bot, settings, registry, user_service, checkpoint=checkpoint
        )

    router.message.register(
        _exclude,
        Command(
            "rating_exclude",
            "rating_out",
            "рейтинг_исключить",
            ignore_case=True,
        ),
        _group,
        F.from_user,
    )
    router.message.register(
        _include,
        Command(
            "rating_include",
            "rating_in",
            "рейтинг_включить",
            ignore_case=True,
        ),
        _group,
        F.from_user,
    )
    # Their private twin (#123). Both toggles gate per-registration
    # rather than router-wide, so the #122 shape applies directly here —
    # no ``with_chat_type_refusal`` wrapper is needed, and none is used:
    # wrapping this router would also refuse ``/rating`` (already
    # two-sided, see ``_refuse_group``) and the ungated ``/top_groups``.
    router.message.register(
        handle_group_only,
        Command(
            "rating_exclude",
            "rating_out",
            "рейтинг_исключить",
            "rating_include",
            "rating_in",
            "рейтинг_включить",
            ignore_case=True,
        ),
        _private,
        F.from_user,
    )
    router.message.register(
        _recalc,
        Command("rating_recalc", "рейтинг_пересчет", ignore_case=True),
        F.from_user,
    )

    async def _nav(
        callback: CallbackQuery,
        callback_data: RatingNav,
        bot: Bot,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_rating_nav(callback, callback_data, bot, user_service, registry, checkpoint)

    async def _drilldown(
        callback: CallbackQuery,
        callback_data: RatingGroupStats,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_rating_group_stats(callback, callback_data, user_service, registry, checkpoint)

    router.callback_query.register(_nav, RatingNav.filter())
    router.callback_query.register(_drilldown, RatingGroupStats.filter())
    return router
