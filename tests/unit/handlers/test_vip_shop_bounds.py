"""``/vip_shop`` must stay inside Telegram's limits on any catalog.

Two unbounded inputs feed one message. ``ShopItemsRepo.list_by_type``
has no ``LIMIT``, so the plan count is whatever the ``shop_items`` table
holds; and a hand-seeded row (one whose name isn't in
``VIP_NAME_DURATIONS``) renders the operator's own ``name`` /
``description`` columns, neither of which is length-checked on the write
side. Either one alone can push the card past 4096, and there the
``reply`` is a 400 the user never sees — the same silent failure
``/filter_list`` and ``/warnings`` were fixed for.

The answer here is a window rather than pagination: ``/shop`` already
pages the whole catalog with the identical buy buttons, so the plans
past the window have a real destination, and the card says how many
they are.

What these tests pin:

* the four canonical plans render untouched — the window must be
  invisible on every catalog anyone actually has;
* a pathological catalog stays under the ceiling, and the card admits
  what it left out;
* the keyboard tracks the same window as the text — buttons for plans
  the body never showed (or plans with no button) is the failure mode
  ``/shop`` documents;
* operator copy is clamped in UTF-16 units, the unit Telegram counts.
"""

from __future__ import annotations

from telegram_invite_bot.core.entities.shop import ShopItemEntity
from telegram_invite_bot.handlers.vip import (
    _MAX_PLANS,
    _PLAN_DESC_MAX,
    _PLAN_NAME_MAX,
    _build_vip_shop_keyboard,
    _format_vip_shop,
    _plan_copy,
)
from telegram_invite_bot.utils.render import (
    TELEGRAM_TEXT_LIMIT,
    parsed_length,
    utf16_length,
)

_CANONICAL = ("👑 VIP (1 месяц)", "👑 VIP (3 месяца)", "👑 VIP (6 месяцев)", "👑 VIP (1 год)")


def _item(item_id: int, *, name: str, description: str = "", price: int = 1000) -> ShopItemEntity:
    return ShopItemEntity(
        id=item_id,
        name=name,
        description=description,
        price=price,
        type="vip",
        stock=-1,
    )


def _canonical_catalog() -> list[ShopItemEntity]:
    return [
        _item(index + 1, name=name, price=(index + 1) * 1000)
        for index, name in enumerate(_CANONICAL)
    ]


def test_the_real_catalog_renders_whole_and_says_nothing_about_hiding() -> None:
    """Four plans is what the seed ships and what prod has — the window
    must leave that card exactly as it was."""
    items = _canonical_catalog()

    card = _format_vip_shop(items, "ru", hidden=0)

    for name in ("VIP на месяц", "VIP на 3 месяца", "VIP на полгода", "VIP на год"):
        assert name in card
    assert "/shop" not in card
    assert len(_build_vip_shop_keyboard(items, "ru").inline_keyboard) == len(items)


def test_a_catalog_of_verbose_hand_seeded_plans_stays_under_the_ceiling() -> None:
    """The case that produces the 400: many rows, each carrying operator
    text nothing on the write side bounds."""
    items = [
        _item(index, name=f"{'П' * 200}{index}", description="о" * 4000, price=100 + index)
        for index in range(1, 41)
    ]
    shown = items[:_MAX_PLANS]

    card = _format_vip_shop(shown, "ru", hidden=len(items) - len(shown))

    assert parsed_length(card) <= TELEGRAM_TEXT_LIMIT
    # And it admits the truncation instead of pretending the catalog ends.
    assert "/shop" in card
    assert str(len(items) - _MAX_PLANS) in card


def test_the_keyboard_never_outgrows_the_window() -> None:
    """A button per plan, but only for plans the body showed."""
    items = [_item(index, name=f"План {index}") for index in range(1, 41)]

    keyboard = _build_vip_shop_keyboard(items[:_MAX_PLANS], "ru")

    assert len(keyboard.inline_keyboard) == _MAX_PLANS


def test_operator_copy_is_clamped_in_utf16_units() -> None:
    """An emoji costs Telegram two units and ``len()`` one, so a clamp
    that counted code points would still let an emoji name through at
    double the intended width."""
    item = _item(1, name="😀" * 200, description="🙂" * 500)

    name, desc = _plan_copy(item, "ru")

    assert utf16_length(name) <= _PLAN_NAME_MAX
    assert utf16_length(desc) <= _PLAN_DESC_MAX
    # Clamping cuts between characters — a lone surrogate is not even
    # encodable, and the send would fail on the exact input the clamp
    # exists to rescue.
    name.encode("utf-8")
    desc.encode("utf-8")


def test_the_operator_ceiling_clears_our_own_authored_blurbs() -> None:
    """The clamp only fires on hand-seeded rows, so it must be wide
    enough for copy of the length we ourselves consider normal —
    otherwise an operator writing a perfectly ordinary blurb gets it cut
    mid-sentence. The curated plans are the only yardstick we have.
    """
    curated = [_plan_copy(_item(index, name=name), "ru") for index, name in enumerate(_CANONICAL)]

    assert curated, "no canonical plan names — the yardstick is gone"
    assert max(utf16_length(name) for name, _ in curated) <= _PLAN_NAME_MAX
    assert max(utf16_length(desc) for _, desc in curated) <= _PLAN_DESC_MAX
