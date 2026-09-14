"""Unit tests for the couple-activities catalog/story renderers (RR-5 #49/#51).

These are pure string builders — no bot, no DB — so they are cheap to
assert in both locales at once. The e2e suite covers the money path;
here we only pin the *shape* of what the user reads.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.core.couple_activities import (
    HISTORY_ONLY_ICONS,
    MARRIAGE_ACTIVITIES,
    RELATIONSHIP_ACTIVITIES,
    MarriageActivity,
    RelationshipActivity,
)
from telegram_invite_bot.handlers import couple_activities as couple_activities_handler
from telegram_invite_bot.handlers.couple_activities import (
    _BUTTON_CAPTION_MAX,
    _activity_icon,
    _activity_title,
    _build_marriage_menu,
    _build_relationship_menu,
    _button_caption,
    _marriage_catalog,
    _relationship_catalog,
    _story_line,
)

_LANGS = ["ru", "en"]

# Every catalog row, both kinds. Annotated because the two frozen
# dataclasses are unrelated types — mypy widens a bare splat to
# ``object`` and then every ``.key`` access is an error.
_EVERY_ACTIVITY: tuple[MarriageActivity | RelationshipActivity, ...] = (
    *MARRIAGE_ACTIVITIES,
    *RELATIONSHIP_ACTIVITIES,
)


# ---------------------------------------------------------------------------
# Icons + titles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", _LANGS)
def test_titles_resolve_for_every_catalog_row(lang: str) -> None:
    for act in _EVERY_ACTIVITY:
        title = _activity_title(act.key, lang)
        assert title and not title.startswith("h_couple_act_name_"), act.key
        assert _activity_icon(act.key) == act.icon


@pytest.mark.parametrize("lang", _LANGS)
def test_history_only_keys_render_a_name_and_their_own_icon(lang: str) -> None:
    """#232: the history log holds far more keys than either catalog.

    Every RP action lands there, and so do the activity keys retired
    before the catalog was last reshuffled. They have no button, no
    price and no story — but they DO have a name, and legacy printed it
    (bot.py:23088-23093, :23401-23407). The port named the two catalogs
    only, so 14 of the 18 rows on production rendered ``💕 rp_tickle``.
    """
    for key in HISTORY_ONLY_ICONS:
        title = _activity_title(key, lang)
        assert title, key
        assert not title.startswith("h_couple_act_name_"), key
        assert title != key, key
        assert _activity_icon(key) == HISTORY_ONLY_ICONS[key], key


@pytest.mark.parametrize("lang", _LANGS)
def test_a_key_no_table_knows_falls_back_to_an_em_dash(lang: str) -> None:
    """The row still happened, so it is still shown — just unnamed.

    Legacy's fallback was ``("—", "—")``. Echoing the raw key back (what
    the port did before #232) reads to the user as a broken bot rather
    than as one stale row, which is exactly the wrong impression when
    the XP and the date beside it are perfectly good.
    """
    assert _activity_title("no_such_activity_key", lang) == "—"
    assert _activity_icon("no_such_activity_key") == "💕"


# ---------------------------------------------------------------------------
# Catalog body + buttons
# ---------------------------------------------------------------------------


def _rows(body: str) -> list[str]:
    """The activity rows only — the trailing block is the glyph legend."""
    return body.split("\n\n")[0].splitlines()


@pytest.mark.parametrize("lang", _LANGS)
def test_catalog_marks_affordable_rows_and_ends_with_a_legend(lang: str) -> None:
    poor = _rows(_marriage_catalog(lang, balance=0))
    rich = _rows(_marriage_catalog(lang, balance=1_000_000))
    assert len(poor) == len(MARRIAGE_ACTIVITIES)
    assert all(row.startswith("🕔") for row in poor)
    assert all(row.startswith("✅") for row in rich)
    # The legend explains both glyphs and closes the block.
    legend = _marriage_catalog(lang, balance=0).split("\n\n")[1]
    assert legend.startswith("✅ —")
    assert "🕔" in legend


@pytest.mark.parametrize("lang", _LANGS)
def test_relationship_catalog_gates_on_level_and_coins(lang: str) -> None:
    # min_level 0 rows only; everything above stays locked.
    low = _rows(_relationship_catalog(lang, level=0, balance=1_000_000))
    open_at_zero = sum(1 for a in RELATIONSHIP_ACTIVITIES if a.min_level == 0)
    assert sum(row.startswith("✅") for row in low) == open_at_zero

    top = _rows(_relationship_catalog(lang, level=10, balance=1_000_000))
    assert all(row.startswith("✅") for row in top)

    broke = _rows(_relationship_catalog(lang, level=10, balance=0))
    assert all(row.startswith("🕔") for row in broke)


@pytest.mark.parametrize("lang", _LANGS)
def test_catalog_row_carries_icon_title_xp_and_cost(lang: str) -> None:
    rows = _rows(_relationship_catalog(lang, level=10, balance=1_000_000))
    for row, act in zip(rows, RELATIONSHIP_ACTIVITIES, strict=True):
        assert act.icon in row
        assert f"<b>{_activity_title(act.key, lang)}</b>" in row
        assert "XP" in row
        assert "🪙" in row


@pytest.mark.parametrize("lang", _LANGS)
def test_every_button_caption_fits_the_telegram_cap(lang: str) -> None:
    """Telegram truncates inline-button captions past 64 chars."""
    for act in MARRIAGE_ACTIVITIES:
        caption = _button_caption(act.key, ok=True, cost=act.cost, xp=act.xp, lang=lang)
        assert len(caption) <= _BUTTON_CAPTION_MAX, act.key
    for rel in RELATIONSHIP_ACTIVITIES:
        caption = _button_caption(rel.key, ok=False, cost=rel.cost, xp=rel.xp, lang=lang)
        assert len(caption) <= _BUTTON_CAPTION_MAX, rel.key


@pytest.mark.parametrize("lang", _LANGS)
def test_over_long_caption_is_clipped_with_an_ellipsis(
    lang: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No shipped title is anywhere near 64 chars, so force one.

    This used to pass an over-long synthetic KEY and lean on the old
    fall-through echoing it back verbatim. #232 replaced that echo with
    ``—``, which is short — so the key can no longer smuggle length into
    the caption. Stubbing the title is the honest way to reach the clip:
    the clip is what this test is about, and it must keep working when
    a future title grows.
    """
    monkeypatch.setattr(couple_activities_handler, "_activity_title", lambda key, lang: "щ" * 90)
    caption = _button_caption("rp_tickle", ok=True, cost=1, xp=1, lang=lang)
    assert len(caption) == _BUTTON_CAPTION_MAX
    assert caption.endswith("…")


