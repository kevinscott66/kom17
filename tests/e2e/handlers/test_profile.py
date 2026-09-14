"""End-to-end ``/profile`` flow: dispatcher → SessionMiddleware → DB.

Migrated to the shared ``make_wired`` / ``capture_outgoing`` fixtures
at Stage 24 — see conftest.py for the rationale. The previous inline
fixture built Settings, registry, schema, middleware and Bot+Dispatcher
in one ~30-line block per test file; that's now a one-liner call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChatMember
from aiogram.types import ChatMemberOwner, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import select

from telegram_invite_bot.config.settings import StatsConfig
from telegram_invite_bot.db import Checkpoint
from telegram_invite_bot.db.models.base import (
    EconomyBase,
    MessageStatsBase,
    ModerationBase,
    UsersBase,
)
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction, UserEmojiBadge
from telegram_invite_bot.db.models.message_stats import MessageCount
from telegram_invite_bot.db.models.moderation import ModerationLog
from telegram_invite_bot.db.models.user_settings import UserSetting
from telegram_invite_bot.db.models.users import Marriage, Relationship, UserGroupJoin
from telegram_invite_bot.db.models.users import User as DBUser
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.keyboards.builders import ProfilePanel, ProfileRefresh
from telegram_invite_bot.services.emoji_badge_service import VIP_BADGE_SET
from telegram_invite_bot.utils.numbers import format_number
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update_for(text: str, *, chat_type: str = "private", user_id: int = 7777) -> Update:
    """File-local defaults: user 7777 ``Иван Петров`` (ru, premium,
    ``@ivanp``). Delegates to the shared builder.
    """
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Иван",
        last_name="Петров",
        username="ivanp",
        language_code="ru",
        is_premium=True,
    )


async def test_profile_renders_user_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/profile"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Иван Петров" in body
    assert "<code>7777</code>" in body
    assert "@ivanp" in body
    assert "Профиль" in body  # ru title


async def test_profile_commits_the_touch_before_the_card_is_built(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/profile`` must not hold the users.db write lock while it prices
    coins.

    The card's RUB line asks ``CurrencyService`` for a live quote, and
    that call goes out to the FX provider with a 10 s timeout on a cold
    cache — twice SQLite's ``busy_timeout``. Since the update's write
    transaction opens at the ``touch`` a few lines earlier and the
    middleware only commits it at the end, every other user's write to
    users.db would sit behind that quote and fail with ``database is
    locked``. The handler ends the transaction first
    (:class:`db.session.Checkpoint`).

    The spy asserts the real property rather than the call: by the time
    the checkpoint returns, a *separate* session already sees the
    touched row, so the lock is gone.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    visible_to_others: list[int | None] = []
    commit = Checkpoint.__call__

    async def spy(self: Checkpoint) -> None:
        await commit(self)
        async with registry.session(DBName.USERS)() as probe:
            row = await probe.execute(select(DBUser.user_id).where(DBUser.user_id == 7777))
            visible_to_others.append(row.scalar_one_or_none())

    monkeypatch.setattr(Checkpoint, "__call__", spy)

    await dispatcher.feed_update(bot, _update_for("/profile"))

    assert visible_to_others == [7777], "the touch was still uncommitted"
    assert "Профиль" in sent[0]["text"]


async def test_profile_alias_info_works(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/info"))
    assert result is not UNHANDLED
    assert "Профиль" in sent[0]["text"]


async def test_profile_hub_finances_panel(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#1/#2: tapping 💰 Finances on the private hub edits the card to the
    balance + cashflow + recent-rows panel."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sm = registry.session(DBName.ECONOMY)
    async with sm() as s:
        s.add(EconomyUser(user_id=7777, balance=1500, language="ru"))
        s.add(
            Transaction(
                to_id=7777,
                amount=200,
                type="daily",
                reason="daily",
                date=datetime(2026, 6, 1),
            )
        )
        s.add(
            Transaction(
                from_id=7777,
                amount=50,
                type="shop",
                reason="hat",
                date=datetime(2026, 6, 2),
            )
        )
        await s.commit()
    sent = capture_callback_outgoing(bot)

    result = await dispatcher.feed_update(
        bot,
        make_callback_update(
            ProfilePanel(panel="fin", user_id=7777).pack(),
            user_id=7777,
            language_code="ru",
        ),
    )
    assert result is not UNHANDLED
    edits = [e for e in sent if e["kind"] == "edit"]
    assert len(edits) == 1
    body = edits[0]["text"]
    assert "Финансы" in body
    assert format_number(1500) in body  # balance, thin-space separated
    assert "200" in body and "50" in body  # the two recent rows


async def test_profile_hub_social_panel(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """RR-1 #2: 👥 Social folds the four legacy panels (referrals,
    commission, relations, marriage) into one DM card — and the bonds
    come from *other* chats, which legacy could never show here."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sm = registry.session(DBName.ECONOMY)
    async with sm() as s:
        s.add(EconomyUser(user_id=7777, balance=10, language="ru", referred_by=111))
        s.add(EconomyUser(user_id=111, balance=0, language="ru"))
        # Two direct invitees, one of whom brought a third (ring 2).
        s.add(EconomyUser(user_id=8001, balance=5, language="ru", referred_by=7777))
        s.add(EconomyUser(user_id=8002, balance=7, language="ru", referred_by=7777))
        s.add(EconomyUser(user_id=8003, balance=1, language="ru", referred_by=8001))
        s.add(
            Transaction(
                to_id=7777,
                amount=42,
                type="referral",
                reason="commission",
                date=datetime(2026, 6, 1),
            )
        )
        await s.commit()
    users_sm = registry.session(DBName.USERS)
    async with users_sm() as s:
        s.add(DBUser(user_id=111, first_name="Пригласитель"))
        s.add(DBUser(user_id=9001, first_name="Жена"))
        s.add(
            Marriage(
                chat_id=-100500,
                user1_id=7777,
                user2_id=9001,
                created_at=datetime(2024, 5, 4),
                experience=250,
                status="active",
            )
        )
        await s.commit()
    sent = capture_callback_outgoing(bot)

    result = await dispatcher.feed_update(
        bot,
        make_callback_update(
            ProfilePanel(panel="soc", user_id=7777).pack(),
            user_id=7777,
            language_code="ru",
        ),
    )
    assert result is not UNHANDLED
    edits = [e for e in sent if e["kind"] == "edit"]
    assert len(edits) == 1
    body = edits[0]["text"]
    assert "Твои связи" in body
    assert "Пригласитель" in body  # inviter resolved to a name, not ID111
    assert "<b>2</b>" in body  # two direct invitees
    assert "<b>1</b>" in body  # one second-level
    assert "<b>42</b>" in body  # lifetime commission
    assert "Жена" in body  # the cross-chat marriage partner
    assert "2024-05-04" in body


