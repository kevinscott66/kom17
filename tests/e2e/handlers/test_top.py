"""End-to-end ``/top`` (Stages 20 + 21).

Wiring + filter contract worth proving here:

* Bare ``/top`` in a group → 30-day messages leaderboard (Stage 20).
* ``/top messages 7`` → days respected (Stage 20).
* Bare ``/top`` in **private** → balance leaderboard, top 10
  (Stage 21; was UNHANDLED in Stage 20).
* ``/top balance`` in private or group → balance leaderboard (Stage 21).
* ``/top unknown`` and every malformed argument → no leaderboard, and
  since #158 an unknown-form hint rather than silence. (``games`` /
  ``wins`` / ``streak`` are no longer in that list — T-016 ported the
  games_stats aggregates behind them.)
* Names join cross-DB via :class:`UsersRepo`; missing users render
  the ``"Пользователь"`` fallback, same as legacy's ID-fallback.
* HTML escape of ``first_name`` — same risk class as ``/marriages``.

Migrated to the shared ``make_wired`` / ``capture_outgoing`` fixtures
at Stage 25.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from telegram_invite_bot.db.models.base import (
    EconomyBase,
    MessageStatsBase,
    UsersBase,
)
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.models.message_stats import MessageCount
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import (
    assert_unknown_form_hint,
    make_message_update,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


# ``/top`` resolves "today" in :pyattr:`StatsConfig.timezone` (Europe/Moscow
# by default). Seeding rows with the *local* TZ's date silently drifted by
# one day around the UTC midnight boundary — the test failed when the
# runner clock was already on the next UTC date but MSK still on the
# previous one (or vice versa). Pinning the seed clock to the same zone
# the handler uses eliminates the flake.
_HANDLER_TZ = ZoneInfo("Europe/Moscow")


def _today_iso() -> str:
    return datetime.now(_HANDLER_TZ).date().isoformat()


def _update(
    text: str,
    *,
    chat_id: int = -100123,
    chat_type: str = "supergroup",
    language_code: str | None = None,
) -> Update:
    """File-local defaults: supergroup ``-100123``, user 7 named
    ``Eve``. Delegates to the shared builder.
    """
    return make_message_update(
        text,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=7,
        first_name="Eve",
        language_code=language_code,
    )


async def _seed(
    registry: Any,
    *,
    counts: list[tuple[int, int, str, int]],
    users: list[tuple[int, str]] | None = None,
) -> None:
    stats_smaker = registry.session(DBName.MESSAGE_STATS)
    async with stats_smaker() as s:
        s.add_all(
            [MessageCount(chat_id=c, user_id=u, date=d, count=cnt) for c, u, d, cnt in counts]
        )
        await s.commit()
    if users:
        users_smaker = registry.session(DBName.USERS)
        async with users_smaker() as s:
            s.add_all([User(user_id=u, first_name=n) for u, n in users])
            await s.commit()


async def _seed_wallets(
    registry: Any,
    *,
    wallets: list[tuple[int, int]],
    users: list[tuple[int, str]] | None = None,
) -> None:
    """Seed (user_id, balance) into ``economy.users`` and matching
    ``users.users`` rows for name resolution. Two-DB seeding mirrors
    the cross-DB join the balance handler performs at runtime — if
    the seed only touched one DB the test wouldn't exercise the same
    code path as production.
    """
    econ_smaker = registry.session(DBName.ECONOMY)
    async with econ_smaker() as s:
        s.add_all([EconomyUser(user_id=u, balance=b) for u, b in wallets])
        await s.commit()
    if users:
        users_smaker = registry.session(DBName.USERS)
        async with users_smaker() as s:
            s.add_all([User(user_id=u, first_name=n) for u, n in users])
            await s.commit()


async def test_empty_window_renders_friendly_copy(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/top"))
    assert result is not UNHANDLED
    assert "сообщений нет" in sent[0]["text"].lower()


async def test_renders_medals_and_mentions(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    today = _today_iso()
    await _seed(
        registry,
        counts=[
            (-100123, 1, today, 50),
            (-100123, 2, today, 80),
            (-100123, 3, today, 20),
        ],
        users=[(1, "Alice"), (2, "Bob"), (3, "Carol")],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top"))
    body = sent[0]["text"]
    # Highest first → Bob, then Alice, then Carol.
    assert body.find("Bob") < body.find("Alice") < body.find("Carol")
    assert "🥇" in body and "🥈" in body and "🥉" in body
    assert 'href="tg://user?id=2"' in body
    # Bare /top defaults to 30 dn label.
    assert "30 дн." in body


async def test_days_arg_is_honoured(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    today = _today_iso()
    await _seed(
        registry,
        counts=[(-100123, 1, today, 5)],
        users=[(1, "Alice")],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top messages 1"))
    body = sent[0]["text"]
    assert "сегодня" in body  # days==1 → "сегодня" label
    assert "Alice" in body


async def test_missing_user_falls_back_to_default_name(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A user with rows in ``message_counts`` but no ``users.users`` row
    (never hit ``/start``) must still render — legacy returns
    ``ID{uid}``; our port emits the ``Пользователь`` fallback (same
    contract as the bonds leaderboard).
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    today = _today_iso()
    await _seed(
        registry,
        counts=[(-100123, 999, today, 5)],
        users=None,
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top"))
    body = sent[0]["text"]
    assert 'href="tg://user?id=999"' in body
    assert "Пользователь" in body


async def test_messages_board_answers_in_the_user_language(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Every other ``/top`` mode already answers in the caller's
    language; the messages ladder used to hand an English user the
    whole board in Russian — header, row suffix and period label.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    today = _today_iso()
    await _seed(
        registry,
        counts=[(-100123, 1, today, 50)],
        users=[(1, "Alice")],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top", language_code="en"))
    body = sent[0]["text"]
    assert "Top by messages" in body
    assert "50 msg." in body
    assert "30 d." in body
    # Nothing Cyrillic survived the render.
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body), body


async def test_messages_board_translates_the_empty_and_fallback_copy(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The two paths that render copy instead of data — an empty
    window and a user with no ``users.users`` row — are the easiest to
    leave behind when translating the happy path.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top", language_code="en"))
    assert "No messages in the selected period" in sent[0]["text"]

    await _seed(registry, counts=[(-100123, 999, _today_iso(), 5)], users=None)
    sent.clear()
    await dispatcher.feed_update(bot, _update("/top messages 1", language_code="en"))
    body = sent[0]["text"]
    assert "User" in body
    assert "today" in body
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body), body


async def test_badge_decoration_translates_the_fallback_name(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The fallback name is written in two places, not one.

    ``_decorate_names`` substitutes it *before* the renderer runs, so a
    VIP-badged user with no ``users.users`` row takes the badge branch
    and the renderer's own fallback never fires — leaving a second
    Russian literal that the happy-path translation misses entirely.
    """
    from telegram_invite_bot.db.models.economy import UserEmojiBadge

    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    econ_smaker = registry.session(DBName.ECONOMY)
    async with econ_smaker() as s:
        s.add_all(
            [
                EconomyUser(user_id=999, balance=0, vip_till=datetime(2099, 1, 1).timestamp()),
                UserEmojiBadge(user_id=999, emoji="👑", set_at=None),
            ]
        )
        await s.commit()
    # No ``users.users`` row for 999 on purpose — that's the branch.
    await _seed(registry, counts=[(-100123, 999, _today_iso(), 7)], users=None)

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top", language_code="en"))
    body = sent[0]["text"]
    assert "👑 User" in body
    assert not any("Ѐ" <= ch <= "ӿ" for ch in body), body


