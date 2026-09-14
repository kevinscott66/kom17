"""Regression guard: the card background is built once, not per render.

:func:`render_stats_card` used to paint its gradient by walking every
one of the card's 760 rows and issuing an ``ImageDraw.line`` per row::

    for y in range(height):
        c = (16 + y // 24, 22 + y // 24, 30 + y // 22)
        draw.line([(0, y), (width, y)], fill=c)

Every caller offloads the render with ``asyncio.to_thread``
(``handlers/profile.py``, ``handlers/stats.py``,
``handlers/chatstats.py``) and says so in a comment — "keep the event
loop free while Pillow rasterises". For the expensive half of the
render that is true: ``img.save(format="PNG")`` runs zlib, which
releases the GIL. For this loop it was not. 760 iterations of Python
bytecode plus 760 bound-method calls hold the GIL and yield only every
``sys.setswitchinterval()``, so the thread the offload created competed
with the event loop instead of relieving it — measured here at ~1.6 ms
of GIL-held work per render against ~0.14 ms for a copy of a prebuilt
image, and the loop lag it caused grew with the number of concurrent
renders.

Nothing about the gradient depends on the card's contents: it is a
function of ``height`` alone, and no argument reaches it. So it is
built once per size and copied.

What this file pins is the shape, because the shape is what a refactor
loses: a render must not redraw the background row by row, the pixels
must be the ones the old loop produced, and the shared image must not
pick up one card's contents and hand them to the next.
"""

from __future__ import annotations

import io
from datetime import datetime

import pytest
from PIL import Image, ImageDraw

from telegram_invite_bot.utils.stats_image import render_stats_card

_NOW = datetime(2026, 9, 11, 12, 0, 0)
_ROWS: list[tuple[str, int]] = [("альфа", 10), ("бета", 5), ("гамма", 1)]
#: Left of every panel (they start at x=28) and below the header band,
#: so these pixels are background and nothing else.
_MARGIN_X = 5
_SAMPLE_Y = (100, 300, 600, 750)


def _card(title: str = "Карточка") -> bytes:
    return render_stats_card(title, _ROWS, subtitle="подпись", generated_at=_NOW)


def test_a_render_does_not_repaint_the_gradient_row_by_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-render, GIL-held loop must be gone — not merely faster.

    Counting the calls rather than timing them: a timing assertion on a
    1.6 ms loop is a flake generator, while the call count is exactly
    the property the fix establishes and is stable on any machine. On
    the unfixed renderer this is 760 — one line per row of the card.
    """
    # The very first card of the process legitimately paints the
    # gradient — once, into the cache. What must never happen again is
    # the SECOND card paying for it, so warm the cache first and then
    # count. On the unfixed renderer the warm-up changes nothing: every
    # render repaints.
    _card()

    calls = 0
    original = ImageDraw.ImageDraw.line

    def counting_line(self: ImageDraw.ImageDraw, *args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ImageDraw.ImageDraw, "line", counting_line)
    _card()
    monkeypatch.undo()

    assert calls == 0, (
        f"the background was repainted with {calls} line() calls inside the render — "
        "that is Python bytecode holding the GIL in a thread that exists to free the loop"
    )


def test_the_gradient_is_the_one_the_loop_drew() -> None:
    """Prebuilding must not change a single pixel of the card.

    The formula is the legacy one, asserted against the image rather
    than against a copy of the expression, so a background rebuilt the
    wrong way round (or at the wrong size) fails here.
    """
    card = Image.open(io.BytesIO(_card())).convert("RGB")
    assert card.size == (1200, 760)
    for y in _SAMPLE_Y:
        expected = (16 + y // 24, 22 + y // 24, 30 + y // 22)
        assert card.getpixel((_MARGIN_X, y)) == expected, f"background changed at y={y}"


def test_one_card_does_not_leak_into_the_next() -> None:
    """A shared background is only safe if every render copies it.

    Drawn on directly, the second card would carry the first card's
    title and rows underneath its own. Two renders of the same input
    must also stay byte-identical — the failure mode there is a
    background that accumulates.
    """
    first = _card("Первая")
    other = _card("Вторая")
    again = _card("Первая")

    assert first != other
    assert first == again, "a second render of the same card differs — the background accumulated"
