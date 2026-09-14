"""End-to-end ``/admin_panel`` unified developer panel (T-024.3).

The panel is the single entry point the operator was missing:
``/admin_panel`` renders an inline-button root menu, and tapping a
category edits the card to that category's command list with a back
button. This file pins:

* Non-developer → silent drop on both the command and the nav callback
  (existence must not enumerate dev IDs).
* Developer in private → root card with the category buttons.
* Bare ``/admin`` is *not* this panel — it is the multi-group admin
  panel (``handlers/mygroups.py``), the surface legacy gave a
  non-developer who typed the word.
* Group invocation → router-level private filter rejects.
* Tapping a category edits the same card to that category's commands.
* The back button returns to the root card.
* An unknown / stale section key acks without editing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.keyboards.builders import AdminNav
from tests.e2e.handlers.conftest import make_callback_update, make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory

_DEV = 555


@pytest.mark.asyncio
async def test_silent_for_non_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_panel", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_root_renders_for_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_panel", user_id=_DEV, chat_type="private")
    )
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "Панель разработчика" in text
    # The recognised dev id is surfaced so a new operator can confirm
    # their id lands them in the dev bucket.
    assert str(_DEV) in text


@pytest.mark.asyncio
async def test_bare_admin_is_not_this_panel(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """``/admin`` belongs to the multi-group admin panel now.

    It used to be an alias of this router, which meant a group admin
    typing the word legacy taught them got nothing. The word moved to
    ``handlers/mygroups.py``; this router answers only to the spelling
    that says what it is. Pinned here so re-adding the alias — and
    stealing the word back — fails loudly rather than quietly.
    """
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
        schemas=[UsersBase],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin", user_id=_DEV, chat_type="private")
    )
    # The developer typed the word too, so *something* answers — the
    # group panel, whose own suite pins the wording. All this asserts is
    # that it is not this card.
    assert not any("Панель разработчика" in item["text"] for item in sent)


@pytest.mark.asyncio
async def test_group_invocation_rejected(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_panel", user_id=_DEV, chat_id=-100, chat_type="supergroup"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_nav_to_category_edits_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sink = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(AdminNav(section="payments").pack(), user_id=_DEV),
    )
    edits = [e for e in sink if e["kind"] == "edit"]
    assert len(edits) == 1
    assert "/payment_keys" in edits[0]["text"]
    assert "/admin_donations" in edits[0]["text"]
    # The spinner must always be cleared.
    assert [e for e in sink if e["kind"] == "callback_answer"]


@pytest.mark.asyncio
async def test_nav_back_to_root(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sink = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(AdminNav(section="root").pack(), user_id=_DEV),
    )
    edits = [e for e in sink if e["kind"] == "edit"]
    assert len(edits) == 1
    assert "Панель разработчика" in edits[0]["text"]


@pytest.mark.asyncio
async def test_nav_non_developer_dropped(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sink = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(AdminNav(section="payments").pack(), user_id=12345),
    )
    # No card edit leaked to the non-dev; the spinner is still cleared.
    assert [e for e in sink if e["kind"] == "edit"] == []
    assert [e for e in sink if e["kind"] == "callback_answer"]


@pytest.mark.asyncio
async def test_nav_unknown_section_acks_without_edit(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=_DEV),
    )
    sink = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(AdminNav(section="bogus_stale_key").pack(), user_id=_DEV),
    )
    assert [e for e in sink if e["kind"] == "edit"] == []
    assert [e for e in sink if e["kind"] == "callback_answer"]


@pytest.mark.asyncio
async def test_root_card_and_buttons_follow_the_callers_language(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """The root screen carried Russian literals in three places at once:
    the title, the hint, and — easiest to miss — the category *button*
    labels, which live in the catalog tuple rather than in the render
    function.
    """
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=333),
        schemas=[UsersBase],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_panel", user_id=333, chat_type="private", language_code="en"),
    )
    card = sent[0]
    assert "Developer panel" in card["text"]
    assert "Developers:" in card["text"]
    assert not any("Ѐ" <= ch <= "ӿ" for ch in card["text"]), card["text"]

    labels = [button.text for row in card["markup"].inline_keyboard for button in row]
    assert "💳 Payments" in labels
    assert "🐧 System" in labels
    assert not any("Ѐ" <= ch <= "ӿ" for ch in "".join(labels)), labels


@pytest.mark.asyncio
async def test_category_card_and_back_button_follow_the_callers_language(
    make_wired: WiredFactory,
    capture_callback_outgoing: Any,
) -> None:
    """A category screen is a separate render path: its header and every
    one-line description come from the same bilingual tuple, and the
    ``⬅️`` back button comes from the YAML. Slash commands stay verbatim
    in both locales — they are what the operator types.
    """
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=334),
        schemas=[UsersBase],
    )
    sink = capture_callback_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_callback_update(AdminNav(section="payments").pack(), user_id=334, language_code="en"),
    )
    edits = [e for e in sink if e["kind"] == "edit"]
    assert len(edits) == 1
    text = edits[0]["text"]
    assert "Payments" in text
    assert "payment-key status" in text
    # The command names themselves must survive translation.
    assert "/payment_keys" in text
    assert "/admin_donations" in text
    assert not any("Ѐ" <= ch <= "ӿ" for ch in text), text

    back = edits[0]["markup"].inline_keyboard[0][0].text
    assert not any("Ѐ" <= ch <= "ӿ" for ch in back), back
