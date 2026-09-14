"""Unit tests for pagination keyboard builder."""

from __future__ import annotations

import pytest

import telegram_invite_bot.keyboards.builders.pagination as pagination_mod
from telegram_invite_bot.keyboards.builders import InventoryPage, ShopPage
from telegram_invite_bot.keyboards.builders.pagination import build_pagination_nav


class MockI18n:
    """Mock i18n function for testing."""

    @staticmethod
    def t(key: str, lang: str, **kwargs) -> str:
        # Mock translations for testing
        translations = {
            "h_shop_nav_prev": "« prev",
            "h_shop_nav_next": "next »",
            "h_shop_nav_indicator": "{current}/{total}",
            "h_inventory_nav_prev": "« back",
            "h_inventory_nav_next": "forward »",
            "h_inventory_nav_indicator": "{current} of {total}",
        }
        template = translations.get(key, key)
        return template.format(**kwargs)


@pytest.fixture(autouse=True)
def _patch_i18n(monkeypatch):
    """Patch the i18n `t` callable only for the duration of each test.

    Without scoping via monkeypatch this previously leaked across the whole
    pytest session and broke shop/inventory e2e tests that exercise the
    real i18n strings.
    """
    monkeypatch.setattr(pagination_mod, "t", MockI18n.t)


def test_pagination_single_page():
    """Single page should result in just indicator button."""
    nav = build_pagination_nav(
        lang="en",
        current_page=0,
        total_pages=1,
        callback_factory=ShopPage,
        nav_keys=("h_shop_nav_prev", "h_shop_nav_indicator", "h_shop_nav_next"),
    )

    # Only indicator button for single page
    assert len(nav) == 1
    assert nav[0].text == "1/1"
    # ShopPage callback prefix is "shop_pg"
    assert nav[0].callback_data.startswith("shop_pg:")


def test_pagination_first_page():
    """First page of multiple should show indicator and next only."""
    nav = build_pagination_nav(
        lang="en",
        current_page=0,
        total_pages=3,
        callback_factory=ShopPage,
        nav_keys=("h_shop_nav_prev", "h_shop_nav_indicator", "h_shop_nav_next"),
    )

    assert len(nav) == 2
    assert nav[0].text == "1/3"  # Indicator
    assert nav[1].text == "next »"  # Next button


def test_pagination_middle_page():
    """Middle page should show all three buttons."""
    nav = build_pagination_nav(
        lang="en",
        current_page=1,
        total_pages=3,
        callback_factory=ShopPage,
        nav_keys=("h_shop_nav_prev", "h_shop_nav_indicator", "h_shop_nav_next"),
    )

    assert len(nav) == 3
    assert nav[0].text == "« prev"  # Previous
    assert nav[1].text == "2/3"  # Indicator
    assert nav[2].text == "next »"  # Next


def test_pagination_last_page():
    """Last page should show previous and indicator only."""
    nav = build_pagination_nav(
        lang="en",
        current_page=2,
        total_pages=3,
        callback_factory=ShopPage,
        nav_keys=("h_shop_nav_prev", "h_shop_nav_indicator", "h_shop_nav_next"),
    )

    assert len(nav) == 2
    assert nav[0].text == "« prev"  # Previous
    assert nav[1].text == "3/3"  # Indicator


def test_pagination_with_different_callback_factory():
    """Should work with different callback factories."""
    nav = build_pagination_nav(
        lang="en",
        current_page=1,
        total_pages=3,
        callback_factory=InventoryPage,
        nav_keys=("h_inventory_nav_prev", "h_inventory_nav_indicator", "h_inventory_nav_next"),
    )

    assert len(nav) == 3
    assert nav[0].text == "« back"  # Different prev text
    assert nav[1].text == "2 of 3"  # Different indicator format
    assert nav[2].text == "forward »"  # Different next text

    # InventoryPage callback prefix is "inv_pg"
    assert nav[0].callback_data.startswith("inv_pg:")


def test_pagination_callback_data_contains_correct_pages():
    """Callback data should contain correct page numbers."""
    nav = build_pagination_nav(
        lang="en",
        current_page=1,
        total_pages=3,
        callback_factory=ShopPage,
        nav_keys=("h_shop_nav_prev", "h_shop_nav_indicator", "h_shop_nav_next"),
    )

    # Previous button should link to page 0
    prev_data = ShopPage.unpack(nav[0].callback_data)
    assert prev_data.page == 0

    # Indicator button should link to current page (no-op)
    indicator_data = ShopPage.unpack(nav[1].callback_data)
    assert indicator_data.page == 1

    # Next button should link to page 2
    next_data = ShopPage.unpack(nav[2].callback_data)
    assert next_data.page == 2


def test_pagination_edge_case_two_pages():
    """Two page case should work correctly."""
    # First page of two
    nav = build_pagination_nav(
        lang="en",
        current_page=0,
        total_pages=2,
        callback_factory=ShopPage,
        nav_keys=("h_shop_nav_prev", "h_shop_nav_indicator", "h_shop_nav_next"),
    )

    assert len(nav) == 2
    assert nav[0].text == "1/2"  # Indicator
    assert nav[1].text == "next »"  # Next

    # Second page of two
    nav = build_pagination_nav(
        lang="en",
        current_page=1,
        total_pages=2,
        callback_factory=ShopPage,
        nav_keys=("h_shop_nav_prev", "h_shop_nav_indicator", "h_shop_nav_next"),
    )

    assert len(nav) == 2
    assert nav[0].text == "« prev"  # Previous
    assert nav[1].text == "2/2"  # Indicator
