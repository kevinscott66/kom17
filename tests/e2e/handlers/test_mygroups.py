"""End-to-end ``/mygroups`` (L-58 read-side panel).

Pins:

* Private + caller has no groups → empty-state card.
* Private + caller added N groups → those exact N rows render with
  ids + titles; other users' groups never leak (``added_by_user_id``
  is the scope-defining column).
* Pagination — >10 groups: page 1 shows exactly 10 rows; the
  :class:`MyGroupsPage` callback edits the message to page 2 with the
  remaining rows.
* Card — :class:`MyGroupsCard` edits to the per-group summary
  (id, donations XP, rating position) when the tapping user owns the
  attribution.
* Card authorisation — a forged card callback for someone ELSE's
  group answers with the not-found alert and edits nothing.
* HTML escaping on title.
* Russian alias (``/мои_группы``) matches.
* ``/admin`` matches — legacy's ``cmd_admin`` answered a non-developer
  with exactly this list, and the word now points back here instead of
  at the developer panel (``handlers/admin/panel.py``, ``/admin_panel``).
* Group invocation → router-level private filter rejects (UNHANDLED).

Keyboard composition (button labels / nav arrows) is pinned directly
against the builder in ``keyboards/builders/mygroups.py`` — the
outgoing-capture fixtures record only text payloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import (
    EconomyBase,
    MessageStatsBase,
    ModerationBase,
    UsersBase,
)
from telegram_invite_bot.db.models.economy import GroupDonationsAggregate
from telegram_invite_bot.db.models.users import BotGroup
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.mygroups import (
    MyGroupsCard,
    MyGroupsPage,
    build_list_markup,
)
from tests.e2e.handlers.conftest import (
    assert_chat_scope_refusal,
    make_callback_update,
    make_message_update,
)

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

# Every schema the card callback touches. The list path needs only
# UsersBase; tests that exercise the card create the full set so the
# cross-DB reads (donations / mod config / activity) hit real tables.
_ALL_SCHEMAS = [UsersBase, EconomyBase, ModerationBase, MessageStatsBase]


async def _seed(
    registry: EngineRegistry,
    rows: list[tuple[int, str | None, int]],
    *,
    is_active: int = 1,
) -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        for chat_id, title, added_by in rows:
            session.add(
                BotGroup(
                    chat_id=chat_id,
                    chat_title=title,
                    added_by_user_id=added_by,
                    is_active=is_active,
                )
            )
        await session.commit()


async def _seed_donations(
    registry: EngineRegistry, *, group_id: int, xp: int, total: int, position: int | None
) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        session.add(
            GroupDonationsAggregate(
                group_id=group_id,
                group_name="seeded",
                total_donations=total,
                group_xp=xp,
                rating_position=position,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_empty_state(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/mygroups", user_id=42, chat_type="private")
    )
    assert sent[0]["text"] == t("h_mygroups_empty", "ru")


@pytest.mark.asyncio
async def test_lists_callers_groups(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    await _seed(
        registry,
        [
            (-1001, "Alpha", 42),
            (-1002, "Beta", 42),
            (-1003, "Gamma — someone else's", 99),  # different added_by
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/mygroups", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Alpha" in text
    assert "Beta" in text
    assert "-1001" in text
    assert "-1002" in text
    # Other users' groups must NOT leak — the whole point of the
    # added_by scoping is that one user can't enumerate another's
    # group footprint via this command.
    assert "Gamma" not in text
    assert "-1003" not in text


@pytest.mark.asyncio
async def test_first_page_shows_ten_rows(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    await _seed(registry, [(-1000 - i, f"G{i}", 7) for i in range(25)])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/mygroups", user_id=7, chat_type="private")
    )
    text = sent[0]["text"]
    # Exactly PAGE_SIZE "• <code>" markers on page 1.
    assert text.count("• <code>") == 10
    assert t("h_mygroups_total", "ru", count=25) in text


@pytest.mark.asyncio
async def test_page_nav_edits_to_next_page(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    # chat_id ascending order ⇒ page 1 is -1024..-1015, page 2 starts
    # at -1014 (rows are ordered by chat_id, not seed order).
    await _seed(registry, [(-1000 - i, f"G{i}", 7) for i in range(25)])
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(bot, make_callback_update(MyGroupsPage(page=2).pack(), user_id=7))
    edits = [e for e in sent if e["kind"] == "edit"]
    assert len(edits) == 1
    text = edits[0]["text"]
    assert text.count("• <code>") == 10
    assert "-1014" in text  # first row of page 2
    assert "-1024" not in text  # page-1 row must be gone


@pytest.mark.asyncio
async def test_card_renders_summary(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=_ALL_SCHEMAS)
    await _seed(registry, [(-1001, "Alpha", 42)])
    await _seed_donations(registry, group_id=-1001, xp=777, total=12, position=3)
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(MyGroupsCard(group_id=-1001, page=1).pack(), user_id=42)
    )
    edits = [e for e in sent if e["kind"] == "edit"]
    assert len(edits) == 1
    text = edits[0]["text"]
    assert t("h_mygroups_card_title", "ru", title="Alpha", chat_id=-1001) in text
    assert t("h_mygroups_card_xp", "ru", xp=777, donations=12) in text
    assert t("h_mygroups_card_rating_pos", "ru", position=3) in text
    # Moderation defaults (no persisted row): automod on, antiflood off.
    on = t("h_mygroups_on", "ru")
    off = t("h_mygroups_off", "ru")
    assert t("h_mygroups_card_mod", "ru", automod=on, antiflood=off) in text
    # No message_counts rows seeded → zero activity.
    assert t("h_mygroups_card_activity", "ru", messages=0, active=0) in text
    # Tap acknowledged without an alert.
    assert any(e["kind"] == "callback_answer" for e in sent)


@pytest.mark.asyncio
async def test_card_no_donations_branch(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=_ALL_SCHEMAS)
    await _seed(registry, [(-1001, "Alpha", 42)])
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(MyGroupsCard(group_id=-1001, page=1).pack(), user_id=42)
    )
    edits = [e for e in sent if e["kind"] == "edit"]
    assert len(edits) == 1
    assert t("h_mygroups_card_no_donations", "ru") in edits[0]["text"]


@pytest.mark.asyncio
async def test_card_forged_callback_rejected(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=_ALL_SCHEMAS)
    await _seed(registry, [(-1001, "Alpha", 42)])
    sent = capture_callback_outgoing(bot)
    # user 99 forges a card callback for user 42's group.
    await dispatcher.feed_update(
        bot, make_callback_update(MyGroupsCard(group_id=-1001, page=1).pack(), user_id=99)
    )
    assert [e["kind"] for e in sent] == ["callback_answer"]
    assert sent[0]["text"] == t("h_mygroups_not_found", "ru")


@pytest.mark.asyncio
async def test_groups_the_bot_left_are_not_listed(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """#111: the row survives the bot's removal (it carries the payout
    attribution), so the list has to filter on ``is_active`` — otherwise
    every group the bot was ever kicked out of stays here forever, and
    the card behind it offers a purchase the bot cannot deliver."""
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    await _seed(registry, [(-1001, "Alpha", 42)])
    await _seed(registry, [(-1002, "Beta", 42)], is_active=0)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/mygroups", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "Alpha" in text
    assert "Beta" not in text
    assert t("h_mygroups_total", "ru", count=1) in text


@pytest.mark.asyncio
async def test_card_for_a_group_the_bot_left_is_rejected(
    make_wired: WiredFactory, capture_callback_outgoing: Any
) -> None:
    """Same filter on the card path: an old callback (a message still
    scrolled up in the DM) must not reopen a retired group."""
    bot, dispatcher, registry = await make_wired(schemas=_ALL_SCHEMAS)
    await _seed(registry, [(-1001, "Alpha", 42)], is_active=0)
    sent = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_callback_update(MyGroupsCard(group_id=-1001, page=1).pack(), user_id=42)
    )
    assert [e["kind"] for e in sent] == ["callback_answer"]
    assert sent[0]["text"] == t("h_mygroups_not_found", "ru")


@pytest.mark.asyncio
async def test_escapes_html_in_title(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    await _seed(registry, [(-1001, "<b>boom</b> & co", 42)])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/mygroups", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    assert "<b>boom</b>" not in text
    assert "&lt;b&gt;boom&lt;/b&gt;" in text
    assert "&amp; co" in text


@pytest.mark.asyncio
async def test_russian_alias(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/мои_группы", user_id=42, chat_type="private")
    )
    assert sent and sent[0]["text"] == t("h_mygroups_empty", "ru")


@pytest.mark.asyncio
async def test_admin_alias(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    """``/admin`` lands here, not on the developer panel.

    Legacy's ``cmd_admin`` (bot.py:25457) answered a non-developer with
    the list of groups they administer; the port had pointed the word at
    the developer panel, where a group admin got silence. The developer
    surface keeps ``/admin_panel``.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin", user_id=42, chat_type="private")
    )
    assert sent and sent[0]["text"] == t("h_mygroups_empty", "ru")


@pytest.mark.asyncio
async def test_group_invocation_is_refused(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    """A group ``/mygroups`` is answered, not ignored (#123)."""
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update("/mygroups", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="private", command="mygroups")


def test_list_markup_buttons_and_nav() -> None:
    """Builder contract: one card button per row, nav row when paginated,
    card payload carries the originating page.
    """
    rows: list[tuple[int, str | None]] = [(-1001, "Alpha"), (-1002, None)]
    markup = build_list_markup("ru", rows=rows, page=2, total=25)
    flat = [btn for row in markup.inline_keyboard for btn in row]
    datas = [btn.callback_data for btn in flat]
    assert MyGroupsCard(group_id=-1001, page=2).pack() in datas
    assert MyGroupsCard(group_id=-1002, page=2).pack() in datas
    # Missing title falls back to the chat id in the label.
    assert any("-1002" in (btn.text or "") for btn in flat)
    # 25 rows / 10 per page → 3 pages; page 2 has both arrows.
    assert MyGroupsPage(page=1).pack() in datas
    assert MyGroupsPage(page=3).pack() in datas
    assert any("2/3" in (btn.text or "") for btn in flat)