async def test_profile_hub_social_panel_empty_state(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A user with no referrals and no bonds gets encouragement, not a
    wall of zeroes — and never a raw ``None``."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase], session_middleware=True)
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            ProfilePanel(panel="soc", user_id=7777).pack(),
            user_id=7777,
            language_code="ru",
        ),
    )
    body = next(e for e in sent if e["kind"] == "edit")["text"]
    assert "None" not in body
    assert "/referral" in body  # CTA for a user with nobody invited
    assert "/marry" in body  # CTA for a user with no bonds
    # The "second level" line is skipped entirely rather than showing 0.
    assert "2-го уровня" not in body


async def test_profile_hub_social_panel_counts_hidden_bonds_exactly(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The "+N more" footer has to report the real remainder. Fetching one
    row past the display cap would have pinned it at 1 forever."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    users_sm = registry.session(DBName.USERS)
    async with users_sm() as s:
        for i in range(9):  # 9 marriages, 5 shown ⇒ 4 hidden
            s.add(
                Marriage(
                    chat_id=-100 - i,
                    user1_id=7777,
                    user2_id=9100 + i,
                    created_at=datetime(2024, 5, 4),
                    experience=100 + i,
                    status="active",
                )
            )
        await s.commit()
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            ProfilePanel(panel="soc", user_id=7777).pack(),
            user_id=7777,
            language_code="ru",
        ),
    )
    body = next(e for e in sent if e["kind"] == "edit")["text"]
    assert "и ещё <b>4</b>" in body
    assert body.count("💍") == 5


