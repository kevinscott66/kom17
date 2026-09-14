"""End-to-end ``/chatstats`` flow: dispatcher → SessionMiddleware +
MessageStatsMiddleware → repos → PNG card + caption (A-08, RR-1 #6).

Since RR-1 #6 the card ships as a **photo with a caption** and a
keyboard, so assertions read ``sent[0]["caption"]``. The six blocks span
two databases (``message_stats`` for activity, ``economy`` for games /
coins / donations), which is why every wiring here seeds both.

The handler is group-gated in-handler (not a router filter) so a private
invocation gets the localised "only in group" refusal instead of falling
through. Names for the top-3 list resolve via ``bot.get_chat_member`` —
the conftest's ``GetChatMember`` stub returns a non-bot member named
``X`` deterministically, and ``GetChatMemberCount`` reports
:data:`STUB_MEMBER_COUNT`, so the rendered card is stable without
freezing the network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import update as sql_update

from telegram_invite_bot.config.settings import StatsConfig
from telegram_invite_bot.db.models.base import EconomyBase, MessageStatsBase, UsersBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    GameResult,
    GroupDonationsAggregate,
)
from telegram_invite_bot.db.models.message_stats import MessageCount
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import chatstats
from tests.e2e.handlers.conftest import (
    STUB_MEMBER_COUNT,
    _try_capture_send,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from aiogram import Bot
    from aiogram.types import Update

    from tests.e2e.handlers.conftest import WiredFactory

# UTC has no DST around the seeded dates, which keeps the naive-UTC
# ``games.date`` seeds below readable. The tz→UTC conversion itself is
# pinned in ``tests/unit/handlers/test_chatstats_helpers.py`` and
# end-to-end by ``test_chatstats_anchors_the_games_day_to_the_stats_tz``.
_TZ = "UTC"
# The handler's clock is frozen (see ``_freeze_clock``) rather than read
# from the wall: the handler and the seeds each computed
# ``datetime.now(...).date()`` independently, so a run that crossed UTC
# midnight between the two pushed every seeded row out of the window and
# failed with counts of zero.
_FROZEN_NOW = datetime(2024, 6, 10, 12, 0, tzinfo=UTC)
_TODAY = _FROZEN_NOW.date()


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin ``handlers.chatstats``'s view of "now" to :data:`_FROZEN_NOW`.

    A ``datetime`` subclass rather than a bare stub, because the module
    also calls ``datetime.combine`` when it converts the local day to
    UTC bounds.
    """

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return _FROZEN_NOW if tz is None else _FROZEN_NOW.astimezone(tz)

    monkeypatch.setattr(chatstats, "datetime", _Clock)
    yield


def _update(
    text: str,
    *,
    chat_id: int = -100,
    chat_type: str = "supergroup",
    user_id: int = 9,
) -> Update:
    return make_message_update(
        text,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Eve",
    )


def _card(sent: list[dict[str, Any]]) -> str:
    """The /chatstats card body, whichever way it went out.

    The happy path is a photo caption; the render/send fallbacks answer
    with the identical text. Tests that aren't *about* the fallback
    shouldn't care which one fired.
    """
    entry = sent[0]
    return entry.get("caption") or entry.get("text") or ""


async def _seed_chat(
    registry: Any,
    *,
    chat_id: int,
    rows: list[tuple[int, date, int]],
) -> None:
    sessionmaker = registry.session(DBName.MESSAGE_STATS)
    async with sessionmaker() as session:
        for user_id, day, count in rows:
            session.add(
                MessageCount(
                    user_id=user_id,
                    chat_id=chat_id,
                    date=day.isoformat(),
                    count=count,
                    last_message=datetime(2024, 1, 1, 12, 0, 0),
                )
            )
        await session.commit()


