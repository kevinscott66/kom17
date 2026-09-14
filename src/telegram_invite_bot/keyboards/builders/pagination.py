"""Common pagination keyboard builder for shop and inventory handlers.

Consolidates the duplicated navigation row logic between _build_shop_keyboard
and _build_inventory_keyboard. Both handlers implement identical pagination
patterns with prev/indicator/next buttons but duplicate the construction code.

Stage 31 (R-001): Extracted to reduce duplication and ensure consistent
pagination behavior across handlers.
"""

from __future__ import annotations

from typing import Protocol

from aiogram.types import InlineKeyboardButton

from telegram_invite_bot.i18n import t


class PaginationCallback(Protocol):
    """Protocol for pagination callback data classes.

    Both ShopPage and InventoryPage implement this pattern.
    """

    def __init__(self, *, page: int, **extra: object) -> None: ...

    def pack(self) -> str: ...


def build_pagination_nav(
    *,
    lang: str,
    current_page: int,
    total_pages: int,
    callback_factory: type[PaginationCallback],
    nav_keys: tuple[str, str, str],
    extra: dict[str, object] | None = None,
) -> list[InlineKeyboardButton]:
    """Build a pagination navigation row with prev/indicator/next buttons.

    Args:
        lang: User language for i18n keys
        current_page: 0-indexed current page number
        total_pages: Total number of pages
        callback_factory: CallbackData class that takes page parameter
        nav_keys: Tuple of (prev_key, indicator_key, next_key) for i18n
        extra: Additional constant fields stamped into EVERY nav button's
            payload — the context the list is scoped to (RR-2 #14 passes
            ``{"group_id": ...}`` so a page flip stays inside the chosen
            group instead of silently falling back to a global purchase).
            Constant by construction: only ``page`` varies across the row.

    Returns:
        List of navigation buttons (prev, indicator, next as applicable)

    Example:
        nav = build_pagination_nav(
            lang="ru",
            current_page=1,
            total_pages=3,
            callback_factory=ShopPage,
            nav_keys=("h_shop_nav_prev", "h_shop_nav_indicator", "h_shop_nav_next")
        )
    """
    nav: list[InlineKeyboardButton] = []
    prev_key, indicator_key, next_key = nav_keys
    fields = extra or {}

    # Previous page button (if not on first page)
    if current_page > 0:
        nav.append(
            InlineKeyboardButton(
                text=t(prev_key, lang),
                callback_data=callback_factory(page=current_page - 1, **fields).pack(),
            )
        )

    # Current page indicator (always present)
    nav.append(
        InlineKeyboardButton(
            text=t(indicator_key, lang, current=current_page + 1, total=total_pages),
            callback_data=callback_factory(page=current_page, **fields).pack(),
        )
    )

    # Next page button (if not on last page)
    if current_page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                text=t(next_key, lang),
                callback_data=callback_factory(page=current_page + 1, **fields).pack(),
            )
        )

    return nav
