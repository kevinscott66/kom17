"""Markdown → HTML for the public guide pages.

Replaces the legacy renderer that :mod:`rendering` carried verbatim from
``guide_site.py``. That renderer existed to keep the Flask and FastAPI
pipelines byte-identical while both served the page; the Flask side is
gone (the legacy host was released), so the parity contract now buys
nothing and costs a lot — it froze four defects into the visual
contract:

* ``[текст](#anchor)`` was reduced to ``текст``, which silently killed
  the guide's own table of contents. Ten "sections" that look like
  links and do nothing is worse than no TOC at all.
* ``{#start}`` anchor syntax was printed literally, so every section
  heading ended with visible ``{#tips}`` noise AND had no id to jump to.
* ``##`` and ``###`` both collapsed to ``<h4>``, flattening a
  three-level document into one, which is what made the page feel like
  an undifferentiated wall of text.
* ``- item`` became ``<p>• item</p>``, so a screen reader announced a
  list of nineteen items as nineteen unrelated paragraphs.

Still a hand-rolled subset rather than a markdown dependency, for the
original and still-good reason: the guides use headings, bullets,
ordered lists, emphasis, inline code, links and rules — nothing else.
A parser would add a dependency and a CVE surface for no feature.

Security: every value is HTML-escaped before any tag is emitted, and
link targets are filtered by scheme (:data:`_SAFE_SCHEMES`) so a
``javascript:`` URL in the markdown cannot become a live link. This
matters because ``/commands/edit`` lets an operator paste markdown into
a form — the renderer treats its input as untrusted by construction.
"""

from __future__ import annotations

import html as html_lib
import re
import unicodedata
from typing import TYPE_CHECKING, Final, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

#: URL schemes allowed to become an ``<a href>``. Everything else is
#: rendered as plain text — an unlinked URL is a cosmetic loss, a live
#: ``javascript:`` URL is a stored-XSS hole on a page the bot links
#: every user to.
_SAFE_SCHEMES: Final[tuple[str, ...]] = ("https://", "http://", "tg://", "mailto:", "/", "#")

#: Second characters that turn a leading ``/`` into an *authority* rather
#: than a path. ``//host`` is the scheme-relative form, and browsers
#: normalise the backslash variants to it as well (WHATWG URL, "relative
#: slash state") — ``/\host`` navigates to ``host``, not to a local path
#: named ``\host``. See :func:`_safe_url` (#214).
_AUTHORITY_STARTS: Final[tuple[str, ...]] = ("/", "\\")

#: ``## Heading {#anchor}`` — the explicit-id syntax the shipped guides
#: already use. Captured and stripped; without it we slugify the text.
_ANCHOR_RE: Final[re.Pattern[str]] = re.compile(r"\s*\{#([A-Za-z0-9_-]+)\}\s*$")

_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE: Final[re.Pattern[str]] = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_CODE_RE: Final[re.Pattern[str]] = re.compile(r"`([^`]+)`")
_ORDERED_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)\.\s+(.*)$")


class Heading(NamedTuple):
    """One entry of the generated table of contents."""

    level: int
    text: str
    anchor: str