async def _seed_economy(
    registry: Any,
    *,
    wallets: list[tuple[int, int, int, int]] | None = None,
    games: list[datetime] | None = None,
    donations: tuple[int, int, int, int] | None = None,
) -> None:
    """Seed ``economy.db``.

    ``wallets`` is ``(user_id, balance, games_played, games_won)``;
    ``games`` a list of **naive UTC** timestamps (what ``record_game``
    writes); ``donations`` is ``(group_id, treasury, group_xp,
    rating_position)`` — the two int columns are deliberately seeded with
    *different* values so a test can tell which one the card prints.
    """
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        for user_id, balance, played, won in wallets or []:
            session.add(
                EconomyUser(
                    user_id=user_id,
                    balance=balance,
                    games_played=played,
                    games_won=won,
                )
            )
        for stamp in games or []:
            session.add(GameResult(user_id=1, game="duel", bet=10, win=True, profit=10, date=stamp))
        if donations is not None:
            group_id, treasury, group_xp, position = donations
            session.add(
                GroupDonationsAggregate(
                    group_id=group_id,
                    group_name="Test group",
                    total_donations=treasury,
                    group_xp=group_xp,
                    rating_position=position,
                )
            )
        await session.commit()


async def _make(make_wired: WiredFactory, *, tz: str = _TZ) -> tuple[Bot, Any, Any]:
    return await make_wired(
        schemas=[MessageStatsBase, UsersBase, EconomyBase],
        session_middleware=True,
        stats_config=StatsConfig(STATS_PERIOD_DAYS=7, STATS_TIMEZONE=tz),
    )