async def test_profile_hub_social_panel_escapes_partner_name(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A partner's ``first_name`` is attacker-controlled. Unescaped, a
    ``<a href=...>`` in it would be honoured as real HTML inside the DM
    card — and a stray ``<`` would fail the whole edit, not one line."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    users_sm = registry.session(DBName.USERS)
    async with users_sm() as s:
        s.add(DBUser(user_id=9002, first_name='<a href="evil">Pwned'))
        s.add(
            Relationship(
                chat_id=-100777,
                user1_id=7777,
                user2_id=9002,
                created_at=datetime(2025, 1, 2),
                experience=1200,
                last_activity_at=datetime(2025, 1, 2),
                status="active",
            )
        )
        await s.commit()
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            ProfilePanel(panel="soc", user_id=7777).pack(),
            user_id=7777,
            language_code="ru",
        ),
    )
    body = next(e for e in sent if e["kind"] == "edit")["text"]
    assert '<a href="evil">' not in body
    assert "&lt;a href=&quot;evil&quot;&gt;Pwned" in body


async def test_profile_hub_social_panel_foreign_tap_refused(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The social panel carries the owner's referral earnings and their
    partners — the owner guard has to cover it too, not just finances."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    users_sm = registry.session(DBName.USERS)
    async with users_sm() as s:
        s.add(DBUser(user_id=9001, first_name="Жена"))
        s.add(
            Marriage(
                chat_id=-100500,
                user1_id=7777,
                user2_id=9001,
                created_at=datetime(2024, 5, 4),
                experience=250,
                status="active",
            )
        )
        await s.commit()
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            ProfilePanel(panel="soc", user_id=7777).pack(),
            user_id=4242,
            language_code="ru",
        ),
    )
    assert not any(e["kind"] == "edit" for e in sent)