@pytest.mark.parametrize("lang", _LANGS)
def test_menus_expose_one_button_per_catalog_row(lang: str) -> None:
    marry = _build_marriage_menu(partner_id=7, lang=lang, balance=0, owner_id=1)
    rel = _build_relationship_menu(partner_id=7, lang=lang, level=10, balance=1_000_000, owner_id=1)
    marry_rows = [b for row in marry.inline_keyboard for b in row]
    rel_rows = [b for row in rel.inline_keyboard for b in row]
    # +1 for the trailing "history" button on each menu.
    assert len(marry_rows) == len(MARRIAGE_ACTIVITIES) + 1
    assert len(rel_rows) == len(RELATIONSHIP_ACTIVITIES) + 1
    assert all(len(b.text) <= _BUTTON_CAPTION_MAX for b in (*marry_rows, *rel_rows))


# ---------------------------------------------------------------------------
# Story line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", _LANGS)
def test_story_line_substitutes_both_names(lang: str) -> None:
    line = _story_line("cinema", lang, actor="Alice", partner="Bob")
    assert "Alice" in line and "Bob" in line
    assert "{actor}" not in line and "{partner}" not in line
    assert line.startswith("💕 🎬 | ")


@pytest.mark.parametrize("lang", _LANGS)
def test_story_line_survives_braces_in_a_display_name(lang: str) -> None:
    """Display names are attacker-controlled and may contain ``{``/``}``.

    Substitution is ``str.replace``, never ``format``, so a name like
    ``{level}`` is inert text rather than a KeyError or a smuggled
    placeholder.
    """
    line = _story_line("hug_act", lang, actor="{level}", partner="}{")
    assert "{level}" in line
    assert "}{" in line


@pytest.mark.parametrize("lang", _LANGS)
def test_story_line_falls_back_for_an_unknown_key(lang: str) -> None:
    line = _story_line("cafe", lang, actor="Alice", partner="Bob")
    assert "Alice" in line and "Bob" in line
    assert "h_couple_act_done_" not in line