def slugify(text: str) -> str:
    """A URL-fragment-safe id for a heading with no explicit anchor.

    Transliteration is deliberately NOT attempted: a lossy ru→en map
    produces ids nobody can predict, and every browser in use handles a
    percent-encoded Cyrillic fragment correctly. Non-word characters
    (including the emoji every section heading opens with) collapse to
    hyphens, so ``## 📦 Раздел 7. Мелочи`` becomes ``раздел-7-мелочи``.
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    slug = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE).strip("-")
    return slug or "section"


def _safe_url(raw: str) -> str | None:
    """The href, or None when the scheme isn't allow-listed.

    The input arrives **already escaped** — :func:`render_inline` escapes
    the whole line before any regex runs, which is what makes the
    escape-first rule hold. Escaping again here would turn a legitimate
    ``?x=&quot;y`` into ``?x=&amp;quot;y`` and break the link.

    ``/`` is on the allow-list to mean *this site*, and that is the only
    thing it is allowed to mean. ``//host`` (and the ``/\\host`` variants
    browsers fold into it) also start with ``/`` while pointing at a
    different origin entirely, so they used to pass the check and then
    be classified as same-site by :func:`_render_link` — rendered
    without ``target``/``rel``, indistinguishable in the markup from a
    link to our own page. Off-site under the allow-list means "write the
    scheme"; a scheme-relative URL is refused, like any other URL whose
    scheme we cannot see (#214).
    """
    url = raw.strip()
    if not url.startswith(_SAFE_SCHEMES):
        return None
    if url.startswith("/") and url[1:2] in _AUTHORITY_STARTS:
        return None
    return url


def _render_link(match: re.Match[str]) -> str:
    label, url = match.group(1), match.group(2)
    href = _safe_url(url)
    if href is None:
        return label
    # In-page anchors and same-site paths stay in the tab; anything
    # off-site opens in a new one, because a user who taps a reference
    # link mid-guide should not lose their scroll position.
    if href.startswith(("#", "/")):
        return f'<a href="{href}">{label}</a>'
    return f'<a href="{href}" target="_blank" rel="noopener noreferrer">{label}</a>'


def render_inline(line: str) -> str:
    """Inline markdown for one already-trimmed line.

    Escape first, always — every later step inserts tags, so anything
    that escaped after them could be smuggled through by the markdown
    source. Links resolve before emphasis so a bolded link label works;
    code resolves last so ``**`` inside backticks is left alone.
    """
    out = html_lib.escape(line)
    out = _LINK_RE.sub(_render_link, out)
    out = _BOLD_RE.sub(r"<b>\1</b>", out)
    out = _ITALIC_RE.sub(r"<i>\1</i>", out)
    return _CODE_RE.sub(r"<code>\1</code>", out)


def _split_heading(stripped: str) -> tuple[int, str] | None:
    for level, prefix in ((1, "# "), (2, "## "), (3, "### "), (4, "#### ")):
        if stripped.startswith(prefix):
            return level, stripped[len(prefix) :]
    return None


def _heading_parts(stripped: str) -> tuple[int, str, str] | None:
    """``(level, text, anchor)`` for a heading line, else None.

    ``#`` maps to ``<h2>``: the page's single ``<h1>`` lives in the
    hero, and a document with two ``<h1>``s is a heading-hierarchy
    violation that screen readers and SEO both punish. The offset is
    applied by the caller so this function stays about markdown.
    """
    parsed = _split_heading(stripped)
    if parsed is None:
        return None
    level, raw_text = parsed
    anchor_match = _ANCHOR_RE.search(raw_text)
    if anchor_match:
        return level, raw_text[: anchor_match.start()].strip(), anchor_match.group(1)
    text = raw_text.strip()
    return level, text, slugify(text)


def extract_headings(md_text: str, *, min_level: int = 2) -> tuple[Heading, ...]:
    """Headings in document order — the input for the sidebar TOC.

    ``min_level`` defaults to 2 because the guide's markdown opens with
    a ``#`` document title: it is already rendered in the hero, and
    listing it in its own table of contents is noise. Documents that
    have no title line of their own — the legal texts, whose title
    comes from :class:`~telegram_invite_bot.cms.legal.documents.LegalDoc`
    rather than from the markdown — pass ``min_level=1``, otherwise
    every section is dropped and the TOC comes back empty.
    ``text`` is the *plain* heading (inline markdown stripped) because
    the TOC renders it inside a link, where nested ``<b>`` would fight
    the link styling.
    """
    found: list[Heading] = []
    for raw in (md_text or "").split("\n"):
        parts = _heading_parts(raw.strip())
        if parts is None:
            continue
        level, text, anchor = parts
        if level < min_level:
            continue
        plain = _CODE_RE.sub(r"\1", _BOLD_RE.sub(r"\1", _ITALIC_RE.sub(r"\1", text)))
        found.append(Heading(level=level, text=plain, anchor=anchor))
    return tuple(found)


def strip_title(md_text: str) -> str:
    """Drop the leading ``# Title`` line, if there is one.

    The page shell already gives the article a heading, and the guide
    files open with a title of their own ("📘 Полное руководство…").
    Rendering both stacks two titles on top of each other with nothing
    between them. The shell's wins because it is the one the table of
    contents and the ``<h1>`` are written against; the markdown keeps
    its title so the ``.md`` file still reads as a document on its own
    (and in the ``/commands/edit`` textarea).
    """
    lines = (md_text or "").split("\n")
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            return "\n".join(lines[index + 1 :])
        break
    return md_text


#: Heading texts that mean "this section is a table of contents",
#: matched after emoji and punctuation are stripped and case-folded.
_CONTENTS_TITLES: Final[frozenset[str]] = frozenset(
    {"содержание", "оглавление", "contents", "table of contents"}
)


def _is_contents_heading(text: str) -> bool:
    """True when a heading names a table of contents.

    Inline markup comes off first, then everything that is not a letter
    or a space — the emoji in front, a trailing colon, punctuation the
    author felt like adding.
    """
    plain = _CODE_RE.sub(r"\1", _BOLD_RE.sub(r"\1", _ITALIC_RE.sub(r"\1", text)))
    cleaned = "".join(ch for ch in plain if ch.isalpha() or ch.isspace())
    return cleaned.strip().casefold() in _CONTENTS_TITLES


def _looks_like_a_contents_list(body: list[str]) -> bool:
    """True when the section body is nothing but list items.

    The name check alone is not enough to delete a section by. The
    guide is editable at ``/commands/edit``, so an operator can write a
    chapter genuinely *called* "Содержание" — and losing it, silently,
    on save is a much worse failure than showing one duplicated list.
    Requiring the body to be list items keeps the deletion to the shape
    it was written for: a numbered run of ``[Раздел](#anchor)``. Blank
    lines and ``---`` rules count as decoration — both guides close
    their contents list with a rule before the first chapter, and a
    separator is not prose worth saving.
    """
    seen_item = False
    for raw in body:
        stripped = raw.strip()
        if not stripped or stripped == "---":
            continue
        if not (stripped.startswith("- ") or _ORDERED_RE.match(stripped)):
            return False
        seen_item = True
    return seen_item


def strip_own_contents(md_text: str) -> str:
    """Drop the guide's hand-written table of contents, if it has one.

    Both guide files open with a "📑 Содержание" section: a numbered
    list of links to the sections below. That was the right call on
    Telegraph, which gave an article no navigation of its own. This page
    builds a real TOC from the headings (:func:`extract_headings`) and
    renders it immediately above the prose, so keeping the hand-written
    copy shows the reader the same list twice in a row — and it is the
    copy that silently goes stale, because nothing regenerates it when a
    section is renamed.

    Three conditions have to hold before anything is removed: the
    heading has to be named like a contents list, the section has to run
    only to the next heading of its own level or shallower (so a
    *subsection* called "Contents" inside a future chapter survives),
    and the body has to be nothing but list items
    (:func:`_looks_like_a_contents_list`).
    """
    lines = (md_text or "").split("\n")
    start: int | None = None
    level = 0
    for index, raw in enumerate(lines):
        parts = _heading_parts(raw.strip())
        if parts is None:
            continue
        if start is None:
            if _is_contents_heading(parts[1]):
                start, level = index, parts[0]
            continue
        if parts[0] <= level:
            if not _looks_like_a_contents_list(lines[start + 1 : index]):
                return md_text
            return "\n".join(lines[:start] + lines[index:])
    if start is None or not _looks_like_a_contents_list(lines[start + 1 :]):
        return md_text
    return "\n".join(lines[:start])


def _flush_list(items: list[str], ordered: bool) -> Iterator[str]:
    if not items:
        return
    tag = "ol" if ordered else "ul"
    yield f"<{tag}>"
    yield from (f"<li>{item}</li>" for item in items)
    yield f"</{tag}>"


def md_to_html(md_text: str) -> str:
    """Block-level markdown → an HTML fragment.

    Consecutive ``- ``/``N. `` lines are collected into one real list
    rather than emitted per-line, which is the whole reason this
    function keeps state instead of being a pure per-line map.
    """
    out: list[str] = []
    items: list[str] = []
    ordered = False

    def flush() -> None:
        nonlocal items, ordered
        out.extend(_flush_list(items, ordered))
        items = []
        ordered = False

    for raw in (md_text or "").strip().split("\n"):
        stripped = raw.strip()

        heading = _heading_parts(stripped)
        if heading is not None:
            flush()
            level, text, anchor = heading
            # +1: markdown ``#`` is the document title, and the page
            # already spends its <h1> on the hero.
            tag = f"h{min(level + 1, 6)}"
            attr = html_lib.escape(anchor, quote=True)
            out.append(f'<{tag} id="{attr}">{render_inline(text)}</{tag}>')
            continue

        if stripped == "---":
            flush()
            out.append("<hr>")
            continue

        ordered_match = _ORDERED_RE.match(stripped)
        if ordered_match:
            if items and not ordered:
                flush()
            ordered = True
            items.append(render_inline(ordered_match.group(2)))
            continue

        if stripped.startswith("- "):
            if items and ordered:
                flush()
            items.append(render_inline(stripped[2:]))
            continue

        flush()
        if not stripped:
            continue
        out.append(f"<p>{render_inline(stripped)}</p>")

    flush()
    return "\n".join(out)