async def test_profile_hub_panel_foreign_tap_refused(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A bystander tapping someone else's hub button is refused — no edit
    leaks the owner's finances."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase, EconomyBase], session_middleware=True)
    sent = capture_callback_outgoing(bot)

    await dispatcher.feed_update(
        bot,
        make_callback_update(
            ProfilePanel(panel="fin", user_id=7777).pack(),  # owner 7777…
            user_id=4242,  # …tapped by 4242
            language_code="ru",
        ),
    )
    assert not any(e["kind"] == "edit" for e in sent)


async def test_profile_in_group_renders_live_preview(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """T-024.4: group ``/profile`` now renders the live PNG stats card.

    The legacy monolith showed a freshly-generated activity chart in
    groups; the port restores it as a ``send_photo`` with an
    identity+balance caption.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert sent[0]["kind"] == "photo"
    caption = sent[0]["caption"]
    assert "Иван Петров" in caption
    assert "<code>7777</code>" in caption
    assert "Баланс" in caption  # ru balance label


async def test_profile_other_unknown_user(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#4: ``/profile @nobody`` for an unseen user → a friendly not-found
    notice (no longer falls through to legacy)."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/profile @someone"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    assert "@юзернейм" in sent[0]["text"] or "Не нашёл" in sent[0]["text"]


async def test_profile_other_renders_public_card(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#4: ``/profile <id>`` for a seen user renders their public card —
    game record + achievements + rank, and crucially NOT their wallet."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    # Seed the target's users row (so it resolves) + an economy row with a
    # balance that must NOT leak onto the public card.
    async with registry.session(DBName.USERS)() as s:
        from telegram_invite_bot.db.models.users import User as UserRow

        s.add(UserRow(user_id=4242, first_name="Цель", username="target"))
        await s.commit()
    async with registry.session(DBName.ECONOMY)() as s:
        s.add(
            EconomyUser(
                user_id=4242,
                balance=999999,
                games_played=10,
                games_won=7,
                language="ru",
            )
        )
        await s.commit()
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/profile 4242"))
    assert result is not UNHANDLED
    assert len(sent) == 1
    body = sent[0]["text"]
    assert "Цель" in body
    assert "<code>4242</code>" in body
    assert "10" in body and "7" in body  # games played / won
    assert "70%" in body  # win-rate
    # Privacy: the target's balance must never appear on a cross-user card.
    assert "999" not in body


@pytest.mark.parametrize("alias", ["/whoami", "/me", "/kom_whoami"])
async def test_whoami_aliases_route_to_profile(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    """Stage 22: legacy ``/whoami`` / ``/me`` / ``/kom_whoami`` at bot.py:41258
    are pure self-profile aliases. Folded into the same handler so users
    landing on any of them see the new card.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for(alias))
    assert result is not UNHANDLED
    assert "Профиль" in sent[0]["text"]
    assert "Иван Петров" in sent[0]["text"]


@pytest.mark.parametrize("alias", ["/whoami", "/me", "/kom_whoami"])
async def test_whoami_aliases_in_group_render_preview(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    """Group ``/whoami`` shares the ``/profile`` preview path (same
    self-profile handler), so it renders the live PNG card too.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for(alias, chat_type="group"))
    assert result is not UNHANDLED
    assert sent[0]["kind"] == "photo"
    assert "Иван Петров" in sent[0]["caption"]


async def test_profile_group_shows_vip_emoji_badge(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#25 display completion: a VIP with an equipped badge sees it lead
    their name on the group ``/profile`` caption. Needs the economy
    schema (badge + vip_till both live in economy.db).
    """
    badge = VIP_BADGE_SET[0]
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    # Seed: future vip_till (VIP gate) + an equipped badge row, user 7777.
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        session.add(
            EconomyUser(
                user_id=7777,
                balance=0,
                language="ru",
                vip_till=datetime(2099, 1, 1).timestamp(),
            )
        )
        session.add(UserEmojiBadge(user_id=7777, emoji=badge))
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert f"{badge} <b>Иван Петров</b>" in caption


async def test_profile_private_card_renders_without_economy_schema(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#25 regression: the private text card opens an economy session for
    the badge read, but a users-only deployment (no economy schema) must
    still render — the badge read degrades to "no badge", never 500s.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/profile"))
    assert result is not UNHANDLED
    body = sent[0]["text"]
    assert "<b>Иван Петров</b>" in body
    # No badge prefix when the economy schema is absent.
    assert "Профиль" in body


async def test_profile_escapes_html_in_name(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A malicious first_name must not break out of the HTML envelope."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    update = Update.model_validate(
        {
            "update_id": 2,
            "message": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": 9999, "type": "private"},
                "from": {
                    "id": 9999,
                    "is_bot": False,
                    "first_name": "<script>alert(1)</script>",
                    "language_code": "ru",
                },
                "text": "/profile",
            },
        }
    )
    await dispatcher.feed_update(bot, update)
    body = sent[0]["text"]
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


async def test_profile_shows_timezone_when_set(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Stage 29: after ``/timezone Europe/Berlin``, the profile card
    should surface the stored value next to the language row. Guards
    against UserService.touch silently dropping the new field.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, _update_for("/timezone Europe/Berlin"))
    sent.clear()
    result = await dispatcher.feed_update(bot, _update_for("/profile"))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "Часовой пояс" in body
    assert "Europe/Berlin" in body


async def test_profile_timezone_placeholder_when_unset(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Users with no stored tz must still see a stable card shape —
    the placeholder (``—``) keeps the row layout consistent so renderers
    that screenshot the card don't break on optional rows.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update_for("/profile"))
    body = sent[-1]["text"]
    assert "Часовой пояс" in body
    # Either the literal — placeholder OR the row simply present
    # with the dash — assert on the row marker and the dash co-located.
    assert "Часовой пояс: —" in body


async def test_profile_refresh_owner_edits_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """T-024.4: the card owner tapping "🔄 Обновить" re-renders the PNG
    via ``edit_media`` and gets a confirmation toast.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    sink = capture_callback_outgoing(bot)

    update = make_callback_update(ProfileRefresh(user_id=7777).pack(), user_id=7777)
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED
    kinds = [e["kind"] for e in sink]
    assert "edit_media" in kinds
    assert "callback_answer" in kinds
    answer = next(e for e in sink if e["kind"] == "callback_answer")
    assert answer["text"] == "Обновлено ✅"


async def test_profile_refresh_foreign_tap_rejected(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A bystander tapping someone else's card gets a refusal toast and
    no card edit — the owner's fresh stats never leak.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    sink = capture_callback_outgoing(bot)

    # Card owner is 7777; tapper is 9999.
    update = make_callback_update(
        ProfileRefresh(user_id=7777).pack(), user_id=9999, language_code="ru"
    )
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED
    kinds = [e["kind"] for e in sink]
    assert "edit_media" not in kinds
    answer = next(e for e in sink if e["kind"] == "callback_answer")
    assert "не ваша карточка" in answer["text"]


# --- RR-1 #3: since-join counter, VIP line, moderation history ------------

# ``_update_for`` builds a group chat whose id mirrors the user id.
_GROUP_ID = 7777


async def _seed_counts(registry: Any, rows: list[tuple[str, int]]) -> None:
    """Seed ``message_counts`` for user 7777 in the test group."""
    sessionmaker = registry.session(DBName.MESSAGE_STATS)
    async with sessionmaker() as session:
        for day, count in rows:
            session.add(MessageCount(user_id=7777, chat_id=_GROUP_ID, date=day, count=count))
        await session.commit()


async def test_profile_group_counts_messages_from_the_recorded_join(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The since-join counter starts at the join record, not at the first
    counted message — otherwise it just restates the lifetime total.

    Seeded so the two genuinely differ: 40 messages predate the join
    (the user was in the chat under a previous membership), 5 follow it.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    await _seed_counts(registry, [("2024-01-10", 40), ("2024-06-01", 5)])
    async with registry.session(DBName.USERS)() as session:
        session.add(
            UserGroupJoin(
                user_id=7777,
                chat_id=_GROUP_ID,
                joined_at=datetime(2024, 5, 20, 12, 0),
                source="join_event",
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert "В группе с: <b>20.05.2024</b>" in caption
    assert f"Всего: <code>{format_number(45)}</code>" in caption
    assert "С момента входа: <code>5</code>" in caption


async def test_profile_group_dates_the_join_in_the_display_timezone(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A join stored at 23:30 UTC already happened *tomorrow* in Moscow.

    The stored instant is naive UTC; message counts are bucketed by
    calendar day in ``STATS_TIMEZONE``. Reading ``.date()` straight off
    the UTC value would print a date one day before the one the rest of
    the card speaks, and would count a day of pre-membership messages
    into the since-join total.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
        stats_config=StatsConfig(STATS_PERIOD_DAYS=7, STATS_TIMEZONE="Europe/Moscow"),
    )
    # 20.05 is the UTC day of the join; in Moscow the join is on 21.05,
    # so only the 22.05 messages are "since joining".
    await _seed_counts(registry, [("2024-05-20", 9), ("2024-05-22", 4)])
    async with registry.session(DBName.USERS)() as session:
        session.add(
            UserGroupJoin(
                user_id=7777,
                chat_id=_GROUP_ID,
                joined_at=datetime(2024, 5, 20, 23, 30),
                source="join_event",
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert "В группе с: <b>21.05.2024</b>" in caption
    assert "С момента входа: <code>4</code>" in caption


async def test_profile_group_reports_an_unknown_join_date_as_unknown(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """No join record → the date falls back to first activity, and the
    counter says ``—``.

    Printing the lifetime total there instead would be a silent lie: we
    do not know when to start counting from, and ``0`` would be a claim.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    await _seed_counts(registry, [("2024-03-02", 11)])

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert "В группе с: <b>02.03.2024</b>" in caption
    assert "С момента входа: <code>—</code>" in caption


async def test_profile_group_shows_the_vip_expiry_line(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A live VIP grant surfaces on the group card, as it already did on
    the DM card — the group caption simply never asked for it.

    Seeded from an explicitly tz-aware instant: ``vip_till`` is an epoch,
    the card renders it in ``STATS_TIMEZONE`` (UTC here), and a naive
    ``datetime(...).timestamp()`` would mean "midnight wherever the test
    machine happens to sit" — which lands on the previous day for any
    developer east of Greenwich.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    async with registry.session(DBName.ECONOMY)() as session:
        session.add(
            EconomyUser(
                user_id=7777,
                balance=0,
                language="ru",
                vip_till=datetime(2099, 1, 1, 12, 0, tzinfo=UTC).timestamp(),
            )
        )
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    assert "👑 VIP: до <b>01.01.2099</b>" in sent[0]["caption"]


async def test_profile_group_lists_the_recent_moderation_actions(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The 📋 block shows the newest three in-window entries, escaped.

    Seeded with four rows: one outside the 90-day window (must not
    appear), and one whose reason carries markup and exceeds the 50-char
    cap (must be truncated *and* escaped — an unescaped ``<`` would make
    Telegram reject the whole caption).
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase, ModerationBase],
        session_middleware=True,
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    async with registry.session(DBName.MODERATION)() as session:
        session.add_all(
            [
                ModerationLog(
                    action="ban",
                    user_id=7777,
                    admin_id=1,
                    chat_id=_GROUP_ID,
                    reason="<b>spam</b> " + "x" * 60,
                    date=now - timedelta(days=1),
                ),
                ModerationLog(
                    action="mute",
                    user_id=7777,
                    admin_id=1,
                    chat_id=_GROUP_ID,
                    reason="flood",
                    date=now - timedelta(days=2),
                ),
                ModerationLog(
                    action="warn",
                    user_id=7777,
                    admin_id=1,
                    chat_id=_GROUP_ID,
                    reason="rudeness",
                    date=now - timedelta(days=3),
                ),
                ModerationLog(
                    action="kick",
                    user_id=7777,
                    admin_id=1,
                    chat_id=_GROUP_ID,
                    reason="ancient history",
                    date=now - timedelta(days=200),
                ),
            ]
        )
        await session.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert "Последние действия модерации" in caption
    assert "• бан (" in caption
    assert "• мут (" in caption
    assert "• предупреждение (" in caption
    # Outside the 90-day window — invisible.
    assert "ancient history" not in caption
    # Reason truncated to 50 chars, and the markup neutralised.
    assert "&lt;b&gt;spam&lt;/b&gt; " + "x" * 38 in caption
    assert "<b>spam</b>" not in caption


async def test_profile_group_hides_moderation_history_from_the_creator(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat owner sees neither the warnings line nor the history —
    same gate legacy applied, extended to the block RR-1 #3 restores."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase, ModerationBase],
        session_middleware=True,
    )
    async with registry.session(DBName.MODERATION)() as session:
        session.add(
            ModerationLog(
                action="warn",
                user_id=7777,
                admin_id=1,
                chat_id=_GROUP_ID,
                reason="rudeness",
                date=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await session.commit()

    async def _creator(_chat_id: int, _user_id: int) -> Any:
        return ChatMemberOwner(
            user=TelegramUser(id=7777, is_bot=False, first_name="Иван"),
            is_anonymous=False,
        )

    monkeypatch.setattr(bot, "get_chat_member", _creator)

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert "Последние действия модерации" not in caption
    assert "Предупреждений" not in caption
    # The creator is still an admin of their own chat — that line stays.
    assert "Админ этой группы" in caption


async def test_profile_group_hides_moderation_history_when_telegram_is_unreachable(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The creator gate fails *closed*.

    ``get_chat_member`` is the only thing that can tell us whether the
    viewer owns the chat, and it is a network call. If it errors we do
    not know — and treating "don't know" as "not the creator" would
    publish the owner's own warnings and moderation history into their
    group, which is the exact outcome the gate exists to prevent. The
    cost of failing closed is a missing block on a refreshable card.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase, ModerationBase],
        session_middleware=True,
    )
    async with registry.session(DBName.MODERATION)() as session:
        session.add(
            ModerationLog(
                action="warn",
                user_id=7777,
                admin_id=1,
                chat_id=_GROUP_ID,
                reason="rudeness",
                date=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await session.commit()

    async def _unreachable(_chat_id: int, _user_id: int) -> Any:
        raise TelegramNetworkError(method=GetChatMember(chat_id=1, user_id=1), message="down")

    monkeypatch.setattr(bot, "get_chat_member", _unreachable)

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert "Последние действия модерации" not in caption
    assert "Предупреждений" not in caption
    # The rest of the card is unaffected — this degrades one block, not
    # the whole profile.
    assert "Иван" in caption


async def test_profile_group_shows_the_saved_city(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Regression (#481): the city line reads ``user_settings.city``.

    The port hardcoded ``<b>—</b>`` here, so ``/city`` wrote a value no
    surface ever showed and the "set your city" hint was permanent.
    Legacy printed the saved value and suppressed the hint once it was
    set (bot.py:39828-39832).
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    async with registry.session(DBName.USERS)() as s:
        s.add(DBUser(user_id=7777, first_name="Иван"))
        # ``user_settings.user_id`` is a real FK and SQLite enforces it
        # here, so the parent row must land in its own transaction.
        await s.commit()
        s.add(UserSetting(user_id=7777, city="Казань"))
        await s.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert "Город: <b>Казань</b>" in caption
    assert "можно указать командой /city" not in caption


async def test_profile_group_keeps_the_city_hint_while_unset(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The empty case is unchanged by #481: dash plus the ``/city`` hint."""
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert "Город: <b>—</b>" in caption
    assert "можно указать командой /city" in caption


async def test_profile_group_escapes_a_city_containing_html(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """``city`` is free-form user input typed into ``/city``.

    It lands inside a ``<b>`` in an HTML-parse-mode caption, so an
    unescaped ``<`` would either break the card or inject a link.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    async with registry.session(DBName.USERS)() as s:
        s.add(DBUser(user_id=7777, first_name="Иван"))
        await s.commit()
        s.add(UserSetting(user_id=7777, city='<a href="evil">Pwned</a>'))
        await s.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert '<a href="evil">' not in caption
    assert "&lt;a href=&quot;evil&quot;&gt;Pwned" in caption


async def test_profile_group_dates_the_registration_in_the_display_timezone(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Regression (#478): ``В боте с`` uses the card's display timezone.

    The group-join line right below it is already converted, so leaving
    the registration stamp in raw UTC made one card print two different
    calendar days for two instants half an hour apart.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
        stats_config=StatsConfig(STATS_PERIOD_DAYS=7, STATS_TIMEZONE="Europe/Moscow"),
    )
    async with registry.session(DBName.USERS)() as s:
        s.add(DBUser(user_id=7777, first_name="Иван", joined_date=datetime(2024, 5, 20, 23, 30)))
        await s.commit()

    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    caption = sent[0]["caption"]
    assert "В боте с: <b>2024-05-21 02:30</b>" in caption


async def test_profile_group_falls_back_to_text_when_the_png_fails(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Pillow failure must cost the picture, not the profile.

    ``/stats`` and ``/chatstats`` have degraded to text since they were
    ported (``handlers/stats.py``, ``handlers/chatstats.py``); the group
    profile card raised through the handler instead, so a truncated font
    or an unencodable glyph turned ``/profile`` into silence in a chat
    where the caption already carries every number on the card.
    """
    from telegram_invite_bot.handlers import profile as profile_module

    def _boom(*_args: Any, **_kwargs: Any) -> bytes:
        raise OSError("no font")

    monkeypatch.setattr(profile_module, "render_stats_card", _boom)

    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update_for("/profile", chat_type="group"))
    assert result is not UNHANDLED
    assert [e["kind"] for e in sent] == ["text"]
    assert "Иван Петров" in sent[0]["text"]
    assert "<code>7777</code>" in sent[0]["text"]
    assert "Баланс" in sent[0]["text"]


async def test_profile_refresh_updates_the_caption_when_the_png_fails(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refresh tap degrades differently, and has to.

    The card on screen is a photo message and Telegram cannot turn one
    into text, so there is no "answer as text" here. What the tap asked
    for — fresh numbers — still arrives: the caption is edited in place
    over the stale picture, and the toast is the usual one.
    """
    from telegram_invite_bot.handlers import profile as profile_module

    def _boom(*_args: Any, **_kwargs: Any) -> bytes:
        raise OSError("no font")

    monkeypatch.setattr(profile_module, "render_stats_card", _boom)

    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase, EconomyBase, MessageStatsBase],
        session_middleware=True,
    )
    sink = capture_callback_outgoing(bot)

    update = make_callback_update(ProfileRefresh(user_id=7777).pack(), user_id=7777)
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED
    kinds = [e["kind"] for e in sink]
    assert "edit_media" not in kinds
    assert "edit_caption" in kinds
    edited = next(e for e in sink if e["kind"] == "edit_caption")
    assert "<code>7777</code>" in edited["caption"]
    assert "Баланс" in edited["caption"]
    answer = next(e for e in sink if e["kind"] == "callback_answer")
    assert answer["text"] == "Обновлено ✅"
