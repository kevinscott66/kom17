"""Pure-function guards for the ``/chatstats`` card (RR-1 #6).

The e2e suite drives the handler with ``STATS_TIMEZONE=UTC``, which makes
:func:`~handlers.chatstats._utc_day_bounds` an identity function and
keeps :func:`~handlers.chatstats._fit` far below its ceiling. Both would
therefore stay green if the conversion were deleted or the fitter never
dropped anything, so the two are pinned here directly.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from telegram_invite_bot.handlers.chatstats import (
    _MIN_BLOCKS,
    _NAME_MAX,
    _fit,
    _resolve_name,
    _trim_markup_safe,
    _utc_day_bounds,
)

_MSK = ZoneInfo("Europe/Moscow")


def test_utc_day_bounds_shifts_a_local_day_into_naive_utc() -> None:
    """A Moscow day starts at 21:00 UTC the previous evening.

    ``economy.games.date`` holds naive UTC, so a handler that skipped the
    conversion would count 00:00–03:00 MSK games against the wrong day —
    the exact drift RR-1 #6 fixed. Both bounds must also come back naive:
    an aware bound compares an offset string against an offset-free
    column in SQLite and silently matches nothing.
    """
    start, end = _utc_day_bounds(date(2024, 6, 10), _MSK)
    assert start == datetime(2024, 6, 9, 21, 0)
    assert end == datetime(2024, 6, 10, 21, 0)
    assert start.tzinfo is None
    assert end.tzinfo is None


def test_utc_day_bounds_is_identity_under_utc() -> None:
    start, end = _utc_day_bounds(date(2024, 6, 10), ZoneInfo("UTC"))
    assert (start, end) == (datetime(2024, 6, 10), datetime(2024, 6, 11))


def test_consecutive_days_tile_across_a_dst_transition() -> None:
    """Europe/Berlin loses an hour on 2024-03-31; the days must still
    tile — no overlap (double-counted game) and no hole (lost game)."""
    tz = ZoneInfo("Europe/Berlin")
    _, end_of_30th = _utc_day_bounds(date(2024, 3, 30), tz)
    start_of_31st, end_of_31st = _utc_day_bounds(date(2024, 3, 31), tz)
    start_of_1st, _ = _utc_day_bounds(date(2024, 4, 1), tz)
    assert end_of_30th == start_of_31st
    assert end_of_31st == start_of_1st
    # The lost hour is real: that local day is 23 UTC hours long.
    assert (end_of_31st - start_of_31st).total_seconds() == 23 * 3600


def test_fit_keeps_everything_when_it_already_fits() -> None:
    blocks = [["a"], ["b"], ["c"]]
    assert _fit(blocks, limit=100) == "a\n\nb\n\nc"


def test_fit_drops_whole_trailing_blocks_rather_than_cutting_markup() -> None:
    """An oversized card loses its last block intact, not mid-tag.

    The dropped block is the one appended last (💬 top-3 — the only one
    carrying user text), and every surviving line stays valid HTML,
    which is the whole point: a half-written ``<a href>`` would make
    Telegram reject the entire message.
    """
    tail = '<a href="tg://user?id=1">' + "n" * 200 + "</a>"
    result = _fit([["head"], ["body"], [tail]], limit=60)
    assert result == "head\n\nbody"


def test_fit_never_drops_below_the_floor() -> None:
    """Even an unfittable card keeps :data:`_MIN_BLOCKS` and returns a
    string — degrading the card is right, raising is not."""
    blocks = [["x" * 500], ["y" * 500], ["z" * 500]]
    result = _fit(blocks, limit=10)
    assert len(result) == 10
    assert result.startswith("x")
    # The floor held: the truncation came out of the first two blocks,
    # never from dropping them.
    assert _MIN_BLOCKS == 2


def test_trim_returns_a_short_caption_untouched() -> None:
    """Below the limit there is nothing to repair — not even a rstrip."""
    assert _trim_markup_safe("<b>ok</b> ", 100) == "<b>ok</b> "


def test_trim_retreats_out_of_a_split_tag() -> None:
    """A cut inside ``<b`` leaves a ``<`` Telegram reads as a tag start."""
    assert _trim_markup_safe("abc<b>def", 5) == "abc"


def test_trim_retreats_out_of_a_split_entity() -> None:
    """``&#x27;`` is what ``html.escape`` makes of an apostrophe in a
    display name, so a cut landing inside one is the likely case, not an
    exotic one."""
    assert _trim_markup_safe("ab&#x27;cd", 5) == "ab"


def test_trim_drops_an_anchor_it_cannot_close() -> None:
    """The opening tag survived the cut intact but ``</a>`` did not.

    Nothing later in the pipeline closes it, so the whole card is
    rejected. Retreating to before the anchor costs one row and keeps
    the other five.
    """
    caption = 'top:\n<a href="tg://user?id=1">Name</a>'
    # 33 = the 5-char prefix plus the 25-char opening tag plus "Nam".
    assert _trim_markup_safe(caption, 33) == "top:"


def test_trim_never_leaves_broken_markup_at_any_limit() -> None:
    """The property the three retreats exist to hold, swept across every
    cut point of a caption built the way ``_render`` builds one."""
    caption = (
        "\U0001f4ac \u0442\u043e\u043f-3:\n"
        '1. <a href="tg://user?id=42">O&#x27;Brien</a> \u2014 10\n'
        '2. <a href="tg://user?id=43">Bob</a> \u2014 5'
    )
    for limit in range(1, len(caption) + 1):
        head = _trim_markup_safe(caption, limit)
        assert len(head) <= limit
        assert head.rfind("<") <= head.rfind(">")
        assert head.rfind("&") <= head.rfind(";")
        assert head.count("<a ") == head.count("</a>")


class _FakeBot:
    def __init__(self, first_name: str | None) -> None:
        self._first_name = first_name

    async def get_chat_member(self, _chat_id: int, _user_id: int) -> Any:
        return SimpleNamespace(user=SimpleNamespace(first_name=self._first_name))


async def test_resolve_name_caps_a_hostile_display_name() -> None:
    """Telegram allows 64 chars; escaping can multiply that sixfold.

    Three uncapped names of that shape overflow the 1024-char caption
    and would silently cost every reader the 💬 block.
    """
    name = await _resolve_name(_FakeBot("'" * 64), -100, 7)  # type: ignore[arg-type]
    assert name == "'" * _NAME_MAX


async def test_resolve_name_falls_back_to_the_id_when_the_lookup_fails() -> None:
    class _Broken(_FakeBot):
        async def get_chat_member(self, _chat_id: int, _user_id: int) -> Any:
            raise RuntimeError("boom")

    assert await _resolve_name(_Broken(None), -100, 7) == "ID7"  # type: ignore[arg-type]
    # A member with a blank profile name is the same deterministic case.
    assert await _resolve_name(_FakeBot("   "), -100, 7) == "ID7"  # type: ignore[arg-type]