async def test_chatstats_renders_activity_and_top(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)

    today = _TODAY
    # Three users across several days in one chat.
    #   today total   = 3 + 4 + 1          = 8
    #   week total    = 8 + 10 (user1 d-1) + 5 (user2 d-3) = 23
    #   avg per day    = 23 // 7            = 3
    #   active (7d)    = users {1, 2, 3}    = 3
    #   top order      = user1(13), user2(9), user3(1)
    await _seed_chat(
        registry,
        chat_id=-100,
        rows=[
            (1, today, 3),
            (2, today, 4),
            (3, today, 1),
            (1, today - timedelta(days=1), 10),
            (2, today - timedelta(days=3), 5),
            # Out-of-window noise — must NOT appear anywhere.
            (1, today - timedelta(days=30), 9999),
        ],
    )

    result = await dispatcher.feed_update(bot, _update("/chatstats"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert sent[0]["kind"] == "photo"
    body = _card(sent)

    # Members: live total from the stub + distinct posters over 7d.
    assert f"Всего: {STUB_MEMBER_COUNT}" in body
    assert "За 7 дн.: 3" in body
    # Activity line.
    assert "Сегодня: 8 | За неделю: 23 | В ср.: 3/день" in body
    # Out-of-window value never leaks — including into the 30d preview
    # bar, which is drawn on the PNG rather than written in the caption.
    assert "9999" not in body
    # Top-3 ordered by count desc; names resolved to "X" by the stub.
    pos1 = body.index('tg://user?id=1"')
    pos2 = body.index('tg://user?id=2"')
    pos3 = body.index('tg://user?id=3"')
    assert pos1 < pos2 < pos3
    assert "13 сообщ." in body
    assert "9 сообщ." in body
    assert "1 сообщ." in body


async def test_chatstats_counts_newcomers_by_first_seen_day(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Only users whose FIRST ever row lands in the week are "new".

    user 1 has been around for a month; user 2 first spoke yesterday.
    A window-then-group implementation would call both of them new.
    """
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    today = _TODAY
    await _seed_chat(
        registry,
        chat_id=-100,
        rows=[
            (1, today - timedelta(days=30), 5),
            (1, today, 2),
            (2, today - timedelta(days=1), 4),
        ],
    )

    await dispatcher.feed_update(bot, _update("/chatstats"))
    assert "Новых за неделю: 1" in _card(sent)


async def test_chatstats_renders_games_and_economy(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    midnight = datetime.combine(_TODAY, datetime.min.time())
    await _seed_economy(
        registry,
        # coins = 1000 + 500 = 1500; avg = 750; richest = 1000
        # lifetime games = 4 + 2 = 6
        wallets=[(1, 1000, 4, 3), (2, 500, 2, 1)],
        games=[
            midnight,
            midnight + timedelta(hours=13),
            # Yesterday — outside the half-open [today, tomorrow) window.
            midnight - timedelta(minutes=1),
        ],
    )

    await dispatcher.feed_update(bot, _update("/chatstats"))
    body = _card(sent)
    assert "Сегодня: 2 | Всего: 6" in body
    assert "В обороте: 1 500 🪙" in body
    assert "Ср. баланс: 750 🪙" in body
    assert "Богач: 1 000 🪙" in body


async def test_the_two_bot_wide_blocks_say_so_in_their_headings(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1255: the card is titled «Статистика чата», but two of its blocks
    are not chat-scoped at all.

    ``EconomyRepo.economy_snapshot`` is documented at
    ``economy_repo.py:785`` as a whole-economy aggregate, and
    ``count_games_between`` (``economy_repo.py:824``) takes no chat
    argument either — so BOTH numbers in the 🎮 block, not just the
    lifetime total, are bot-wide. That is not fixable at the query layer:
    prod's ``economy.games`` has no chat column, and legacy printed the
    same globals here (bot.py:41008-41011). What was fixable is the
    heading, which until now let any member of any group read the bot's
    global coin supply, mean and richest balance as if it described their
    own chat.

    Whether those globals should be visible to a non-owner at all is a
    separate, owner-level question; this only stops the card from
    mislabelling them.
    """
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    await _seed_economy(registry, wallets=[(1, 1000, 4, 3)])

    await dispatcher.feed_update(bot, _update("/chatstats"))
    body = _card(sent)

    assert "Игры (по всему боту)" in body
    assert "Экономика (по всему боту)" in body


async def test_chatstats_renders_donations_when_the_group_has_them(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    await _seed_economy(registry, donations=(-100, 7, 25_000, 3))

    await dispatcher.feed_update(bot, _update("/chatstats"))
    body = _card(sent)
    assert "Всего донатов: 25 000 🪙" in body
    assert "Место в рейтинге: 3" in body


async def test_chatstats_donations_line_reads_group_xp_not_the_treasury(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Regression (#475): the printed total is ``group_xp``.

    Legacy's ``total_donations`` dict key was built from
    ``COALESCE(group_xp, 0)`` (bot.py:10812), so it never showed the
    column of the same name. That column is the withdrawable group
    treasury, which goes *down* on a withdrawal
    (``treasury_repo.debit``) and is never credited by a donation
    (``donations_rating_repo``) — printing it as a lifetime total was
    wrong in both directions.
    """
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    # A group that donated 9 000 and has since withdrawn its treasury
    # down to 0: the lifetime line must still read 9 000.
    await _seed_economy(registry, donations=(-100, 0, 9_000, 1))

    await dispatcher.feed_update(bot, _update("/chatstats"))
    body = _card(sent)
    assert "Всего донатов: 9 000 🪙" in body
    assert "Всего донатов: 0 🪙" not in body


async def test_chatstats_omits_the_donations_block_without_an_aggregate(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No aggregate row → no 💸 block, and no row created as a side effect.

    Legacy called ``donations_ensure_group`` while *reading* the card;
    this port is read-only, so the table must still be empty afterwards.
    """
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update("/chatstats"))
    assert "Донаты" not in _card(sent)

    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        assert await session.get(GroupDonationsAggregate, -100) is None


async def test_chatstats_carries_the_rating_and_boost_buttons(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keyboard rides along with the photo (legacy shipped two rows)."""
    bot, dispatcher, _ = await _make(make_wired)
    markups: list[Any] = []

    # Same instance-level patch shape as ``capture_outgoing``; that
    # fixture's sink drops ``reply_markup``, which is the one field
    # this test is about.
    async def _capture(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        markups.append(getattr(method, "reply_markup", None))
        response = _try_capture_send(method, [])
        if response is None:
            raise AssertionError(f"unexpected Telegram call: {type(method).__name__}")
        return response

    monkeypatch.setattr(bot.session, "make_request", _capture)
    await dispatcher.feed_update(bot, _update("/chatstats"))

    keyboard = next(m for m in markups if m is not None)
    buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    assert [btn.callback_data for btn in buttons] == ["ratnav:1", "gsboost"]


async def test_chatstats_falls_back_to_text_when_the_photo_is_refused(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 on sendPhoto must still deliver the numbers as text."""
    from aiogram.methods import SendPhoto

    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    today = _TODAY
    await _seed_chat(registry, chat_id=-100, rows=[(1, today, 7)])

    original = bot.__class__.__call__

    async def _refuse_photo(self: Any, method: Any, *a: Any, **kw: Any) -> Any:
        if isinstance(method, SendPhoto):
            raise TelegramBadRequest(method=method, message="not enough rights")
        return await original(self, method, *a, **kw)

    monkeypatch.setattr(bot.__class__, "__call__", _refuse_photo)
    await dispatcher.feed_update(bot, _update("/chatstats"))

    assert [e["kind"] for e in sent] == ["text"]
    assert "Сегодня: 7" in sent[0]["text"]


async def test_chatstats_anchors_the_games_day_to_the_stats_tz(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """ "Игры сегодня" follows ``STATS_TIMEZONE``, not the UTC day.

    Frozen now is 12:00 UTC = 15:00 MSK on 2024-06-10, so the Moscow day
    is ``[2024-06-09 21:00, 2024-06-10 21:00)`` in the naive-UTC values
    ``games.date`` stores. The two seeds straddle that boundary and sit
    on the *same* UTC day — an implementation that skipped the
    conversion (legacy's ``date('now')``) would count them the other way
    round and report 0.
    """
    bot, dispatcher, registry = await _make(make_wired, tz="Europe/Moscow")
    sent = capture_outgoing(bot)
    await _seed_economy(
        registry,
        games=[
            datetime(2024, 6, 9, 21, 30),  # 10 июня 00:30 МСК — внутри
            datetime(2024, 6, 9, 20, 30),  # 9 июня 23:30 МСК — снаружи
        ],
    )

    await dispatcher.feed_update(bot, _update("/chatstats"))
    assert "Сегодня: 1 | Всего: 0" in _card(sent)


async def test_chatstats_drops_the_member_total_when_telegram_wont_say(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``getChatMemberCount`` costs one segment, not the card.

    The point is the *absence* of a fabricated ``Всего: 0`` — a group
    with zero members is something a reader would believe.
    """
    from aiogram.methods import GetChatMemberCount

    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    await _seed_chat(registry, chat_id=-100, rows=[(1, _TODAY, 7)])

    original = bot.__class__.__call__

    async def _refuse_count(self: Any, method: Any, *a: Any, **kw: Any) -> Any:
        if isinstance(method, GetChatMemberCount):
            raise TelegramBadRequest(method=method, message="chat not found")
        return await original(self, method, *a, **kw)

    monkeypatch.setattr(bot.__class__, "__call__", _refuse_count)
    await dispatcher.feed_update(bot, _update("/chatstats"))

    body = _card(sent)
    # Asserted on the 👥 line itself, not on the whole card: the 🎮 block
    # has its own lifetime "Всего:" segment.
    members_line = next(line for line in body.splitlines() if "За 7 дн." in line)
    assert members_line == " • За 7 дн.: 1 | Новых за неделю: 1"
    assert f"Всего: {STUB_MEMBER_COUNT}" not in body
    # Every other block survived.
    assert "Сегодня: 7 | За неделю: 7" in body


async def test_chatstats_falls_back_to_text_when_the_preview_cannot_render(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rasteriser blow-up must not cost the six blocks of numbers.

    Distinct from the ``sendPhoto``-refused path below: here nothing is
    ever offered to Telegram, so the card has to go out as plain text on
    the first and only attempt.
    """
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    await _seed_chat(registry, chat_id=-100, rows=[(1, _TODAY, 7)])

    def _explode(*_a: Any, **_kw: Any) -> bytes:
        raise OSError("truncated font")

    monkeypatch.setattr(chatstats, "render_stats_card", _explode)
    await dispatcher.feed_update(bot, _update("/chatstats"))

    assert [e["kind"] for e in sent] == ["text"]
    assert "Сегодня: 7 | За неделю: 7" in sent[0]["text"]


async def test_chatstats_private_refuses(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await _make(make_wired)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(
        bot, _update("/chatstats", chat_id=9, chat_type="private")
    )
    assert result is not UNHANDLED
    assert len(sent) == 1
    # The "only in group" refusal, not the activity card.
    assert "только в группах" in sent[0]["text"].lower()
    assert "Активность" not in sent[0]["text"]


async def test_chatstats_empty_group_shows_zeros(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await _make(make_wired)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/chatstats"))
    assert result is not UNHANDLED
    body = _card(sent)
    assert "За 7 дн.: 0" in body
    assert "Сегодня: 0 | За неделю: 0 | В ср.: 0/день" in body
    # Empty economy must read as zeros, not a ZeroDivisionError on the mean.
    assert "Ср. баланс: 0 🪙" in body
    # No top rows → empty placeholder, no crash.
    assert "пока нет сообщений" in body


async def test_chatstats_alias_cstats(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await _make(make_wired)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/cstats"))
    assert result is not UNHANDLED
    assert sent and "Активность" in _card(sent)


# ── /chatinfo (CMD-2) — alias of /chatstats ──────────────────────────


async def test_chatinfo_renders_same_activity_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """/chatinfo is a legacy thin alias of /chatstats — renders the card."""
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    today = _TODAY
    await _seed_chat(registry, chat_id=-100, rows=[(1, today, 5)])

    result = await dispatcher.feed_update(bot, _update("/chatinfo"))
    assert result is not UNHANDLED
    assert "За 7 дн.: 1" in _card(sent)  # the chatstats card body


# ── /topactive (CMD-2) ───────────────────────────────────────────────


async def test_topactive_renders_leaderboard_with_medals(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    today = _TODAY
    await _seed_chat(
        registry,
        chat_id=-100,
        rows=[(1, today, 10), (2, today, 7), (3, today, 3)],
    )

    result = await dispatcher.feed_update(bot, _update("/topactive"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "ТОП АКТИВНОСТИ ЗА 7 ДН." in body
    assert "🥇" in body and "🥈" in body and "🥉" in body
    # Ordered by count desc.
    assert body.index('tg://user?id=1"') < body.index('tg://user?id=2"')
    assert "10 сообщ." in body


async def test_topactive_honours_days_arg(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    today = _TODAY
    await _seed_chat(registry, chat_id=-100, rows=[(1, today, 4)])

    await dispatcher.feed_update(bot, _update("/topactive 14"))
    assert "ТОП АКТИВНОСТИ ЗА 14 ДН." in sent[0]["text"]


async def test_topactive_private_refuses(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot, _update("/topactive", chat_type="private", chat_id=9)
    )
    assert result is not UNHANDLED
    assert "только в групп" in (sent[-1]["text"] or "")


async def test_topactive_does_not_hold_the_write_lock_across_the_name_lookups(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#220: ten sequential ``getChatMember`` calls, one writer per DB.

    ``user_service.touch`` opens the update's ``users.db`` transaction,
    and ``BEGIN IMMEDIATE`` holds it until the middleware commits — after
    the whole leaderboard has been named, without a checkpoint. The probe
    runs inside the name lookup, where the real round-trip would be.
    """
    bot, dispatcher, registry = await _make(make_wired)
    sent = capture_outgoing(bot)
    await _seed_chat(registry, chat_id=-100, rows=[(1, _TODAY, 5)])
    other_updates_could_write: list[bool] = []

    async def resolve_while_probing(_bot: Bot, _chat_id: int, user_id: int) -> str:
        async with registry.session(DBName.USERS)() as other:
            await other.execute(
                sql_update(User).where(User.user_id == user_id).values(messages_count=1)
            )
            await other.commit()
        other_updates_could_write.append(True)
        return f"U{user_id}"

    monkeypatch.setattr(chatstats, "_resolve_name", resolve_while_probing)

    await dispatcher.feed_update(bot, _update("/topactive"))

    assert other_updates_could_write == [True]
    assert "ТОП АКТИВНОСТИ" in sent[0]["text"]
