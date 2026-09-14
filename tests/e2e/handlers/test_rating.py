"""E2E: donations rating leaderboard — ``/rating`` / ``/top_groups`` (A-04).

Covers the entry points the deleted legacy bridge dead-ended: the
``/rating`` command (private page + group refusal), the ``/top_groups``
alias (any chat), the private-DM ``/groupstats`` branch (the silent
dead-end the group-only router filter left), the empty-state, plus the
pagination and group drill-down callbacks.

RR-1 #9 adds the on-the-fly identity backfill and the ``in_rating``
read-side filter; those tests live at the bottom of the file.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import GetChat
from aiogram.types import AcceptedGiftTypes, ChatFullInfo, ChatInviteLink
from aiogram.types import User as TgUser
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy import update as sql_update

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import GroupDonationsAggregate, GroupTopDonator
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import rating as rating_module
from telegram_invite_bot.handlers.rating import (
    _BACKFILL_BLOCKED,
    _BACKFILL_RETRY_AFTER,
    _backfill_identity,
    _RatingRow,
)
from telegram_invite_bot.keyboards.builders.rating import RatingGroupStats, RatingNav
from telegram_invite_bot.repositories.donations_rating_repo import (
    DonationsRatingRepo,
)
from telegram_invite_bot.utils.numbers import MAX_DB_INT

from .conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.fixture(autouse=True)
def _clear_backfill_cooldown() -> None:
    """The backfill's "don't ask again" map is module-global by design
    (it outlives a single render), so it also outlives a single test."""
    _BACKFILL_BLOCKED.clear()


async def _seed_groups(registry: EngineRegistry, count: int) -> None:
    """Seed ``count`` groups with descending XP (group -100 has the most)."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for i in range(count):
            session.add(
                GroupDonationsAggregate(
                    group_id=-100 - i,
                    group_name=f"Group {i}",
                    group_link="https://t.me/joinchat/abc" if i == 0 else None,
                    total_donations=1000 - i,
                    group_xp=1000 - i,
                    rating_position=i + 1,
                )
            )
        await session.commit()


async def _seed_drilldown(registry: EngineRegistry) -> None:
    """One group + a top-donator + a user row for the drill-down card."""
    econ = registry.session(DBName.ECONOMY)
    async with econ() as session:
        session.add(
            GroupDonationsAggregate(
                group_id=-500,
                group_name="Donor Hub",
                total_donations=777,
                group_xp=777,
                rating_position=1,
            )
        )
        session.add(
            GroupTopDonator(
                group_id=-500, user_id=42, total_donated=300, last_donate=datetime(2024, 1, 1)
            )
        )
        await session.commit()
    users = registry.session(DBName.USERS)
    async with users() as session:
        session.add(User(user_id=42, first_name="Whale"))
        await session.commit()


