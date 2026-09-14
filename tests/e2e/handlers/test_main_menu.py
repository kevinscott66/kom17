"""End-to-end main-menu callback flow (A-01 Part B).

The private ``/start`` welcome carries an inline keyboard
(:func:`main_menu_keyboard`) whose taps route to a single
``handle_menu`` callback that edits the message into the chosen card.
These tests drive real ``callback_query`` Updates through the wired
dispatcher and assert the edited card text + that the acting user is
``callback.from_user`` (never the bot on ``callback.message``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram.dispatcher.event.bases import UNHANDLED

from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu
from tests.e2e.handlers.conftest import make_callback_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


async def _feed(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
    action: str,
    *,
    user_id: int = 5001,
) -> list[dict[str, Any]]:
    bot, dispatcher, _registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sink = capture_callback_outgoing(bot)
    update = make_callback_update(
        MainMenu(action=action).pack(),
        user_id=user_id,
        language_code="ru",
    )
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED
    return sink


async def test_menu_balance_edits_into_balance_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    sink = await _feed(make_wired, capture_callback_outgoing, "balance")
    edit = next(e for e in sink if e["kind"] == "edit")
    assert "Баланс" in edit["text"]
    # A callback_answer must always be emitted to clear the spinner.
    assert any(e["kind"] == "callback_answer" for e in sink)


async def test_menu_profile_edits_into_profile_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    sink = await _feed(make_wired, capture_callback_outgoing, "profile")
    edit = next(e for e in sink if e["kind"] == "edit")
    assert "Профиль" in edit["text"]
    # The card renders the *tapping* user's id, not the bot's (id 0).
    assert "<code>5001</code>" in edit["text"]


async def test_menu_referral_edits_into_referral_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    sink = await _feed(make_wired, capture_callback_outgoing, "referral")
    edit = next(e for e in sink if e["kind"] == "edit")
    # Deep link carries the tapper's own id.
    assert "ref_5001" in edit["text"]


async def test_menu_home_returns_to_welcome(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """ "⬅️ back" must land on exactly the card ``/start`` shows for a
    returning user — same renderer, refreshed numbers (RR-6 #60)."""
    sink = await _feed(make_wired, capture_callback_outgoing, "home")
    edit = next(e for e in sink if e["kind"] == "edit")
    text = edit["text"]
    assert "Баланс" in text
    assert "Игр сыграно" in text
    assert "Выбери, чем займёмся" in text
    # Plain member (not a developer, owns no groups) → 👤.
    assert "👤" in text


# --- RR-59: richer menu destinations ---------------------------------------


async def test_menu_shop_edits_into_shop_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Shop tap renders the catalog in place (empty-catalog path here —
    proves ``shop_items_repo`` is injected and the read runs)."""
    sink = await _feed(make_wired, capture_callback_outgoing, "shop")
    edit = next(e for e in sink if e["kind"] == "edit")
    assert "Магазин" in edit["text"]


async def test_menu_games_edits_into_games_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    sink = await _feed(make_wired, capture_callback_outgoing, "games")
    edit = next(e for e in sink if e["kind"] == "edit")
    assert "/duel" in edit["text"]


async def test_menu_help_edits_into_help_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """RR-6 #62: the tap renders the *same* catalog ``/help`` sends, so
    the two entry points can't drift into two different answers."""
    sink = await _feed(make_wired, capture_callback_outgoing, "help")
    edit = next(e for e in sink if e["kind"] == "edit")
    text = edit["text"]
    assert "Что я умею" in text
    assert "• /start —" in text
    assert "• /profile —" in text
    # Category headings, i.e. the grouped card and not a flat bullet list.
    assert "Базовые" in text
    # ``t()`` echoes the key on a miss — none may reach the card.
    assert "h_cmd_" not in text
    assert "h_help_" not in text


async def test_menu_help_card_leaks_no_staff_surface(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A menu tap carries no chat-admin context, so ``handle_menu``
    deliberately passes no ``ranks``: the in-place card is the plain-user
    view even when the tapper happens to be staff elsewhere. Moderation
    rows and the ``[от N⭐]`` thresholds must both stay out."""
    sink = await _feed(make_wired, capture_callback_outgoing, "help")
    text = next(e for e in sink if e["kind"] == "edit")["text"]
    assert "/ban —" not in text
    assert "/cmdcfg —" not in text
    assert "⭐" not in text


async def test_menu_help_card_keeps_the_back_control(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The in-place card replaces the menu, so its own "back" button is
    the only way out — losing it strands the user on a dead card."""
    sink = await _feed(make_wired, capture_callback_outgoing, "help")
    edit = next(e for e in sink if e["kind"] == "edit")
    markup = edit["markup"]
    assert markup is not None
    actions = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert any((data or "").endswith("home") for data in actions), actions
    # ``has_button=False`` → no Telegraph footer promising a button that
    # the in-place card never attaches.
    assert "кнопке ниже" not in edit["text"]


async def test_menu_help_pages_forward_in_place(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """#114: the plain catalog no longer fits one message, so the card
    pages in place. Page 1 offers ▶️ and no ◀️ (there is nothing behind
    it), and the arrow must carry the *next* page, not re-render this
    one."""
    sink = await _feed(make_wired, capture_callback_outgoing, "help")
    edit = next(e for e in sink if e["kind"] == "edit")
    actions = [b.callback_data for row in edit["markup"].inline_keyboard for b in row]
    assert MainMenu(action="help2").pack() in actions, actions
    assert MainMenu(action="help").pack() not in actions, actions
    assert "📄" in edit["text"]


async def test_menu_help_second_page_carries_the_tail_and_a_way_back(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The tail category is the one the old "type /help for the rest"
    fallback hid — ``/ai`` and ``/support`` are exactly the commands a
    lost user needs. Page 2 must show them and offer both ◀️ and the
    menu "back", or the arrow strands the user one page deep."""
    sink = await _feed(make_wired, capture_callback_outgoing, "help2")
    edit = next(e for e in sink if e["kind"] == "edit")
    assert "• /ai —" in edit["text"]
    actions = [b.callback_data for row in edit["markup"].inline_keyboard for b in row]
    assert MainMenu(action="help").pack() in actions, actions
    assert any((data or "").endswith("home") for data in actions), actions


async def test_menu_help_paging_arrow_is_not_the_back_arrow(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Two buttons that look identical must not do different things.

    Found on the live bot: page 2 rendered ``⬅️`` (previous page) sitting
    directly above ``⬅️ Назад`` (leave the help card). Same glyph, same
    card, two destinations — the user has to guess. The paging pair is
    ``◀️``/``▶️`` for exactly this reason.

    Asserted as "no leading glyph repeats" rather than "the arrow is
    ``◀️``": the defect is the collision, so a future re-styling is free
    to pick another pair as long as it stays distinguishable.
    """
    sink = await _feed(make_wired, capture_callback_outgoing, "help2")
    edit = next(e for e in sink if e["kind"] == "edit")
    labels = [b.text for row in edit["markup"].inline_keyboard for b in row]
    assert len(labels) > 1, labels
    glyphs = [label.split()[0] for label in labels]
    assert len(set(glyphs)) == len(glyphs), labels


async def test_menu_help_out_of_range_page_clamps_instead_of_erroring(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Callback data is user-editable: ``menu:help999`` is one tampered
    button away. It must land on a real page, not raise an IndexError
    into the global error router (which shows "⚠️ Произошла ошибка")."""
    sink = await _feed(make_wired, capture_callback_outgoing, "help999")
    edit = next(e for e in sink if e["kind"] == "edit")
    assert "• /ai —" in edit["text"]
    assert any(e["kind"] == "callback_answer" for e in sink)


async def test_menu_help_garbage_page_falls_back_to_the_first(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Non-numeric junk after the action must not be parsed as a page.
    ``"٣"`` is an Arabic-Indic three: ``str.isdigit()`` says yes and
    ``int()`` would even accept it, which is the class of bug #102 was
    about — pin the ASCII-only check so it can't come back here."""
    for action in ("help٣", "helpx", "help-1"):
        sink = await _feed(make_wired, capture_callback_outgoing, action)
        edit = next(e for e in sink if e["kind"] == "edit")
        assert "• /start —" in edit["text"], action


async def test_menu_commission_edits_into_commission_card(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    sink = await _feed(make_wired, capture_callback_outgoing, "commission")
    edit = next(e for e in sink if e["kind"] == "edit")
    # No referral earnings seeded → COALESCE renders a hard zero, not None.
    assert "Мои комиссии" in edit["text"]
    assert "<b>0</b>" in edit["text"]


async def test_menu_daily_is_readonly_hint_no_claim(
    make_wired: WiredFactory,
    capture_callback_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """The 🎁 daily tap must be a *hint* — never a silent claim. It points
    at ``/daily`` and leaves the economy completely untouched (no wallet
    row is even created)."""
    from telegram_invite_bot.db.models.economy import EconomyUser
    from telegram_invite_bot.db.names import DBName

    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase, EconomyBase], session_middleware=True
    )
    sink = capture_callback_outgoing(bot)
    update = make_callback_update(MainMenu(action="daily").pack(), user_id=7007, language_code="ru")
    result = await dispatcher.feed_update(bot, update)
    assert result is not UNHANDLED
    edit = next(e for e in sink if e["kind"] == "edit")
    assert "/daily" in edit["text"]
    # Safety invariant: a menu tap created no economy state.
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        assert await session.get(EconomyUser, 7007) is None


def test_main_menu_keyboard_is_rich_and_addgroup_url_optional() -> None:
    """RR-59 keyboard shape: the base menu carries the full breadth, the
    🏆 rating button reuses the rating router's ``ratnav`` callback (not a
    ``menu`` action), and the add-to-group URL row appears only when a
    deep link is resolved."""
    from telegram_invite_bot.handlers.main_menu import main_menu_keyboard

    bare = main_menu_keyboard("ru")
    flat = [b for row in bare.inline_keyboard for b in row]
    labels = [b.text for b in flat]
    # Breadth: far more than the original 3 buttons.
    assert len(flat) >= 9
    assert any("Магазин" in x for x in labels)
    assert any("Комиссии" in x for x in labels)
    # Rating reuses the rating router's callback prefix, no duplicate render.
    assert any((b.callback_data or "").startswith("ratnav") for b in flat)
    # No URL button without a resolved deep link.
    assert all(b.url is None for b in flat)

    with_url = main_menu_keyboard("ru", add_group_url="https://t.me/x?startgroup=true")
    url_btns = [b for row in with_url.inline_keyboard for b in row if b.url]
    assert len(url_btns) == 1
    assert url_btns[0].url == "https://t.me/x?startgroup=true"
