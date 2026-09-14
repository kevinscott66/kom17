"""Live stats-card PNG generator (T-024.4 — profile preview restoration).

The legacy monolith rendered the user profile as a freshly-generated
PNG "stats card" (``bot.py:create_stats_preview_image`` / ``get_pil_font``):
a dark gradient panel with a title bar, an optional subtitle, up to a
handful of labelled rows each drawn as ``label … value …
proportional-bar``, and a "generated at" timestamp in the corner. The
card is re-rendered on every view, so the numbers and the timestamp
are always *live as of this minute* — that's the "real-time preview"
users expect from ``/profile``.

This module re-ports that generator into the new pipeline, trimmed to
its load-bearing core:

* No watermark overlay (the legacy watermark pulled a Telegram
  ``file_id`` download into image generation — out of scope here).
* No emoji-as-image compositing — truetype text rendering covers the
  Latin/Cyrillic labels we feed it; emoji in labels degrade to the
  font's glyph rather than a colour image, which is fine for a stats
  card.

Pillow is already a runtime dependency (``pyproject.toml`` →
``pillow>=11``), so this adds no new install. Generation is pure-CPU
and synchronous; callers on the asyncio loop should wrap it in
``asyncio.to_thread`` if they render large batches, but a single
profile card (~30 ms) is fine inline.
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from datetime import datetime

# Font search path — mirrors legacy ``get_pil_font``. Cyrillic-capable
# truetype faces across macOS / Debian / Arch / generic Linux. The
# first that exists wins; ``load_default`` is the last resort.
_FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)

# Module-level cache: truetype face objects are reusable and cheap to
# keep, but expensive to re-open per render. Keyed by (path, size).
#
# Unbounded by type, bounded in fact (checked against the #74 growth
# class): both halves of the key have fixed cardinality — the path comes
# from ``_FONT_CANDIDATES``, the size from the literals in
# :func:`render_stats_card`. Keep it that way; a size derived from user
# input would turn this into a leak, and it would need the same TTL/LRU
# treatment as the rank caches.
_FONT_CACHE: dict[tuple[str | None, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def get_pil_font(size: int = 20) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A Cyrillic-capable truetype font at ``size`` px, or the bundled
    default if no system face is found.

    Pillow ≥10 ``load_default(size)`` returns a scalable face, so even
    the fallback renders at the requested size rather than the old
    fixed-8px bitmap.
    """
    for path in _FONT_CANDIDATES:
        key = (path, size)
        cached = _FONT_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            if Path(path).exists():
                font = ImageFont.truetype(path, size)
                _FONT_CACHE[key] = font
                return font
        except OSError:
            continue
    default_key = (None, size)
    cached_default = _FONT_CACHE.get(default_key)
    if cached_default is not None:
        return cached_default
    try:
        default = ImageFont.load_default(size)
    except TypeError:  # pragma: no cover - Pillow <10 has no size arg
        default = ImageFont.load_default()
    _FONT_CACHE[default_key] = default
    return default


@lru_cache(maxsize=4)
def _background(width: int, height: int) -> Image.Image:
    """The card's vertical gradient, built once per size.

    Painted row by row, which is 760 iterations of Python bytecode and
    760 bound-method calls for one card. That cost used to be paid on
    every render, and paid in the worst possible place: all three
    callers hand the render to ``asyncio.to_thread`` to keep the event
    loop free, but a Python loop holds the GIL and yields only every
    ``sys.setswitchinterval()`` — so the thread competed with the loop
    instead of relieving it, and the stall grew with the number of
    people rendering at once. (The genuinely expensive half, the PNG
    encode, has always been fine: zlib releases the GIL.)

    Nothing here depends on the card's contents — the colour is a
    function of ``y`` alone — so there is nothing to rebuild. Bounded
    by construction: the only size any caller asks for is the one
    literal in :func:`render_stats_card`.

    The returned image is SHARED. Callers must ``copy()`` it before
    drawing; :func:`render_stats_card` does, and
    ``tests/regression/test_stats_card_background_is_prebuilt.py``
    keeps it honest.
    """
    canvas = Image.new("RGB", (width, height), color=(16, 22, 30))
    draw = ImageDraw.Draw(canvas)
    for y in range(height):
        draw.line([(0, y), (width, y)], fill=(16 + y // 24, 22 + y // 24, 30 + y // 22))
    return canvas


def render_stats_card(
    title: str,
    rows: list[tuple[str, int]],
    *,
    subtitle: str = "",
    generated_at: datetime,
) -> bytes:
    """Render a dark stats card and return the PNG bytes.

    ``rows`` is ``[(label, value), ...]`` — at most 9 are drawn (the
    legacy ceiling); each becomes a panel with the label, the integer
    value, and a horizontal bar proportional to the largest value in
    the set. ``generated_at`` is stamped bottom-left so the card is
    self-documenting about *when* the snapshot was taken.
    """
    width, height = 1200, 760
    img = _background(width, height).copy()
    draw = ImageDraw.Draw(img)
    font_title = get_pil_font(46)
    font_sub = get_pil_font(24)
    font_row = get_pil_font(28)

    safe_rows: list[tuple[str, int]] = [
        (str(label)[:30], int(value) if value is not None else 0) for label, value in rows[:9]
    ]
    max_value = max((v for _, v in safe_rows), default=1) or 1

    # Header band + title.
    draw.rectangle((0, 0, width, 92), fill=(34, 45, 60))
    draw.text((30, 26), (title or "")[:60], fill=(245, 245, 245), font=font_title)
    if subtitle:
        draw.text((30, 108), subtitle[:80], fill=(180, 190, 200), font=font_sub)

    start_y = 160
    bar_x0 = 560
    bar_x1 = width - 48
    row_h = 64
    for idx, (label, value) in enumerate(safe_rows, 1):
        y0 = start_y + (idx - 1) * row_h
        y1 = y0 + 50
        draw.rectangle((28, y0, width - 32, y1), fill=(29, 38, 52), outline=(60, 80, 110), width=1)
        draw.text((42, y0 + 10), f"{idx}. {label}", fill=(232, 236, 241), font=font_row)
        draw.text((460, y0 + 10), str(value), fill=(255, 216, 130), font=font_row)

        progress = (value / max_value) if max_value > 0 else 0
        bx = int(bar_x0 + (bar_x1 - bar_x0) * progress)
        draw.rectangle((bar_x0, y0 + 12, bar_x1, y0 + 36), fill=(45, 60, 82))
        if bx > bar_x0:
            draw.rectangle((bar_x0, y0 + 12, bx, y0 + 36), fill=(92, 170, 255))

    draw.text(
        (30, height - 36),
        generated_at.strftime("%d.%m.%Y %H:%M"),
        fill=(120, 130, 145),
        font=font_sub,
    )

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
