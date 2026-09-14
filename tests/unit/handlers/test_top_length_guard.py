"""Unit tests for the ``/top`` 4096-char body guard.

Telegram rejects a ``sendMessage`` whose rendered text exceeds 4096
chars (a 400 "message is too long"). The count is over the *rendered*
text, not the HTML markup — a ``<a href="tg://user?id=…">Name</a>``
mention counts only as ``Name``. A 50-row leaderboard whose members
each set a 100-char ``/nick`` can blow past the cap, so the renderer
truncates on a row boundary and appends a ``…`` marker.

These tests pin the two pure helpers directly (no Bot/DB needed):

* :func:`visible_len` measures rendered length, not markup length.
* :func:`_join_within_limit` keeps the header, drops overflowing rows,
  and signals truncation.
"""

from __future__ import annotations

from telegram_invite_bot.handlers.top import (
    _TRUNCATION_MARKER,
    _join_within_limit,
)
from telegram_invite_bot.utils.html import (
    TELEGRAM_TEXT_LIMIT,
    html_user_mention,
    visible_len,
)
from telegram_invite_bot.utils.render import parsed_length


def test_visible_len_ignores_mention_markup() -> None:
    """The ``<a href…>`` wrapper is markup Telegram doesn't count —
    visible length must equal the display name length only."""
    line = html_user_mention(123456789, "Alice")
    assert visible_len(line) == len("Alice")


def test_visible_len_unescapes_entities() -> None:
    """``&lt;`` is 4 chars in our string but 1 rendered char — measuring
    the un-escaped form is what keeps the guard from under-counting."""
    line = html_user_mention(1, "<b>")  # escapes to &lt;b&gt;
    assert visible_len(line) == len("<b>")


def test_join_keeps_all_rows_when_under_limit() -> None:
    header = ["HEADER", ""]
    rows = [f"{i}. row" for i in range(10)]
    out = _join_within_limit(header, rows)
    assert _TRUNCATION_MARKER not in out
    for row in rows:
        assert row in out


def test_join_truncates_and_marks_when_over_limit() -> None:
    header = ["HEADER", ""]
    # Each row is ~120 visible chars; 50 of them ≈ 6000 chars > 4096.
    rows = [f"{i:02d}. " + ("Z" * 116) for i in range(50)]
    out = _join_within_limit(header, rows)

    assert out.endswith(_TRUNCATION_MARKER)
    # The rendered body must fit under the cap (over-estimate is fine).
    assert visible_len(out) <= TELEGRAM_TEXT_LIMIT
    # Header always survives; at least the first row fits.
    assert "HEADER" in out
    assert rows[0] in out
    # Not every row made it — proves real truncation, not a no-op.
    assert rows[-1] not in out


def test_join_header_only_when_no_rows() -> None:
    out = _join_within_limit(["HEADER", ""], [])
    assert out == "HEADER\n"
    assert _TRUNCATION_MARKER not in out


# ----- #187: an emoji leaderboard must still fit ----------------------


def test_join_truncates_a_body_that_only_fits_when_emoji_are_miscounted() -> None:
    """The rows below are under the cap by character count and over it by
    Telegram's count.

    A display name is whatever the user set it to, and an emoji costs two
    UTF-16 code units against the 4096 ceiling while costing ``len()``
    one. Thirty rows of a hundred emoji each are ~3 030 characters — the
    guard used to wave all thirty through — and ~6 060 code units, which
    Telegram answers with a 400. Measuring in the unit Telegram measures
    in is the whole job of this guard.
    """
    header = ["HEADER", ""]
    rows = ["🎉" * 100 for _ in range(30)]

    # The premise: character counting says this body is comfortably legal.
    assert sum(len(r) + 1 for r in rows) + len("HEADER") + 1 < TELEGRAM_TEXT_LIMIT

    out = _join_within_limit(header, rows)

    assert out.endswith(_TRUNCATION_MARKER)
    assert parsed_length(out) <= TELEGRAM_TEXT_LIMIT


def test_join_stays_under_the_cap_once_the_marker_is_added() -> None:
    """#543: the marker is appended AFTER the loop, so measuring rows
    against the full cap let the finished body reach 4097 units — the
    exact 400 this helper exists to prevent, in the one case it was
    written for. The header/row sizes below are chosen so the last
    accepted row lands flush on the cap, leaving the marker nowhere to
    go."""
    header = ["HEADE"]  # 5 visible + 1 newline; rows cost 2 each
    rows = ["Z"] * 3000

    out = _join_within_limit(header, rows)

    assert out.endswith(_TRUNCATION_MARKER)
    assert visible_len(out) <= TELEGRAM_TEXT_LIMIT


def test_join_keeps_a_list_that_fills_the_cap_exactly() -> None:
    """The mirror of the fix: the marker is only spent when a row is
    dropped, so the final row is still measured against the full cap.
    Reserving unconditionally would clip a leaderboard that fits and
    then label it truncated."""
    header = ["HEADE"]
    rows = ["Z"] * 2045  # 6 + 2*2045 == 4096 units used exactly

    out = _join_within_limit(header, rows)

    assert _TRUNCATION_MARKER not in out
    assert out.count("\n") == len(rows)
    assert visible_len(out) <= TELEGRAM_TEXT_LIMIT