async def test_html_in_first_name_is_escaped(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    today = _today_iso()
    await _seed(
        registry,
        counts=[(-100123, 7, today, 10)],
        users=[(7, "<b>Pwn</b>")],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top"))
    body = sent[0]["text"]
    assert "&lt;b&gt;Pwn&lt;/b&gt;" in body
    assert '<a href="tg://user?id=7"><b>Pwn</b></a>' not in body


@pytest.mark.parametrize("alias", ["/top", "/kom_top", "/top messages", "/top messages 7"])
async def test_owned_forms_route(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED


@pytest.mark.parametrize(
    "alias",
    [
        # T-016 ported games/wins/streak; only unknown subcommands and
        # malformed args are left unowned. Pinning so a future commit
        # doesn't silently start claiming malformed forms.
        "/top unknown",
        "/top winrate",  # winrate ratio aggregate not yet ported
        "/top messages abc",  # non-int days
        "/top messages 0",  # below clamp
        "/top messages 366",  # above clamp
        "/top messages 7 extra",  # extra arg
        "/top balance 5",  # balance + trailing arg → no board
        "/top games 7",  # mode + trailing arg → no board
    ],
)
async def test_unowned_forms_get_the_unknown_form_hint(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    """None of these render a board; all of them answer (#158).

    The half this test was written for is untouched — a malformed
    ``days``, a trailing token or an unknown aggregate must never be
    silently rounded into a board the user did not ask for. The other
    half, ``assert result is UNHANDLED``, described legacy picking the
    form up. Nothing picks it up now, so the assertion had turned into
    "``/top winrate`` does nothing at all", which for a command whose
    ``/help`` line lists its sub-commands is the least helpful possible
    reply.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update(alias))

    assert result is not UNHANDLED
    assert_unknown_form_hint(sent, command="top")


# ---------------------------------------------------------------------------
# Stage 21 — /top balance
# ---------------------------------------------------------------------------


async def test_top_balance_in_private_renders_ladder_in_order(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``/top balance`` in a private chat → top 10 by ``balance DESC``.
    Three seeded wallets must come back richest-first; the handler
    drives the order off the repo (which has the tiebreaker), so the
    rendering test is asserting on the whole stack end-to-end."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    await _seed_wallets(
        registry,
        wallets=[(1, 100), (2, 500), (3, 250)],
        users=[(1, "Alice"), (2, "Bob"), (3, "Carol")],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top balance", chat_type="private", chat_id=7))
    body = sent[0]["text"]
    # Bob (500) > Carol (250) > Alice (100).
    assert body.find("Bob") < body.find("Carol") < body.find("Alice")
    assert "🥇" in body and "🥈" in body and "🥉" in body
    # format_number adds a thousands separator at >=1000; 500 stays "500".
    assert "500" in body and "250" in body and "100" in body
    # Header from i18n is rendered, not the messages-mode header.
    assert "БОГАЧЕЙ" in body or "RICHEST" in body
    assert "сообщ" not in body  # not the messages template


async def test_top_balance_decorates_vip_badge(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#25: a VIP-active user with an equipped badge shows it prefixed to
    their name on the ladder; a lapsed-VIP badge-holder does NOT.

    Bob is VIP-active with a 👑 badge → his mention carries the emoji.
    Alice has a stored badge but a lapsed grant → her name stays plain.
    """
    from telegram_invite_bot.db.models.economy import UserEmojiBadge

    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    future = datetime(2099, 1, 1).timestamp()
    past = datetime(2000, 1, 1).timestamp()
    econ_smaker = registry.session(DBName.ECONOMY)
    async with econ_smaker() as s:
        s.add_all(
            [
                EconomyUser(user_id=1, balance=100, vip_till=past),
                EconomyUser(user_id=2, balance=500, vip_till=future),
                UserEmojiBadge(user_id=1, emoji="💎", set_at=None),
                UserEmojiBadge(user_id=2, emoji="👑", set_at=None),
            ]
        )
        await s.commit()
    users_smaker = registry.session(DBName.USERS)
    async with users_smaker() as s:
        s.add_all([User(user_id=1, first_name="Alice"), User(user_id=2, first_name="Bob")])
        await s.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top balance", chat_type="private", chat_id=7))
    body = sent[0]["text"]
    # VIP-active Bob carries his badge; lapsed-VIP Alice does not.
    assert "👑 Bob" in body
    assert "💎" not in body


async def test_top_balance_empty_renders_friendly_copy(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No positive-balance wallets → translated empty-state copy.
    Distinguishes the "no data" branch from the silent-drop branch
    (the latter is a regression where the handler awaits nothing)."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top balance", chat_type="private", chat_id=7))
    body = sent[0]["text"]
    # RU empty-state copy. The render path went through the i18n key,
    # not a hard-coded string — checks lang resolution + key lookup.
    assert "Кошельков" in body or "wallets" in body.lower()


async def test_bare_top_in_private_routes_to_balance(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Bare ``/top`` in a private chat → balance ladder, NOT messages.
    Legacy's default (bot.py:18727); Stage 20 used to UNHANDLED here.
    Pinning the new contract: an actual balance card lands in chat."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    await _seed_wallets(
        registry,
        wallets=[(99, 777)],
        users=[(99, "Solo")],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top", chat_type="private", chat_id=99))
    body = sent[0]["text"]
    assert "Solo" in body
    assert "777" in body
    # Definitely NOT the messages-mode header.
    assert "сообщ" not in body


async def test_bare_top_in_group_still_routes_to_messages(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Regression pin: Stage 20's group-side contract MUST survive
    Stage 21. A group user typing ``/top`` (no subcommand) gets the
    messages leaderboard, NOT the balance one — the messages handler
    owns bare ``/top`` in groups and that mustn't drift."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    today = _today_iso()
    await _seed(
        registry,
        counts=[(-100123, 1, today, 5)],
        users=[(1, "Alice")],
    )
    # Also seed a wallet so a routing bug to balance would still
    # produce *some* output — the assertion has to discriminate on
    # the template, not on emptiness.
    await _seed_wallets(registry, wallets=[(1, 999_999)], users=None)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top"))
    body = sent[0]["text"]
    assert "сообщ" in body  # messages template
    assert "999" not in body  # balance NOT rendered


async def test_top_balance_falls_back_to_default_name_for_missing_user(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Wallet exists, ``users.users`` row missing → ``"Пользователь"``
    fallback in the rendered mention. Same cross-DB contract as the
    messages leaderboard."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    await _seed_wallets(
        registry,
        wallets=[(555, 1234)],
        users=None,  # deliberately no users.users row
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top balance", chat_type="private", chat_id=7))
    body = sent[0]["text"]
    assert 'href="tg://user?id=555"' in body
    assert "Пользователь" in body


async def test_top_balance_in_group_is_owned(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Explicit ``/top balance`` typed in a group → balance ladder.
    Legacy supports this (bot.py:18766 has no chat-type guard); the
    strangler keeps parity to avoid a "command worked yesterday in
    the group, doesn't today" regression."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    await _seed_wallets(
        registry,
        wallets=[(1, 42)],
        users=[(1, "Alice")],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/top balance"))
    body = sent[0]["text"]
    assert "Alice" in body
    assert "42" in body
    assert "сообщ" not in body


# ---------------------------------------------------------------------------
# T-016 — /top games | wins | streak
# ---------------------------------------------------------------------------


async def _seed_game_stats(
    registry: Any,
    *,
    rows: list[tuple[int, int, int, int]],
    users: list[tuple[int, str]] | None = None,
) -> None:
    """Seed (user_id, games_played, games_won, daily_streak) into
    ``economy.users`` plus matching ``users.users`` for name resolution.
    """
    econ_smaker = registry.session(DBName.ECONOMY)
    async with econ_smaker() as s:
        s.add_all(
            [
                EconomyUser(
                    user_id=u,
                    balance=0,
                    games_played=gp,
                    games_won=gw,
                    daily_streak=ds,
                )
                for u, gp, gw, ds in rows
            ]
        )
        await s.commit()
    if users:
        users_smaker = registry.session(DBName.USERS)
        async with users_smaker() as s:
            s.add_all([User(user_id=u, first_name=n) for u, n in users])
            await s.commit()


@pytest.mark.parametrize(
    "mode,column_idx,marker",
    [
        ("games", 0, "игр"),
        ("wins", 1, "побед"),
        # #207: the streak row used to read a fixed "дн."; the noun is
        # now declined, and every seeded streak here (30/12/5) takes the
        # genitive-plural form.
        ("streak", 2, "дней"),
    ],
)
async def test_top_mode_renders_ladder_in_order(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mode: str,
    column_idx: int,
    marker: str,
) -> None:
    """``/top {mode}`` returns rows DESC by the corresponding column.

    The seed assigns distinct values per user across each column so the
    expected order is mode-specific — verifies the handler reads the
    right repo method (not the same one three times).
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    # Per-column distinct ordering:
    #   games_played: Carol(50) > Alice(20) > Bob(10)
    #   games_won:    Alice(15) > Bob(8)    > Carol(3)
    #   daily_streak: Bob(30)   > Carol(12) > Alice(5)
    await _seed_game_stats(
        registry,
        rows=[
            # (user_id, games_played, games_won, daily_streak)
            (1, 20, 15, 5),  # Alice
            (2, 10, 8, 30),  # Bob
            (3, 50, 3, 12),  # Carol
        ],
        users=[(1, "Alice"), (2, "Bob"), (3, "Carol")],
    )
    expected_order = {
        "games": ("Carol", "Alice", "Bob"),
        "wins": ("Alice", "Bob", "Carol"),
        "streak": ("Bob", "Carol", "Alice"),
    }[mode]
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update(f"/top {mode}", chat_type="private", chat_id=7))
    body = sent[0]["text"]
    positions = [body.find(name) for name in expected_order]
    assert positions == sorted(positions), f"unexpected order: {positions} ({body!r})"
    assert "🥇" in body and "🥈" in body and "🥉" in body
    assert marker in body  # row template marker for this mode


@pytest.mark.parametrize(
    ("mode", "played", "won", "streak", "expected"),
    [
        ("games", 1, 0, 0, "1 игра"),
        ("games", 2, 0, 0, "2 игры"),
        ("games", 11, 0, 0, "11 игр"),
        ("wins", 1, 1, 0, "1 победа"),
        ("wins", 3, 3, 0, "3 победы"),
        ("streak", 1, 0, 1, "1 день"),
        ("streak", 1, 0, 22, "22 дня"),
    ],
)
async def test_top_mode_declines_the_row_noun(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mode: str,
    played: int,
    won: int,
    streak: int,
    expected: str,
) -> None:
    """#207: the ladder's noun agrees with the number beside it.

    Row 1 is the most-read line on the screen, and a first-time player
    is exactly the user whose count is 1 — so the frozen form ("1 игр")
    was wrong precisely where it was most visible. One case per Russian
    form, per mode that has its own noun family.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    await _seed_game_stats(
        registry,
        rows=[(1, played, won, streak)],
        users=[(1, "Alice")],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update(f"/top {mode}", chat_type="private", chat_id=7))
    assert expected in sent[0]["text"]


@pytest.mark.parametrize("mode", ["games", "wins", "streak"])
async def test_top_mode_empty_renders_friendly_copy(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mode: str,
) -> None:
    """No qualifying rows → translated empty-state copy, not silence."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update(f"/top {mode}", chat_type="private", chat_id=7))
    body = sent[0]["text"]
    # All three empty messages contain "📊" plus mode-specific copy
    # ("игр" / "Побед" / "Активных") — assert on the discriminating
    # token to prove the right key was fetched.
    expected_token = {"games": "игр", "wins": "Побед", "streak": "Активных"}[mode]
    assert expected_token in body


@pytest.mark.parametrize("mode", ["games", "wins", "streak"])
async def test_top_mode_falls_back_to_default_name(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    mode: str,
) -> None:
    """Wallet row exists, ``users.users`` row missing → ``"Пользователь"``
    fallback. Same cross-DB contract as balance/messages modes."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    await _seed_game_stats(
        registry,
        rows=[(555, 7, 7, 7)],
        users=None,
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update(f"/top {mode}", chat_type="private", chat_id=7))
    body = sent[0]["text"]
    assert 'href="tg://user?id=555"' in body
    assert "Пользователь" in body


@pytest.mark.parametrize("alias", ["/top games", "/top wins", "/top streak"])
async def test_top_modes_owned(
    make_wired: WiredFactory,
    alias: str,
) -> None:
    """T-016 regression pin: the three new modes must NOT fall through."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, MessageStatsBase, EconomyBase])
    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED
