"""VIP cosmetic effects in the ``/profile`` renderers (L-36).

These pin the *display* contract — given a resolved
:class:`VipDisplayEffects` bundle, the text card / group caption show
the colored-nick marker, legend line, and custom title, and crucially
HTML-escape the user-supplied title so a malicious ``custom_title``
can't inject markup into the card.

The resolver itself (rows → bundle) is covered in
``tests/integration/services/test_vip_display.py``; here we feed
bundles directly so the render logic is tested without a DB.
"""

from __future__ import annotations

from datetime import datetime

from telegram_invite_bot.core.entities.user import User
from telegram_invite_bot.handlers.profile import _format_text, _name_html
from telegram_invite_bot.services.vip_display import VipDisplayEffects


def _user() -> User:
    return User(
        user_id=777,
        username="alice",
        first_name="Alice",
        last_name=None,
        language_code="en",
        is_premium=False,
        joined_date=datetime(2024, 1, 1, 12, 0),
        last_seen=datetime(2024, 6, 1, 9, 30),
        last_active=datetime(2024, 6, 1, 9, 30),
        is_new=False,
    )


def test_plain_user_card_unchanged_by_none_effects() -> None:
    """No bundle (or an empty one) → the name is plain bold, no effect
    lines. Guards that L-36 is purely additive for non-VIP users."""
    text_none = _format_text(_user(), None)
    text_empty = _format_text(_user(), VipDisplayEffects())
    assert "<b>Alice</b>" in text_none
    assert text_none == text_empty
    assert "🌈" not in text_none
    assert "📝" not in text_none


def test_color_marker_prefixes_name_in_text_card() -> None:
    effects = VipDisplayEffects(color_marker="🌈")
    text = _format_text(_user(), effects)
    assert "🌈 <b>Alice</b>" in text


def test_legend_and_title_lines_render_in_text_card() -> None:
    effects = VipDisplayEffects(custom_title="Boss", legend_badge="💎")
    text = _format_text(_user(), effects)
    assert "💎" in text
    assert "📝 Boss" in text


def test_custom_title_is_html_escaped_in_text_card() -> None:
    """The title is the only free-form value; a ``<b>``/``<script>``
    payload must be neutralised, not embedded raw."""
    effects = VipDisplayEffects(custom_title="<b>x</b>&y")
    text = _format_text(_user(), effects)
    assert "📝 &lt;b&gt;x&lt;/b&gt;&amp;y" in text
    assert "<b>x</b>" not in text.replace("<b>Alice</b>", "")


def test_effects_render_in_group_name() -> None:
    # The group card's name (legacy parity) leads with the badge/colour
    # markers via the shared ``_name_html`` — the same unit the text card
    # uses, so the rich-caption restore can't drift the name layering.
    effects = VipDisplayEffects(color_marker="🎨", legend_badge="👑")
    name = _name_html(_user(), effects)
    assert "🎨 <b>Alice</b>" in name


def test_group_name_without_effects_matches_baseline() -> None:
    name_none = _name_html(_user(), None)
    name_empty = _name_html(_user(), VipDisplayEffects())
    assert name_none == name_empty
    assert "<b>Alice</b>" in name_none


# --- VIP cosmetic emoji badge (#25) on the /profile card -----------------


def test_badge_prefixes_name_in_text_card() -> None:
    """A resolved badge leads the bolded name, outside ``<b>``."""
    text = _format_text(_user(), None, "👑")
    assert "👑 <b>Alice</b>" in text


def test_badge_prefixes_name_in_group_name() -> None:
    name = _name_html(_user(), None, "💎")
    assert "💎 <b>Alice</b>" in name


def test_badge_sits_outside_color_marker() -> None:
    """With both a colour nick and a badge, the badge is outermost:
    ``👑 🌈 <b>Name</b>`` — pins the layering order."""
    effects = VipDisplayEffects(color_marker="🌈")
    text = _format_text(_user(), effects, "👑")
    assert "👑 🌈 <b>Alice</b>" in text


def test_no_badge_leaves_card_unchanged() -> None:
    """``badge=None`` (default) is byte-identical to the pre-#25 card —
    guards that the badge column is purely additive."""
    assert _format_text(_user(), None, None) == _format_text(_user(), None)
    assert _name_html(_user(), None, None) == _name_html(_user(), None)
