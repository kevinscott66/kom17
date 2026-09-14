"""Unit tests for the live stats-card PNG generator (T-024.4)."""

from __future__ import annotations

import io
from datetime import datetime

from PIL import Image

from telegram_invite_bot.utils.stats_image import get_pil_font, render_stats_card


def test_render_stats_card_returns_decodable_png() -> None:
    png = render_stats_card(
        "Иван Петров",
        [("Сегодня", 3), ("За 7 дней", 21), ("За 30 дней", 90), ("Всего", 412)],
        subtitle="Личный график активности",
        generated_at=datetime(2026, 6, 6, 14, 30),
    )
    assert isinstance(png, bytes)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"
    assert img.size == (1200, 760)


def test_render_stats_card_handles_empty_rows() -> None:
    """A user with no activity still gets a valid (empty) card."""
    png = render_stats_card(
        "Newbie",
        [],
        generated_at=datetime(2026, 6, 6, 0, 0),
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_stats_card_caps_at_nine_rows() -> None:
    """The legacy ceiling is 9 rows; extra rows must not raise."""
    rows = [(f"row {i}", i * 10) for i in range(20)]
    png = render_stats_card("Many", rows, generated_at=datetime(2026, 6, 6, 0, 0))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_get_pil_font_is_cached() -> None:
    """Same (path, size) returns the identical face object — no re-open."""
    a = get_pil_font(28)
    b = get_pil_font(28)
    assert a is b
