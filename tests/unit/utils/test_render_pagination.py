"""``paginate_lines`` / ``parsed_length`` — the shared 4096 guard.

Both list commands that outgrew Telegram's ceiling (``/filter_list``,
``/aliases``) now budget through these two helpers, so the subtle parts
are pinned here once rather than re-derived in each handler's suite:

* the budget is measured on the text Telegram parses, not on the HTML we
  build — entities are free and ``&amp;`` is one character;
* a page break never eats or duplicates a line;
* ``max_pages`` bounds the burst, and the last page says how much was
  left rather than dropping it silently;
* a single line too big for the budget still goes out instead of
  spinning on an empty page.
"""

from __future__ import annotations

from telegram_invite_bot.utils.render import (
    PAGE_BUDGET,
    clamp_utf16,
    paginate_lines,
    parsed_length,
    utf16_length,
)

_MORE = "…ещё {count}"


def _more(count: int) -> str:
    return _MORE.format(count=count)


def test_utf16_length_charges_two_units_for_an_emoji() -> None:
    """Telegram's ruler, not Python's: 🔥 is one char and two units."""
    assert utf16_length("abc") == 3
    assert utf16_length("Правила") == 7
    assert utf16_length("🔥") == 2
    assert utf16_length("🔥x") == 3


def test_parsed_length_uses_the_same_ruler() -> None:
    """An emoji inside markup still costs two."""
    assert parsed_length("<b>🔥</b>") == 2


def test_clamp_utf16_leaves_a_short_string_alone() -> None:
    assert clamp_utf16("🔥abc", 10) == "🔥abc"


def test_clamp_utf16_never_splits_a_surrogate_pair() -> None:
    """An odd budget must drop the whole emoji, not half of it."""
    clamped = clamp_utf16("🔥🔥🔥", 5)

    assert clamped == "🔥🔥"
    # A lone surrogate cannot be encoded at all — the send would fail on
    # exactly the input the clamp exists to rescue.
    assert clamped.encode("utf-8")


def test_clamp_utf16_measures_in_units_not_characters() -> None:
    assert clamp_utf16("🔥" * 100, 10) == "🔥" * 5


def test_parsed_length_ignores_markup() -> None:
    """Tags become entities; they cost nothing toward the ceiling."""
    assert parsed_length("<b>abc</b>") == 3
    assert parsed_length("<code>ab</code> → <code>cd</code>") == 7


def test_parsed_length_counts_an_escaped_entity_once() -> None:
    """``&amp;`` is five characters of HTML and one of message text."""
    assert parsed_length("&amp;") == 1
    assert parsed_length("&lt;b&gt;") == 3


def test_a_short_list_is_one_page() -> None:
    pages = paginate_lines("head", ["a", "b", "c"], more_line=_more)

    assert pages == ["head\na\nb\nc"]


def test_a_long_list_splits_without_losing_a_line() -> None:
    lines = [f"line-{i:04d}" for i in range(1000)]

    pages = paginate_lines("head", lines, more_line=_more, max_pages=100)

    assert len(pages) > 1
    assert all(parsed_length(page) <= PAGE_BUDGET for page in pages)
    body = "\n".join(pages).splitlines()
    assert body[0] == "head"
    assert body[1:] == lines


def test_the_page_ceiling_is_reported_not_swallowed() -> None:
    """Hitting ``max_pages`` must say how many rows never rendered."""
    lines = [f"line-{i:04d}" for i in range(1000)]

    pages = paginate_lines("head", lines, more_line=_more, max_pages=2)

    assert len(pages) == 2
    rendered = [line for page in pages for line in page.splitlines()]
    rendered.remove("head")
    tail = rendered.pop()
    dropped = len(lines) - len(rendered)
    assert dropped > 0
    assert tail == _more(dropped)
    assert rendered == lines[: len(rendered)]


def test_one_oversized_line_still_goes_out() -> None:
    """A line past the budget must not loop on an empty page."""
    pages = paginate_lines("head", ["x" * (PAGE_BUDGET * 2)], more_line=_more)

    assert len(pages) == 1
    assert pages[0].endswith("x" * 10)


def test_a_continuation_page_respects_the_budget_too() -> None:
    """#542: ``header`` goes on page one only, so every later page starts
    empty — and the old ``len(page) > 1`` escape therefore demanded TWO
    lines before it would break, letting the second one land past the
    budget on every page after the first. Page one was never affected,
    which is why a uniform-line split test never saw it."""
    lines = ["x" * 12] * 6

    pages = paginate_lines("head", lines, more_line=_more, budget=20, max_pages=100)

    assert len(pages) > 2
    assert all(parsed_length(page) <= 20 for page in pages)
    body = "\n".join(pages).splitlines()
    assert body[0] == "head"
    assert body[1:] == lines
