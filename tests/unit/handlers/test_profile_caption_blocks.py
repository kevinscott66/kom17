"""Pure-function guards for the RR-1 #3 ``/profile`` caption blocks.

The e2e suite renders short cards for a user with a two-word name and a
handful of audit rows, so it exercises neither the caption ceiling nor
the timezone conversion. Both are pinned here directly: a card that
silently exceeds 1024 is rejected by Telegram in full, and a stamp shown
in the wrong zone is wrong in a way no assertion on "some digits" would
notice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from telegram_invite_bot.handlers.profile import (
    _CAPTION_MAX,
    _CAPTION_MIN_LINES,
    _caption_len,
    _clamp_caption,
    _log_action_label,
    _mod_action_lines,
)
from telegram_invite_bot.repositories.moderation_repo import ActionRow

_MSK = ZoneInfo("Europe/Moscow")


def test_caption_len_ignores_markup() -> None:
    """Telegram parses the HTML and measures the *result*.

    Counting the raw string instead over-states a real card by hundreds
    of characters, and the tail the clamp would then shed is exactly the
    restored moderation history.
    """
    assert _caption_len("<b>abc</b>") == 3
    assert _caption_len('<a href="tg://user?id=1">Ann</a>') == 3


def test_caption_len_counts_entities_as_the_character_they_become() -> None:
    """``&lt;`` is four bytes on the wire and one character to Telegram."""
    assert _caption_len("&lt;b&gt;") == 3


def test_caption_len_counts_emoji_as_two_units() -> None:
    """The limit is in UTF-16 code units, so astral characters are two.

    A display name may legally be nothing but emoji, and counting code
    points there lets a caption past the guard that Telegram then
    rejects in full.
    """
    assert _caption_len("🎉") == 2
    assert _caption_len("<b>🎉🎉</b>") == 4


def test_clamp_measures_the_parsed_length_not_the_raw_string() -> None:
    """A heavily-marked-up card that fits keeps all of its lines."""
    lines = ["title", "name", "id", *[f"<b>{'x' * 10}</b>" for _ in range(4)]]
    assert _clamp_caption(lines, limit=60).splitlines() == lines


def test_clamp_catches_an_emoji_card_that_a_naive_count_would_pass() -> None:
    """64 emoji is 64 code points but 128 caption units — the case where
    counting characters says "fits" and Telegram says ``CAPTION_TOO_LONG``."""
    lines = ["title", "name", "id", "🎉" * 64]
    result = _clamp_caption(lines, limit=100)
    assert _caption_len(result) <= 100
    assert "🎉" not in result


def test_clamp_output_always_fits_the_real_ceiling() -> None:
    lines = ["title", "name", "id", *[f"<b>{'ё' * 60}</b>" for _ in range(40)]]
    assert _caption_len(_clamp_caption(lines)) <= _CAPTION_MAX


def test_clamp_never_strands_a_block_header() -> None:
    """Dropping every row under a header must drop the header too — a
    card ending in "📋 Последние действия:" with nothing beneath reads
    as a bug rather than as truncation."""
    lines = ["title", "name", "id", "📋 <b>Последние действия:</b>", "  • бан: spam"]
    assert _clamp_caption(lines, limit=30) == "title\nname\nid"


def test_clamp_keeps_a_card_that_already_fits() -> None:
    assert _clamp_caption(["a", "b", "c", "d"], limit=100) == "a\nb\nc\nd"


def test_clamp_drops_whole_trailing_lines_not_characters() -> None:
    """An oversized card loses its tail intact.

    Cutting mid-string could leave a dangling ``<a href=`` and make
    Telegram reject the entire caption — a slightly-too-long card would
    become no card at all.
    """
    lines = ["title", "name", "id", "x" * 40, '<a href="tg://user?id=1">y</a>']
    # 55, not 60: the limit is compared against the *parsed* length, and
    # the anchor line parses down to a single character.
    result = _clamp_caption(lines, limit=55)
    assert result == "title\nname\nid\n" + "x" * 40
    assert "<a href" not in result


def test_clamp_never_strands_a_blank_separator_at_the_end() -> None:
    lines = ["title", "name", "id", "", "z" * 80]
    assert _clamp_caption(lines, limit=40) == "title\nname\nid"


def test_clamp_keeps_the_identity_block_even_when_it_cannot_fit() -> None:
    """A card whose first three lines alone overflow is a bug at the
    source; emitting a nameless stub would hide it, not fix it."""
    lines = ["t" * 500, "n" * 500, "i" * 500, "extra"]
    result = _clamp_caption(lines, limit=10)
    assert len(result.splitlines()) == _CAPTION_MIN_LINES


def test_unknown_actions_fall_back_to_a_generic_label() -> None:
    """A raw column value must never reach the card — an action added by
    a future handler renders as "действие", not as ``shadowban``."""
    assert _log_action_label("shadowban", "ru") == "действие"
    assert _log_action_label("ban", "ru") == "бан"
    assert _log_action_label("ban", "en") == "ban"


def test_unwarn_has_its_own_label() -> None:
    """Prod's audit log contains ``unwarn`` rows, and it is the block's
    most common piece of good news — it must not render as "действие"."""
    assert _log_action_label("unwarn", "ru") == "снятие предупреждения"
    assert _log_action_label("unwarn", "en") == "warning lifted"


def test_mod_action_lines_are_empty_without_rows() -> None:
    assert _mod_action_lines([], "ru", datetime.now(UTC)) == []


def test_mod_action_stamps_are_converted_to_the_card_timezone() -> None:
    """Audit rows are stored naive-UTC; the card speaks the configured
    display zone. 21:30 UTC is 00:30 the *next day* in Moscow — the case
    where a naive render is wrong about the date, not just the hour."""
    rows = [ActionRow(action="ban", reason="spam", date=datetime(2024, 6, 9, 21, 30))]
    lines = _mod_action_lines(rows, "ru", datetime(2024, 6, 10, 12, 0, tzinfo=_MSK))
    assert lines[1] == "  • бан (10.06.2024 00:30): spam"


def test_mod_action_reasons_are_truncated_then_escaped() -> None:
    """A moderator's free-form text is the only attacker-controlled part
    of the block: a bare ``<`` would be parsed as a tag and take the
    whole caption down with it."""
    rows = [
        ActionRow(
            action="mute",
            reason="<b>" + "q" * 80,
            date=datetime(2024, 6, 10, 9, 0),
        )
    ]
    lines = _mod_action_lines(rows, "ru", datetime(2024, 6, 10, 12, 0, tzinfo=UTC))
    assert lines[1] == "  • мут (10.06.2024 09:00): &lt;b&gt;" + "q" * 47
    assert "<b>" not in lines[1]


def test_a_blank_reason_renders_as_an_em_dash() -> None:
    rows = [ActionRow(action="warn", reason="   ", date=datetime(2024, 6, 10, 9, 0))]
    lines = _mod_action_lines(rows, "ru", datetime(2024, 6, 10, 12, 0, tzinfo=UTC))
    assert lines[1].endswith(": —")