async def test_rating_private_renders_leaderboard(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_groups(registry, 3)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "Рейтинг групп" in text
    # Top group rendered as an HTML anchor (it has a link).
    assert 'href="https://t.me/joinchat/abc"' in text
    assert "1. " in text


async def test_rating_in_group_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_groups(registry, 2)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update("/rating", chat_type="supergroup", chat_id=-100, user_id=5),
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "личке" in sent[0]["text"]


async def test_top_groups_in_group_renders(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_groups(registry, 2)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update("/top_groups", chat_type="supergroup", chat_id=-100, user_id=5),
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "Рейтинг групп" in sent[0]["text"]


async def test_groupstats_private_renders_leaderboard(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_groups(registry, 2)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update("/groupstats", chat_type="private", user_id=9)
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "Рейтинг групп" in sent[0]["text"]


async def test_rating_empty_state(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "нет групп" in sent[0]["text"].lower()


async def test_rating_single_page_has_no_nav_row(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Two groups fit on one page, so there is nowhere to navigate.

    The nav row used to render regardless, leaving a lone "1" button
    that re-drew the very page it was on — and the header claimed a
    page number for a board that has only one.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_groups(registry, 2)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )
    assert result is not UNHANDLED
    assert "стр." not in sent[0]["text"]
    labels = [btn.text for row in sent[0]["markup"].inline_keyboard for btn in row]
    # Only the two group rows and the back-to-menu button survive.
    assert not any(label in {"1", "1/1", "⬅️", "➡️"} for label in labels)


async def test_rating_nav_callback_edits_to_page_two(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_groups(registry, 15)  # > one page of 10
    sink = capture_callback_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, make_callback_update(RatingNav(page=2).pack(), user_id=1, language_code="ru")
    )
    assert result is not UNHANDLED
    edits = [e for e in sink if e["kind"] == "edit"]
    answers = [e for e in sink if e["kind"] == "callback_answer"]
    assert len(edits) == 1
    assert "(стр. 2/2)" in edits[0]["text"]
    # Page 2 starts at rank 11.
    assert "11. " in edits[0]["text"]
    assert len(answers) == 1


async def test_rating_nav_at_the_int64_ceiling_snaps_back_instead_of_crashing(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1984: the page number survives ``DbInt``; the OFFSET it becomes
    does not.

    #1978 bounded every callback int field by what SQLite can BIND, and
    said in as many words that a domain clamp is still the call site's
    to make. This is the call site that never made one: ``page`` reaches
    :func:`_fetch_page` at the ceiling, ``(page - 1) * 10`` leaves the
    64-bit range, and the very next bind raises the ``OverflowError``
    #1978 was written to remove — one multiplication further along.

    Nothing here is about authorisation: the board is public. What it
    costs is the snap-back below, which every other out-of-range page
    already gets.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_groups(registry, 15)
    sink = capture_callback_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_callback_update(RatingNav(page=MAX_DB_INT).pack(), user_id=1, language_code="ru"),
    )
    assert result is not UNHANDLED
    edits = [e for e in sink if e["kind"] == "edit"]
    assert len(edits) == 1
    # Snapped back to the top, exactly like any other page past the end.
    assert "(стр. 1/2)" in edits[0]["text"]
    assert "1. " in edits[0]["text"]
    assert len([e for e in sink if e["kind"] == "callback_answer"]) == 1


async def test_rating_group_stats_drilldown(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_drilldown(registry)
    sink = capture_callback_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_callback_update(RatingGroupStats(group_id=-500).pack(), user_id=1, language_code="ru"),
    )
    assert result is not UNHANDLED
    edits = [e for e in sink if e["kind"] == "edit"]
    assert len(edits) == 1
    assert "Donor Hub" in edits[0]["text"]
    assert "Whale" in edits[0]["text"]


async def test_rating_group_stats_not_found_alerts(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sink = capture_callback_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_callback_update(RatingGroupStats(group_id=-999).pack(), user_id=1, language_code="ru"),
    )
    assert result is not UNHANDLED
    answers = [e for e in sink if e["kind"] == "callback_answer"]
    assert len(answers) == 1
    assert answers[0]["text"]  # non-empty alert text
    assert not [e for e in sink if e["kind"] == "edit"]


# ── RR-1 #9: invite-link / name backfill + the in_rating read filter ──────

_NO_GIFTS = AcceptedGiftTypes(
    unlimited_gifts=False,
    limited_gifts=False,
    unique_gifts=False,
    premium_subscription=False,
    gifts_from_channels=False,
)
_BOT_USER = TgUser(id=1, is_bot=True, first_name="Kom")


async def _seed_bare(
    registry: EngineRegistry, *ids: int, in_rating: int = 1, xp: int = 1000
) -> None:
    """Groups with XP but neither a name nor a link — prod's #1 shape."""
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for i, gid in enumerate(ids):
            session.add(
                GroupDonationsAggregate(
                    group_id=gid,
                    group_xp=xp - i,
                    total_donations=xp - i,
                    in_rating=in_rating,
                )
            )
        await session.commit()


async def _stored(registry: EngineRegistry, group_id: int) -> GroupDonationsAggregate:
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        row = await session.get(GroupDonationsAggregate, group_id)
        assert row is not None
        return row


def _fake_chat(gid: int, *, title: str | None, link: str | None) -> ChatFullInfo:
    """A ``getChat`` response carrying only the two fields we read.

    ``model_construct`` skips validation so the stub doesn't have to
    invent values for the two dozen Bot-API fields this code never looks
    at; the handful mypy still insists on are filled with their neutral
    defaults.
    """
    return ChatFullInfo.model_construct(
        id=gid,
        type="supergroup",
        title=title,
        invite_link=link,
        accent_color_id=0,
        max_reaction_count=11,
        accepted_gift_types=_NO_GIFTS,
    )


class _Calls:
    """Records which Telegram methods the backfill reached for."""

    def __init__(self) -> None:
        self.get_chat: list[int] = []
        self.create: list[int] = []
        # Every kwarg the mint was called with, so a test can assert the
        # link we hand out is gated rather than an open door.
        self.create_kwargs: list[dict[str, object]] = []


def _stub_telegram(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    *,
    title: str | None = "Ком-клуб",
    primary_link: str | None = None,
    minted: str | None = "https://t.me/+minted",
    get_chat_fails: bool = False,
) -> _Calls:
    calls = _Calls()

    async def fake_get_chat(chat_id: int) -> ChatFullInfo:
        calls.get_chat.append(chat_id)
        # A real API call suspends. Without a yield here the whole
        # backfill would run to completion inside one step of the event
        # loop, and the concurrency tests below would be vacuous.
        await asyncio.sleep(0)
        if get_chat_fails:
            msg = "chat not found"
            raise RuntimeError(msg)
        return _fake_chat(chat_id, title=title, link=primary_link)

    async def fake_create(
        chat_id: int,
        name: str | None = None,
        creates_join_request: bool = False,
    ) -> ChatInviteLink:
        calls.create.append(chat_id)
        await asyncio.sleep(0)
        calls.create_kwargs.append({"name": name, "creates_join_request": creates_join_request})
        if minted is None:
            msg = "not enough rights"
            raise RuntimeError(msg)
        # ``is_primary=False`` is the point of using createChatInviteLink
        # rather than legacy's exportChatInviteLink: the group's existing
        # primary link keeps working.
        return ChatInviteLink.model_construct(
            invite_link=minted,
            name=name,
            creator=_BOT_USER,
            creates_join_request=creates_join_request,
            is_primary=False,
            is_revoked=False,
        )

    monkeypatch.setattr(bot, "get_chat", fake_get_chat)
    monkeypatch.setattr(bot, "create_chat_invite_link", fake_create)
    return calls


async def test_a_linkless_group_is_backfilled_and_rendered_clickable(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this restores: prod's #1 group sits on the board as
    a bare chat id nobody can join."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    calls = _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+primary")
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert result is not UNHANDLED
    text = sent[0]["text"]
    assert 'href="https://t.me/+primary"' in text
    assert "Ком-клуб" in text
    # The existing primary link is reused, never re-minted: legacy called
    # ``export_chat_invite_link``, which would have revoked it.
    assert calls.create == []
    row = await _stored(registry, -100)
    assert row.group_link == "https://t.me/+primary"
    assert row.group_name == "Ком-клуб"


async def test_a_link_is_minted_only_when_the_chat_has_none(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    calls = _stub_telegram(bot, monkeypatch, primary_link=None)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert calls.create == [-100]
    assert 'href="https://t.me/+minted"' in sent[0]["text"]
    assert (await _stored(registry, -100)).group_link == "https://t.me/+minted"
    # The link is a join *request*, not an open door — see below.
    assert calls.create_kwargs == [{"name": "Kom rating", "creates_join_request": True}]


async def test_a_minted_link_only_lets_someone_ask_to_join(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/top_groups`` answers anyone in any chat, so an unauthenticated
    stranger decides *when* this mint happens and inside which third-party
    group. A plain ``createChatInviteLink`` is permanent and unlimited —
    that stranger would be publishing entry to a private group whose
    admins never agreed to it. ``creates_join_request=True`` keeps the row
    clickable (the whole point of the backfill) while leaving the decision
    with the group's own admins.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    calls = _stub_telegram(bot, monkeypatch, primary_link=None)
    capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/top_groups", chat_type="supergroup", user_id=777)
    )

    assert [kw["creates_join_request"] for kw in calls.create_kwargs] == [True]


async def test_the_name_is_saved_even_when_no_link_can_be_obtained(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case: the bot can see the group but isn't an admin, so
    it can't make a link. A named row still beats a bare chat id."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    _stub_telegram(bot, monkeypatch, primary_link=None, minted=None)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    text = sent[0]["text"]
    assert "Ком-клуб" in text
    assert "href=" not in text
    row = await _stored(registry, -100)
    assert row.group_name == "Ком-клуб"
    assert row.group_link is None


async def test_a_total_failure_still_renders_the_page(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enrichment is best-effort — a dead ``get_chat`` must not cost the
    caller their leaderboard."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    _stub_telegram(bot, monkeypatch, get_chat_fails=True, minted=None)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert result is not UNHANDLED
    assert "Рейтинг групп" in sent[0]["text"]
    assert "-100" in sent[0]["text"]


async def test_a_group_that_refused_is_not_asked_again_on_the_next_render(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the cooldown, every view of the board would pay two doomed
    round-trips for a group the bot has no rights in."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    calls = _stub_telegram(bot, monkeypatch, get_chat_fails=True, minted=None)
    capture_outgoing(bot)

    for _ in range(3):
        await dispatcher.feed_update(
            bot, make_message_update("/rating", chat_type="private", user_id=1)
        )

    assert calls.get_chat == [-100]
    assert calls.create == [-100]


async def test_at_most_two_groups_are_enriched_per_render(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full page is ten rows; enriching all of them on every pagination
    tap is a flood-wait waiting to happen. Legacy capped at 2 too."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100, -101, -102, -103, -104)
    calls = _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+primary")
    capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert calls.get_chat == [-100, -101]


async def test_an_excluded_group_disappears_from_the_leaderboard(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/rating_exclude`` writes ``in_rating = 0``; until RR-1 #9 the
    read side ignored it and the group kept its place on the board."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100, in_rating=0)
    await _seed_bare(registry, -200)
    calls = _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+primary")
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    text = sent[0]["text"]
    assert "-100" not in text
    assert "Ком-клуб" in text  # the still-included group did render
    # An opted-out group must not have a link minted for it either.
    assert calls.get_chat == [-200]


async def test_every_group_excluded_is_the_empty_state(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100, -101, in_rating=0)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert "нет групп" in sent[0]["text"].lower()


async def test_a_legacy_row_with_no_flag_stays_on_the_board(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy monolith still writes ``groups_donations`` through raw
    SQL that never names ``in_rating`` — reproduced literally here, since
    a row it inserts must not silently vanish from the leaderboard.

    ``in_rating`` is ``NOT NULL DEFAULT 1``, so what such an INSERT
    actually lands is the default rather than a NULL. The read filter
    handles NULL anyway (see ``_ranked``); that branch is pinned by
    ``test_rating_backfill`` at the SQL level, because no schema this
    suite can build will store one.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        await session.execute(
            sql_text(
                "INSERT INTO groups_donations "
                "(group_id, group_name, group_link, group_xp) "
                "VALUES (-100, 'Старый', 'https://t.me/+old', 50)"
            )
        )
        await session.commit()
    _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+primary")
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert "Старый" in sent[0]["text"]


async def test_the_backfill_also_runs_on_the_pagination_callback(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page 2 rows are never seen by the command handler, so the callback
    path needs its own pass or those rows stay unlinked forever."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_groups(registry, 10)  # ranks 1..10 (XP 1000..991)
    await _seed_bare(registry, -300, xp=1)  # lowest XP → rank 11, page 2
    calls = _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+page2")
    sink = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_callback_update(RatingNav(page=2).pack(), user_id=1, language_code="ru")
    )

    assert calls.get_chat == [-300]
    edits = [e for e in sink if e["kind"] == "edit"]
    assert 'href="https://t.me/+page2"' in edits[0]["text"]
    assert (await _stored(registry, -300)).group_link == "https://t.me/+page2"


async def test_equal_xp_rows_keep_a_stable_order_across_pages(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Ordering by XP alone leaves ties to SQLite's discretion, which can
    shuffle a row between page 1 and page 2 and make it vanish."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for gid in (-102, -100, -101):
            session.add(
                GroupDonationsAggregate(
                    group_id=gid,
                    group_name=f"G{gid}",
                    group_link="https://t.me/+x",  # already linked → no backfill
                    group_xp=7,
                )
            )
        await session.commit()
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    lines = [ln for ln in sent[0]["text"].splitlines() if ln.startswith(("1.", "2.", "3."))]
    assert [ln.split(". ", 1)[0] for ln in lines] == ["1", "2", "3"]
    assert "G-102" in lines[0]  # ties broken by group_id ASC
    assert "G-101" in lines[1]
    assert "G-100" in lines[2]


async def test_a_junk_link_is_replaced_rather_than_published(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-Telegram value in ``group_link`` counts as missing on both
    sides: it is not rendered as an href, and the backfill overwrites it."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            GroupDonationsAggregate(
                group_id=-100,
                group_name="Клуб",
                group_link="https://phish.example/joinchat/abc",
                group_xp=10,
            )
        )
        await session.commit()
    _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+real")
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert "phish.example" not in sent[0]["text"]
    assert 'href="https://t.me/+real"' in sent[0]["text"]
    assert (await _stored(registry, -100)).group_link == "https://t.me/+real"


async def test_a_renamed_group_gets_its_title_refreshed(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the backfill visits a group anyway, Telegram's live title wins
    over whatever was stored — legacy kept the stale one, so a renamed
    group advertised itself under its old name indefinitely."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(GroupDonationsAggregate(group_id=-100, group_name="Старое имя", group_xp=9))
        await session.commit()
    _stub_telegram(bot, monkeypatch, title="Новое имя", primary_link="https://t.me/+n")
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert "Новое имя" in sent[0]["text"]
    assert "Старое имя" not in sent[0]["text"]
    assert (await _stored(registry, -100)).group_name == "Новое имя"


async def test_a_nameless_chat_does_not_blank_a_stored_name(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``title=None`` means "we didn't learn one", not "it has none"."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(GroupDonationsAggregate(group_id=-100, group_name="Известное", group_xp=9))
        await session.commit()
    _stub_telegram(bot, monkeypatch, title=None, primary_link="https://t.me/+n")
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert "Известное" in sent[0]["text"]
    assert (await _stored(registry, -100)).group_name == "Известное"


async def test_the_backfill_never_creates_an_aggregate_row(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write is an UPDATE, not an upsert — a group with no donations
    has no business on a donations leaderboard."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+primary")
    capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        ids = (await session.execute(select(GroupDonationsAggregate.group_id))).scalars().all()
    assert list(ids) == [-100]


async def test_two_concurrent_renders_never_mint_the_same_link_twice(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why every target is claimed *before* the first ``await``.

    Marking inside the fetch loop would leave targets 2..n unclaimed
    across target 1's two round-trips, so a render arriving in that window
    picks them up again — and each duplicate is a real invite link created
    inside somebody's group.

    Driven through :func:`_backfill_identity` rather than the dispatcher
    because the window is a few microseconds wide: the second render is
    released deliberately while the first is suspended inside its first
    ``get_chat``, which is exactly the interleaving being defended
    against and is not reproducible by racing whole updates.
    """
    bot, _dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100, -101)
    calls = _stub_telegram(bot, monkeypatch, primary_link=None)

    ok_get_chat = bot.get_chat
    suspended = asyncio.Event()
    release = asyncio.Event()

    async def gated_get_chat(chat_id: int) -> ChatFullInfo:
        suspended.set()
        await release.wait()
        return await ok_get_chat(chat_id)

    monkeypatch.setattr(bot, "get_chat", gated_get_chat)
    rows = [
        _RatingRow(group_id=gid, group_name=None, group_link=None, xp=10) for gid in (-100, -101)
    ]

    first = asyncio.create_task(_backfill_identity(bot, registry, list(rows)))
    await suspended.wait()  # first render is mid-flight on target 1
    second = asyncio.create_task(_backfill_identity(bot, registry, list(rows)))
    await asyncio.sleep(0)  # let it choose its targets from the same page
    release.set()
    await asyncio.gather(first, second)

    assert sorted(calls.create) == [-101, -100]


async def test_a_flood_wait_does_not_poison_the_group_for_six_hours(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal is permanent-ish and deserves the cooldown; a flood-wait
    is the opposite — transient, and precisely the case where a six-hour
    mark would turn "slow down" into "never ask again"."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    calls = _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+primary")
    capture_outgoing(bot)

    flooded = {"first": True}
    ok_get_chat = bot.get_chat

    async def flaky_get_chat(chat_id: int) -> ChatFullInfo:
        if flooded["first"]:
            flooded["first"] = False
            raise TelegramRetryAfter(
                method=GetChat(chat_id=chat_id), message="flood", retry_after=5
            )
        return await ok_get_chat(chat_id)

    monkeypatch.setattr(bot, "get_chat", flaky_get_chat)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )
    assert _BACKFILL_BLOCKED == {}, "a flood-wait must not leave a mark"

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )
    assert calls.get_chat == [-100]
    assert (await _stored(registry, -100)).group_link == "https://t.me/+primary"


async def test_a_flood_wait_abandons_the_rest_of_the_batch(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telegram just said "back off" — the second target waits for the
    next render rather than being pushed through the same closed door."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100, -101)
    calls = _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+primary")
    capture_outgoing(bot)

    async def flooding_get_chat(chat_id: int) -> ChatFullInfo:
        calls.get_chat.append(chat_id)
        raise TelegramRetryAfter(method=GetChat(chat_id=chat_id), message="flood", retry_after=5)

    monkeypatch.setattr(bot, "get_chat", flooding_get_chat)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert calls.get_chat == [-100]
    assert calls.create == []
    assert _BACKFILL_BLOCKED == {}


async def test_a_failed_write_is_retried_on_the_next_render(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page renders values that never reached the DB. Holding the
    cooldown would mean the row stays unlinked for six hours *and* nothing
    ever tries again."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    calls = _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+primary")
    sent = capture_outgoing(bot)

    broken = {"still": True}
    real_save = DonationsRatingRepo.save_group_identity

    async def flaky_save(
        self: DonationsRatingRepo, group_id: int, *, link: str | None, title: str | None
    ) -> bool:
        if broken["still"]:
            broken["still"] = False
            msg = "database is locked"
            raise RuntimeError(msg)
        return await real_save(self, group_id, link=link, title=title)

    monkeypatch.setattr(DonationsRatingRepo, "save_group_identity", flaky_save)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )
    # The reader still gets a usable board out of the values in hand...
    assert 'href="https://t.me/+primary"' in sent[0]["text"]
    # ...but nothing was stored, so nothing is on cooldown either.
    assert (await _stored(registry, -100)).group_link is None
    assert _BACKFILL_BLOCKED == {}

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )
    assert calls.get_chat == [-100, -100]
    assert (await _stored(registry, -100)).group_link == "https://t.me/+primary"


async def test_the_cooldown_lets_go_once_it_expires(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mark is a cooldown, not a tombstone: a bot that gets promoted
    to admin tomorrow must be able to fill the row in."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    calls = _stub_telegram(bot, monkeypatch, get_chat_fails=True, minted=None)
    capture_outgoing(bot)

    clock = {"now": 1_000.0}
    monkeypatch.setattr("telegram_invite_bot.handlers.rating.monotonic", lambda: clock["now"])

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )
    assert calls.get_chat == [-100]

    clock["now"] += _BACKFILL_RETRY_AFTER - 1
    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )
    assert calls.get_chat == [-100], "still inside the cooldown"

    clock["now"] += 2
    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )
    assert calls.get_chat == [-100, -100]


async def test_the_cooldown_is_honoured_across_pages(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tapping the pagination arrows is the cheapest way to hammer this
    path, so the mark has to survive a re-render rather than being
    per-page."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    calls = _stub_telegram(bot, monkeypatch, get_chat_fails=True, minted=None)
    capture_callback_outgoing(bot)

    for _ in range(4):
        await dispatcher.feed_update(bot, make_callback_update(RatingNav(page=1).pack(), user_id=1))

    assert calls.get_chat == [-100]


async def test_a_positive_chat_id_is_never_asked_about(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-negative id is a user chat, not a group: ``get_chat`` would
    succeed and the mint is guaranteed to fail. Legacy skipped these
    (bot.py:25143) and so do we."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, 4242)
    calls = _stub_telegram(bot, monkeypatch, primary_link="https://t.me/+primary")
    capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert calls.get_chat == []
    assert calls.create == []


async def test_an_excluded_group_card_is_not_reachable_from_an_old_message(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A leaderboard message keeps working after it is sent, so the row
    disappearing from the board is not enough — the drill-down behind it
    has to honour ``/rating_exclude`` too. Otherwise "hide us from the
    rating" hides the row and keeps serving the numbers to anyone with an
    hour-old message in their history."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100, in_rating=0)
    sink = capture_callback_outgoing(bot)

    result = await dispatcher.feed_update(
        bot,
        make_callback_update(RatingGroupStats(group_id=-100).pack(), user_id=1, language_code="ru"),
    )

    assert result is not UNHANDLED
    assert not [e for e in sink if e["kind"] == "edit"]
    answers = [e for e in sink if e["kind"] == "callback_answer"]
    assert len(answers) == 1
    assert answers[0]["text"]


async def test_rating_does_not_hold_the_write_lock_across_the_backfill(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#220: the board asks Telegram for a title per row it cannot name.

    ``user_service.touch`` opens the update's ``users.db`` transaction and
    ``BEGIN IMMEDIATE`` means one writer per DB until the middleware
    commits — which, without a checkpoint, is only after the whole
    backfill has answered. Every other update wanting ``users.db`` (that
    includes ``/start``) waits out ``busy_timeout`` (5 s) and fails with
    ``database is locked``; the webhook still answers 200, so Telegram
    never redelivers and that update's work is simply lost. The probe
    below runs *inside* the backfill, exactly where the real ``getChat``
    calls are in flight.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_groups(registry, 3)
    sent = capture_outgoing(bot)
    other_updates_could_write: list[bool] = []

    async def backfill_while_probing(
        _bot: Bot, _registry: EngineRegistry, rows: list[_RatingRow]
    ) -> list[_RatingRow]:
        async with registry.session(DBName.USERS)() as other:
            await other.execute(sql_update(User).where(User.user_id == 1).values(messages_count=1))
            await other.commit()
        other_updates_could_write.append(True)
        return rows

    monkeypatch.setattr(rating_module, "_backfill_identity", backfill_while_probing)

    await dispatcher.feed_update(
        bot, make_message_update("/rating", chat_type="private", user_id=1)
    )

    assert other_updates_could_write == [True]
    assert "Рейтинг групп" in sent[0]["text"]


# ``rating_history`` ships in migration 0009 as a raw-SQL table (no ORM
# model), so ``create_all(EconomyBase)`` doesn't produce it — same
# fixture shape as tests/e2e/handlers/test_group_pay.py. Both write-side
# entrypoints below snapshot into it, so it has to exist for the happy
# path to run to completion.
_RATING_HISTORY_DDL = (
    "CREATE TABLE rating_history ("
    "  group_id INTEGER NOT NULL,"
    "  date TEXT NOT NULL,"
    "  total_donations INTEGER NOT NULL,"
    "  position INTEGER,"
    "  PRIMARY KEY (group_id, date)"
    ")"
)


async def _create_rating_history(registry: EngineRegistry) -> None:
    async with registry.session(DBName.ECONOMY)() as session:
        await session.execute(sql_text(_RATING_HISTORY_DDL))
        await session.commit()


async def test_rating_toggle_releases_the_write_lock_before_the_admin_probe(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#234: ``/rating_exclude`` cannot decide anything until
    ``getChatMember`` comes back, and ``user_service.touch`` has already
    taken ``users.db`` under ``BEGIN IMMEDIATE``. Same contract the
    backfill test above pins for the read side (#220): every other update
    in the process must still be able to write while we wait on Telegram.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    await _seed_bare(registry, -100)
    await _create_rating_history(registry)
    capture_outgoing(bot)
    other_updates_could_write: list[bool] = []

    async def probe_while_authorising(
        _bot: Bot, _settings: Any, *, chat_id: int, user_id: int
    ) -> bool:
        # Stands in for the ``getChatMember`` round-trip: if the caller's
        # own transaction were still open, this second writer would fail
        # with "database is locked" instead of appending.
        async with registry.session(DBName.USERS)() as other:
            await other.execute(sql_update(User).where(User.user_id == 1).values(messages_count=1))
            await other.commit()
        other_updates_could_write.append(True)
        return True

    monkeypatch.setattr(rating_module, "_caller_is_rating_admin", probe_while_authorising)

    await dispatcher.feed_update(
        bot,
        make_message_update("/rating_exclude", chat_type="supergroup", chat_id=-100, user_id=1),
    )

    assert other_updates_could_write == [True]
    # ...and the toggle still did its job once the probe returned.
    assert (await _stored(registry, -100)).in_rating == 0


async def test_rating_recalc_releases_the_write_lock_before_the_recalc(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#234: ``/rating_recalc`` walks every included group (one UPDATE
    per group) and then replies — all of it after ``touch`` opened this
    update's ``users.db`` transaction. The checkpoint sits *after* the
    developer gate, so a non-dev caller (dropped silently) never pays for
    it; a developer does.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=555),
    )
    await _seed_bare(registry, -100)
    await _create_rating_history(registry)
    capture_outgoing(bot)
    other_updates_could_write: list[bool] = []
    real_recalc = DonationsRatingRepo.recalc_positions

    async def probe_then_recalc(self: DonationsRatingRepo) -> int:
        async with registry.session(DBName.USERS)() as other:
            await other.execute(
                sql_update(User).where(User.user_id == 555).values(messages_count=1)
            )
            await other.commit()
        other_updates_could_write.append(True)
        return await real_recalc(self)

    monkeypatch.setattr(DonationsRatingRepo, "recalc_positions", probe_then_recalc)

    await dispatcher.feed_update(
        bot, make_message_update("/rating_recalc", chat_type="private", user_id=555)
    )

    assert other_updates_could_write == [True]
