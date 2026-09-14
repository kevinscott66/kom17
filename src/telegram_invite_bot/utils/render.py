"""Small shared display/render helpers."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def on_off_text(value: bool, lang: str) -> str:  # noqa: FBT001 — display helper
    """Localised on/off label: ``вкл``/``выкл`` (ru), ``on``/``off`` (en)."""
    if lang == "ru":
        return "вкл" if value else "выкл"
    return "on" if value else "off"


# Language-neutral state marks. Shared by the /groupadmin card, its
# settings readout and its settings KEYBOARD — a toggle's button label
# and the card line above it must agree, and they live in different
# modules, so the glyphs belong here rather than in either of them.
# They need no i18n and satisfy the EN zero-Cyrillic rule trivially.
MARK_ON = "✅"
MARK_OFF = "❌"


def state_mark(value: bool) -> str:  # noqa: FBT001 — display helper, like on_off_text
    """``✅``/``❌`` for a boolean setting."""
    return MARK_ON if value else MARK_OFF


# Telegram's hard ceiling for one message's text.
TELEGRAM_TEXT_LIMIT = 4096
# The other three body ceilings the Bot API enforces. They are much
# lower than the text one, and treating them as 4096 (as the length
# guard first did) means a caption at 2 000 characters reads as fine
# and is rejected anyway. A media caption gets a quarter of a message;
# a poll question a fourteenth; a callback toast a twentieth.
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_POLL_QUESTION_LIMIT = 300
TELEGRAM_CALLBACK_ANSWER_LIMIT = 200
# Default per-page budget. Mirrors the broadcast draft cap
# (``handlers.broadcast._TEXT_MAX_LEN``) and leaves the ceiling a wide
# margin for a header or a trailing hint the caller adds later.
PAGE_BUDGET = 3500
# Default ceiling on pages. A list renderer that hits it is a list the
# user was never going to read anyway; the point is that an admin
# command can't turn into an unbounded burst of messages.
PAGE_MAX = 5

_TAG_RE = re.compile(r"<[^>]+>")


def utf16_length(text: str) -> int:
    """Length Telegram measures for a plain string.

    Telegram counts UTF-16 code units, not Python code points: an emoji
    outside the BMP is ONE ``len()`` character but TWO units against the
    4096 ceiling. Handlers that cap free text with ``len()`` therefore
    under-count, and an emoji-per-line card is the case where the gap
    eats a 96-character margin whole.
    """
    return len(text.encode("utf-16-le")) // 2


def clamp_utf16(text: str, limit: int) -> str:
    """Trim raw (unescaped) text to at most ``limit`` measured units.

    Cuts between characters, never between the two halves of a surrogate
    pair — a lone surrogate is not encodable and the send would fail on
    exactly the input the clamp exists to rescue.
    """
    if utf16_length(text) <= limit:
        return text
    used = 0
    kept: list[str] = []
    for char in text:
        cost = 2 if ord(char) > 0xFFFF else 1
        if used + cost > limit:
            break
        kept.append(char)
        used += cost
    return "".join(kept)


def parsed_length(text: str) -> int:
    """Length Telegram measures for an HTML-parse-mode message.

    The 4096 ceiling applies to the text AFTER entity parsing: ``<b>``
    and ``<code>`` become entities and cost nothing, and ``&amp;`` is
    delivered as one character. Measuring ``len()`` on the markup we
    build overstates a list of escaped words by up to 6x.

    Safe on our own rendered lines specifically: every variable part is
    run through :func:`html.escape` before it lands in a template, so a
    literal ``>`` inside user text is already ``&gt;`` and cannot be
    mistaken for the end of a tag.

    This is the only implementation of this count in the codebase, and it
    has one alias: :func:`telegram_invite_bot.utils.html.visible_len`
    calls straight through to it, so ``/top``, ``/help``, ``/profile``
    and the outgoing-length middleware all get the same answer for the
    same string. Before #187 and #302 there were three separate copies
    and one of them was wrong; a fourth belongs here, not next to its
    caller.
    """
    return utf16_length(html.unescape(_TAG_RE.sub("", text)))


def paginate_lines(
    header: str,
    lines: Sequence[str],
    *,
    more_line: Callable[[int], str],
    budget: int = PAGE_BUDGET,
    max_pages: int = PAGE_MAX,
) -> list[str]:
    """Split a rendered list into messages Telegram will accept.

    Any command that renders one line per stored row is a 400 waiting
    for the row count to grow: ``/filter_list`` (500 words) and
    ``/aliases`` (100 mappings) both pass 4096 characters well inside
    the ceilings their own add-paths enforce, and the failure is silent
    — the admin sees no answer at all.

    Splitting rather than truncating is deliberate: these are the
    surfaces that exist to show the WHOLE list (the /groupadmin Words
    panel literally says "показать все: /filter_list"), so dropping the
    tail would defeat the command. ``more_line`` is only reached in the
    pathological case where ``max_pages`` pages still cannot hold the
    list; it receives the number of unrendered items.

    ``header`` goes on the first page only. ``lines`` must be rendered
    HTML — budgeting happens on :func:`parsed_length`, so callers do not
    have to reason about escaping expansion.
    """
    pages: list[str] = []
    page: list[str] = [header]
    used = parsed_length(header)
    # Lines on the current page with the header excluded. The escape
    # below has to count BODY lines: ``header`` goes on page one only,
    # so a continuation page starts empty and the old ``len(page) > 1``
    # test demanded TWO lines there before it would break — letting the
    # second one land past ``budget`` on every page after the first.
    body = 0
    for position, line in enumerate(lines):
        # +1 for the newline that joins this line to the previous one.
        cost = parsed_length(line) + 1
        # ``body`` keeps a single over-budget line on a page of its own
        # rather than looping on an empty page forever.
        if used + cost > budget and body:
            if len(pages) + 1 == max_pages:
                page.append(more_line(len(lines) - position))
                break
            pages.append("\n".join(page))
            page = []
            used = 0
            body = 0
        page.append(line)
        used += cost
        body += 1
    pages.append("\n".join(page))
    return pages
