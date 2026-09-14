"""The page shell for the public guide.

Pure templating: no I/O, no environment reads, no imports from FastAPI.
The caller escapes anything user-controlled before handing it over
(:func:`router.render_commands_page` does).

The layout answers two different visitors with one page, in the order
they arrive:

1. **"What was the command for X?"** — a search box and the generated
   command index sit above the prose. This is the common case, it is
   answered in one keystroke, and it is the half that cannot go stale
   because it is generated (see :mod:`command_index`).
2. **"How does this bot actually work?"** — the long-form guide below,
   with a real table of contents built from its own headings.

Design notes worth keeping:

* **The look is the product's, not a template's.** The bot is a coin
  economy — earn, top up, bet, withdraw — so the page is dressed as a
  *ledger / operator's manual*: monospace for every command name and
  every piece of chrome, a serif for the prose that is actually read,
  one gold accent standing in for the coin, numbered sections, and
  hairline rules instead of cards. The first draft was the default
  dark-slate-and-indigo card grid, and it looked like every other
  generated page; nothing about it said what this bot does. Concretely
  that means: no drop shadows, no blur, no rounded corners, no filled
  pills — if a change starts adding those back, it is drifting back to
  the template.
* **No web fonts, no CDN, no framework.** Most visitors arrive through
  Telegram's in-app browser on mobile, frequently on a bad connection
  and sometimes from a network where fonts.googleapis.com is slow or
  blocked. A system font stack renders instantly and cannot fail; a
  webfont buys a nicer heading and risks a page of invisible text.
* **Both themes.** ``prefers-color-scheme`` decides, because Telegram's
  in-app browser follows the user's app theme and a page that ignores
  it reads as broken. The dark palette is the primary one (it is what
  the majority of Telegram clients are set to), the light one is a real
  palette rather than an inversion.
* **The slash is not the whole surface.** A panel above the index says
  so before the reader has scrolled past their first command: most of
  the bot answers to bare words, and the list below would otherwise
  read as "type a slash or nothing happens". It sits outside the
  filtered rows on purpose — the rule stays on screen even when a
  search matches nothing.
* **Search filters, never hides structure.** Typing narrows the command
  cards and reports a count; an empty result says so instead of leaving
  a blank page. The guide prose is not filtered — searching a 40 KB
  article by substring produces confetti, and the TOC is the right tool
  for that half.
* **Tap a command to copy it.** The one interaction on the page. It
  works without JavaScript in the sense that the command text is still
  visible and selectable — the button only saves a step.
"""

from __future__ import annotations

import html as html_lib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from telegram_invite_bot.cms.guide_site.markdown import Heading

#: The one piece of iconography on the page: a coin with the ``C`` of
#: COM struck into it. Inline rather than a file so the page stays a
#: single request, and an ``<svg>`` rather than the 🪙 emoji so it
#: inherits ``currentColor`` and renders the same on every platform.
_COIN_SVG = (
    '<svg class="coin" viewBox="0 0 20 20" aria-hidden="true" focusable="false">'
    '<circle cx="10" cy="10" r="8.4" fill="none" stroke="currentColor" stroke-width="1.5"/>'
    '<path d="M12.4 7.5a3.2 3.2 0 1 0 0 5" fill="none" stroke="currentColor"'
    ' stroke-width="1.7" stroke-linecap="round"/></svg>'
)

#: The same coin, redrawn to survive being sixteen pixels wide. The
#: inline mark above is a thin ring in ``currentColor``: correct beside
#: a wordmark, invisible on a browser tab, where the stroke lands under
#: one pixel and there is no text colour to inherit. So the tab gets
#: the coin filled solid, stating its own colours — the site's gold on
#: the site's background — and reads as the same mark at a glance.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#0d0c0a'/>"
    "<circle cx='16' cy='16' r='10' fill='#e8b437'/>"
    "<path d='M20 11.6a5.6 5.6 0 1 0 0 8.8' fill='none' stroke='#0d0c0a'"
    " stroke-width='3.2' stroke-linecap='round'/></svg>"
)

#: The icon as the pages carry it: inline, in a ``data:`` URI. A file
#: would mean a route to mount, cache and test in order to deliver
#: three hundred bytes that never change, on a site built to be one
#: request per page. ``img-src 'self' data:`` in :mod:`cms.csp` was
#: written for this and says so.
#:
#: Three characters are encoded, each for its own reason: an unencoded
#: ``#`` starts the URI's fragment and would cut both colours off the
#: end, and ``<``/``>`` are encoded so nothing reading the attribute
#: with less care than a parser can mistake it for markup. The SVG is
#: single-quoted throughout, so the double-quoted attribute around it
#: needs no escaping of its own.
_HEAD_ICON = (
    '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,'
    + _FAVICON_SVG.replace("<", "%3C").replace(">", "%3E").replace("#", "%23")
    + '"/>'
)


def _head_links(*, lang: str, url_ru: str, url_en: str, canonical: bool) -> str:
    """The ``<head>`` links that say which document this page is.

    Every page on this site exists twice, in Russian and in English, at
    two different addresses. Without these links nothing in the markup
    says so: the language row in the topbar is an ``<a>`` like any
    other, and a crawler reading it learns that two pages link to each
    other, not that they are one document in two languages. The
    consequences are the ordinary ones — the two versions compete with
    each other instead of pooling their standing, and a reader searching
    in English can be handed the Russian page — and they land on the
    surface an acquiring bank reads.

    ``canonical=False`` turns the whole block off, and the 404 page is
    the reason it exists. That page has a language row, so it has a
    ``url_ru`` and a ``url_en``, but they lead to the front page rather
    than to this address in the other language: the address does not
    exist in either. Declaring the front page as this page's English
    twin would be false, and declaring a canonical for an address that
    names no document would be worse — it invites the missing page to
    be indexed under a URL that will never serve it.

    ``x-default`` points at the Russian page. It names the version for
    a reader whose language matches neither, and this is a bot whose
    entire interface, catalogue and support are Russian: sending that
    reader to the English translation would be a friendlier-looking
    lie. Both concrete alternates are still declared, so a reader who
    does ask for English is matched by ``hreflang="en"`` before the
    default is ever consulted.

    The URLs arrive already absolute and already escaped — the callers
    build them with :func:`cms.paths.absolute`, which falls back to the
    relative form when no origin is configured. A relative
    self-canonical resolves to the page's own address, which is what it
    would have said anyway, so the fallback degrades to a no-op rather
    than to a wrong claim.
    """
    if not canonical:
        return ""
    self_url = url_en if lang == "en" else url_ru
    return (
        f'\n<link rel="canonical" href="{self_url}"/>'
        f'\n<link rel="alternate" hreflang="ru" href="{url_ru}"/>'
        f'\n<link rel="alternate" hreflang="en" href="{url_en}"/>'
        f'\n<link rel="alternate" hreflang="x-default" href="{url_ru}"/>'
    )


_CSS = """
:root {
  color-scheme: dark light;
  --bg: #0d0c0a;
  --panel: #16140f;
  --ink: #ece7dc;
  --ink-soft: #c6c0b2;
  --muted: #8e8676;
  --gold: #e8b437;
  --gold-soft: rgba(232, 180, 55, 0.13);
  --rule: rgba(236, 231, 220, 0.14);
  --rule-strong: rgba(236, 231, 220, 0.30);
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, "DejaVu Sans Mono", Consolas, monospace;
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Charter, Georgia, "Times New Roman", serif;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f5f0e6;
    --panel: #ebe4d4;
    --ink: #17150f;
    --ink-soft: #3c372c;
    --muted: #6c6353;
    --gold: #8a5a08;
    --gold-soft: rgba(138, 90, 8, 0.10);
    --rule: rgba(23, 21, 15, 0.16);
    --rule-strong: rgba(23, 21, 15, 0.34);
  }
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  min-height: 100vh;
  font-family: var(--serif);
  background: var(--bg);
  color: var(--ink);
  line-height: 1.62;
  font-size: 17px;
  -webkit-font-smoothing: antialiased;
  overflow-wrap: break-word;
}
a { color: var(--gold); text-underline-offset: 3px; }
::selection { background: var(--gold); color: var(--bg); }
:focus-visible { outline: 2px solid var(--gold); outline-offset: 3px; }
.wrap { max-width: 760px; margin: 0 auto; padding: 0 1.15rem; }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
}
/* Small caps mono label — the page's recurring "chrome" voice. */
.tag {
  font-family: var(--mono); font-weight: 700; font-size: 0.72rem;
  letter-spacing: 0.18em; text-transform: uppercase;
}

/* ---------- top bar ---------- */
.topbar {
  position: sticky; top: 0; z-index: 20;
  background: var(--bg); border-bottom: 1px solid var(--rule-strong);
}
.top-inner {
  display: flex; align-items: center; gap: 0.75rem 1rem;
  justify-content: space-between; min-height: 54px;
}
.brand {
  display: inline-flex; align-items: center; gap: 0.55rem;
  color: var(--ink); text-decoration: none;
  font-family: var(--mono); font-weight: 700; font-size: 0.82rem;
  letter-spacing: 0.14em; text-transform: uppercase;
}
.brand .coin { width: 19px; height: 19px; flex: none; color: var(--gold); }
.nav { display: flex; align-items: center; gap: 0.55rem; }
.nav .sep { color: var(--rule-strong); font-family: var(--mono); }
.lang {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 34px; min-height: 44px; text-decoration: none;
  font-family: var(--mono); font-weight: 700; font-size: 0.8rem;
  letter-spacing: 0.1em; color: var(--muted);
  border-bottom: 2px solid transparent;
  transition: color .15s, border-color .15s;
}
.lang:hover { color: var(--ink); }
.lang.is-on { color: var(--gold); border-bottom-color: var(--gold); }

/* ---------- masthead ---------- */
/* Padding-top/bottom, never the shorthand: this element also carries
   .wrap, whose horizontal padding the shorthand would silently zero
   out and push the title into the screen edge. */
.masthead { padding-top: 2.1rem; padding-bottom: 1.6rem; }
.masthead .tag { display: block; color: var(--muted); margin: 0 0 1rem; }
.masthead h1 {
  font-family: var(--mono); font-weight: 700;
  font-size: clamp(1.55rem, 6.5vw, 2.1rem); line-height: 1.18;
  letter-spacing: -0.01em; margin: 0;
}
.masthead .lede {
  margin: 0.9rem 0 0; max-width: 44ch;
  font-size: 1.02rem; color: var(--ink-soft);
}
.tg-link {
  display: inline-flex; align-items: center; gap: 0.6rem;
  margin-top: 1.5rem; min-height: 46px; padding: 0.55rem 1.15rem;
  border: 1px solid var(--gold); color: var(--gold); text-decoration: none;
  font-family: var(--mono); font-weight: 700; font-size: 0.76rem;
  letter-spacing: 0.14em; text-transform: uppercase;
  transition: background .15s, color .15s;
}
.tg-link:hover { background: var(--gold); color: var(--bg); }
.tg-link .arw { font-size: 0.95rem; }

/* ---------- document navigation ---------- */
/* Shared by all four page kinds: the front page, the guide and the
   three legal documents each render the same row. */
.docnav {
  display: flex; flex-wrap: wrap; gap: 0.35rem 1.4rem;
  margin: 1.6rem 0 0; padding: 0.9rem 0;
  border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule);
}
.docnav a {
  display: inline-flex; align-items: center; min-height: 34px;
  font-family: var(--mono); font-weight: 700; font-size: 0.74rem;
  letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--muted); text-decoration: none;
  border-bottom: 1px solid transparent;
}
.docnav a:hover { color: var(--ink); }
.docnav a[aria-current="page"] { color: var(--gold); border-bottom-color: var(--gold); }

/* ---------- search ---------- */
.searchbar {
  position: sticky; top: 54px; z-index: 15;
  background: var(--bg); border-bottom: 1px solid var(--rule);
  padding: 0.55rem 0 0.5rem;
}
.search-field {
  display: flex; align-items: center; gap: 0.55rem;
  border-bottom: 2px solid var(--rule-strong);
  transition: border-color .15s;
}
.search-field:focus-within { border-bottom-color: var(--gold); }
/* The prompt glyph is the slash every command starts with — the field
   reads as a command line, which is exactly what is typed into it. */
.search-field::before {
  content: "/"; font-family: var(--mono); font-weight: 700;
  font-size: 1.15rem; color: var(--gold); line-height: 1;
}
.search-field input {
  flex: 1; min-width: 0; min-height: 46px;
  padding: 0.55rem 0; border: 0; background: transparent; color: var(--ink);
  font-family: var(--mono); font-size: 16px; /* 16px or iOS zooms on focus */
}
.search-field input:focus { outline: none; }
.search-field input::placeholder { color: var(--muted); }
.search-field input::-webkit-search-cancel-button { display: none; }
.search-clear {
  width: 44px; height: 44px; flex: none; border: 0; background: transparent;
  color: var(--muted); font-family: var(--mono); font-size: 1.15rem;
  cursor: pointer; display: none;
}
.search-clear:hover { color: var(--gold); }
.search-field[data-filled="1"] .search-clear { display: block; }
.search-count {
  margin: 0.45rem 0 0; min-height: 1.15em;
  font-family: var(--mono); font-size: 0.72rem;
  letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted);
}

/* ---------- command index ---------- */
main { counter-reset: grp; }
.section-head { margin: 2.4rem 0 0; }
.section-head h2 {
  display: flex; align-items: center; gap: 0.85rem; margin: 0;
  font-family: var(--mono); font-weight: 700; font-size: 0.76rem;
  letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted);
}
.section-head h2::after { content: ""; flex: 1; height: 1px; background: var(--rule); }
.section-head p { margin: 0.6rem 0 0; font-size: 0.95rem; color: var(--ink-soft); }
/* Sections are numbered so a section can be referred to by number the
   way a printed manual does. The counter deliberately keeps counting
   through search-hidden groups: the number is a stable address, not a
   position in the current filter. */
.cmd-group { margin-top: 2rem; counter-increment: grp; }
.cmd-group h3 {
  display: flex; align-items: baseline; gap: 0.8rem; margin: 0;
  font-family: var(--mono); font-weight: 700; font-size: 0.82rem;
  letter-spacing: 0.14em; text-transform: uppercase; color: var(--ink);
}
.cmd-group h3::before { content: counter(grp, decimal-leading-zero); color: var(--gold); }
.cmd-group h3::after { content: ""; flex: 1; height: 1px; background: var(--rule); }
.cmd-note { margin: 0.5rem 0 0; font-size: 0.92rem; color: var(--muted); }
.cmd-list { margin-top: 0.9rem; border-top: 1px solid var(--rule); }
.cmd {
  display: grid; grid-template-columns: 1fr; gap: 0.15rem;
  padding: 0.8rem 0; border-bottom: 1px solid var(--rule);
}
.cmd-name {
  justify-self: start; display: inline-flex; align-items: center;
  min-height: 34px; margin: 0; padding: 0; border: 0; background: none;
  cursor: pointer; text-align: left;
}
.cmd-name code {
  font-family: var(--mono); font-weight: 700; font-size: 0.95rem;
  color: var(--gold); border-bottom: 1px dotted transparent;
  transition: border-color .15s;
}
.cmd-name:hover code { border-bottom-color: var(--gold); }
.cmd-desc { margin: 0; font-size: 0.96rem; color: var(--ink-soft); }
.cmd-aliases { margin: 0.15rem 0 0; display: flex; flex-wrap: wrap; gap: 0.55rem; }
.alias { font-family: var(--mono); font-size: 0.75rem; color: var(--muted); }
/* The no-slash triggers. Deliberately NOT chips: they are words a
   person types, not commands, and giving them the ``/alias`` shape
   would imply a slash belongs in front. The label carries the gold so
   the eye can skip the whole line once it knows what it is. */
.cmd-plain {
  margin: 0.2rem 0 0; font-family: var(--mono); font-size: 0.75rem; color: var(--muted);
}
.cmd-plain span { color: var(--gold); margin-right: 0.5rem; }
/* Commands that share this row without being spellings of it (#164).
   Each gets its own line and its own description, because the whole
   point is that they are not interchangeable with the name on the
   left — an alias chip run would say the opposite. */
.cmd-subs { margin: 0.2rem 0 0; font-size: 0.8rem; color: var(--muted); }
.cmd-subs > span:first-child { display: block; color: var(--gold); font-family: var(--mono); }
.cmd-sub { display: block; margin: 0.15rem 0 0; }
.cmd-sub code { font-family: var(--mono); color: var(--ink-soft); margin-right: 0.35rem; }
@media (min-width: 620px) {
  .cmd { grid-template-columns: 12.5rem 1fr; gap: 0.15rem 1.5rem; align-items: baseline; }
  .cmd-desc, .cmd-aliases, .cmd-plain, .cmd-subs { grid-column: 2; }
}

/* ---------- "without a slash" panel ---------- */
.plain-note {
  margin: 1.5rem 0 0; padding: 1.1rem 0 1.2rem;
  border-top: 1px solid var(--rule-strong); border-bottom: 1px solid var(--rule-strong);
}
.plain-note .tag { display: block; margin: 0 0 0.8rem; color: var(--gold); }
.plain-note ul { list-style: none; margin: 0; padding: 0; }
.plain-note li {
  margin: 0.5rem 0; font-size: 0.95rem; color: var(--ink-soft); line-height: 1.75;
}
.plain-note code {
  font-family: var(--mono); font-size: 0.8rem;
  background: var(--gold-soft); color: var(--gold); padding: 0.12rem 0.4rem;
  white-space: nowrap;
}
/* The prefix/whitelist runs sit inside a sentence, so they lose the
   gold — an unbroken row of it reads as a warning. They keep a hairline
   box instead, because several of them are two words ("chat info", and
   its Russian twin) and without an edge the run reads as one long
   sentence fragment rather than a list of separate things you can type.
   Examples here stay Latin on purpose: the stylesheet is inlined into
   every page, English ones included (#176). */
.plain-note code.pfx {
  background: none; color: var(--ink); padding: 0.1rem 0.4rem;
  border: 1px solid var(--rule-strong); margin: 0 0.06rem;
}
.cmd[hidden], .cmd-group[hidden] { display: none; }
.empty {
  display: none; margin: 1.6rem 0 0; padding: 1.3rem 0;
  border-top: 1px dashed var(--rule-strong); border-bottom: 1px dashed var(--rule-strong);
  color: var(--muted); font-size: 0.95rem; text-align: center;
}
.empty[data-on="1"] { display: block; }

/* ---------- table of contents ---------- */
.toc { margin-top: 1.4rem; border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule); }
.toc > summary {
  cursor: pointer; list-style: none; min-height: 50px;
  display: flex; align-items: center; justify-content: space-between; gap: 0.7rem;
  font-family: var(--mono); font-weight: 700; font-size: 0.74rem;
  letter-spacing: 0.16em; text-transform: uppercase; color: var(--ink);
}
.toc > summary::-webkit-details-marker { display: none; }
.toc > summary::after { content: "[+]"; color: var(--gold); }
.toc[open] > summary::after { content: "[\\2212]"; }
.toc ul { list-style: none; margin: 0 0 0.9rem; padding: 0; border-top: 1px solid var(--rule); }
.toc li { margin: 0; }
.toc a {
  display: block; padding: 0.5rem 0.1rem; font-size: 0.95rem;
  text-decoration: none; color: var(--ink-soft);
}
.toc a:hover { color: var(--gold); }
.toc .lvl-3 a { padding-left: 1.5rem; font-size: 0.88rem; color: var(--muted); }

/* ---------- guide prose ---------- */
.guide { margin-top: 1.5rem; max-width: 66ch; }
/* The converter shifts every heading down a level, because the page
   spends its <h1> on the masthead. The guide files sit one level deeper
   still — their body is nested in an <h2>-headed <section>, so their
   chapters are written at ``##`` (rendering as h3) and their per-command
   subsections at ``###``. Everything served by ``build_doc_shell`` — the
   home page, the legal documents, the contact page — goes straight into
   the article under the masthead, so its chapters are written at ``#``
   and render as h2. Hence the *chapter* rule has to cover h2 and h3
   both. Getting this wrong is invisible in a unit test and obvious on
   the page — every chapter looks like a paragraph lead. */
.guide h2, .guide h3 {
  font-size: 1.34rem; font-weight: 700; color: var(--ink); line-height: 1.3;
  margin: 2.8rem 0 0.8rem; padding-top: 1.3rem; border-top: 1px solid var(--rule);
  scroll-margin-top: 118px;
}
.guide h3 { font-size: 1.26rem; }
.guide > h2:first-child, .guide > h3:first-child { margin-top: 0.4rem; padding-top: 0; border-top: 0; }
/* Subsections are named after commands ("Command /start"), so they get
   the same monospaced gold the command index uses. */
.guide h4 {
  font-family: var(--mono); font-weight: 700; font-size: 0.95rem;
  color: var(--gold); margin: 1.9rem 0 0.4rem; scroll-margin-top: 118px;
}
.guide p { margin: 0.7rem 0; color: var(--ink-soft); }
.guide ul, .guide ol { margin: 0.7rem 0; padding-left: 1.3rem; color: var(--ink-soft); }
.guide li { margin: 0.35rem 0; }
.guide li::marker { color: var(--gold); }
.guide code {
  font-family: var(--mono); font-size: 0.86em;
  background: var(--gold-soft); color: var(--gold); padding: 0.1rem 0.35rem;
}
.guide blockquote {
  margin: 1rem 0; padding: 0.2rem 0 0.2rem 1.1rem;
  border-left: 2px solid var(--gold); color: var(--muted);
}
.guide hr { border: none; border-top: 1px solid var(--rule); margin: 1.6rem 0; }
.guide b, .guide strong { color: var(--ink); font-weight: 700; }

/* ---------- misc ---------- */
.toast {
  position: fixed; left: 1.15rem; bottom: 1.3rem; max-width: calc(100% - 2.3rem);
  background: var(--panel); color: var(--ink);
  border: 1px solid var(--rule-strong); border-left: 3px solid var(--gold);
  padding: 0.6rem 0.9rem; font-family: var(--mono); font-size: 0.8rem;
  opacity: 0; transform: translateY(0.5rem); pointer-events: none;
  transition: opacity .18s, transform .18s; z-index: 40;
}
.toast[data-on="1"] { opacity: 1; transform: none; }
footer {
  margin: 3.2rem 0 0; border-top: 1px solid var(--rule);
  padding: 1.5rem 1.15rem 3rem;
  font-family: var(--mono); font-size: 0.7rem;
  letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--muted); text-align: center;
}
@media (min-width: 640px) {
  body { font-size: 18px; }
  .masthead { padding: 3rem 0 2rem; }
}
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  * { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
}
"""

_JS = """
(function () {
  var input = document.getElementById('q');
  var field = document.getElementById('qfield');
  var clear = document.getElementById('qclear');
  var count = document.getElementById('qcount');
  var empty = document.getElementById('qempty');
  var cards = Array.prototype.slice.call(document.querySelectorAll('.cmd'));
  var groups = Array.prototype.slice.call(document.querySelectorAll('.cmd-group'));
  var labels = JSON.parse(document.getElementById('i18n').textContent);

  function apply() {
    var q = (input.value || '').trim().toLowerCase().replace(/^\\//, '');
    field.setAttribute('data-filled', q ? '1' : '0');
    var shown = 0;
    cards.forEach(function (card) {
      var hit = !q || card.getAttribute('data-search').indexOf(q) !== -1;
      card.hidden = !hit;
      if (hit) shown++;
    });
    groups.forEach(function (group) {
      group.hidden = !group.querySelector('.cmd:not([hidden])');
    });
    count.textContent = q ? labels.found.replace('{n}', String(shown)) : '';
    empty.setAttribute('data-on', q && shown === 0 ? '1' : '0');
  }

  input.addEventListener('input', apply);
  clear.addEventListener('click', function () {
    input.value = '';
    apply();
    input.focus();
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { input.value = ''; apply(); }
  });

  var toast = document.getElementById('toast');
  var timer = null;
  function flash(text) {
    toast.textContent = text;
    toast.setAttribute('data-on', '1');
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () { toast.setAttribute('data-on', '0'); }, 1800);
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.cmd-name') : null;
    if (!btn) return;
    var text = btn.getAttribute('data-copy');
    // Clipboard API needs a secure context; on plain http (or an old
    // in-app webview) it is simply absent, so say what happened rather
    // than failing silently — the command is on screen either way.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { flash(labels.copied.replace('{cmd}', text)); },
        function () { flash(labels.copyfail); }
      );
    } else {
      flash(labels.copyfail);
    }
  });
  apply();
})();
"""


def build_toc_html(headings: Sequence[Heading], title: str) -> str:
    """The collapsible table of contents.

    ``<details>`` rather than a JS accordion: it is one element, it
    works before scripts load, it is keyboard-operable for free, and on
    a phone a closed-by-default TOC keeps the guide's first section
    within a thumb's reach. Open by default on wide screens would be
    nicer, but a media-query-driven ``open`` attribute does not exist —
    and toggling it from JS would make the page jump on load, which is
    a worse trade than one extra tap.
    """
    if not headings:
        return ""
    rows = "\n".join(
        f'<li class="lvl-{heading.level}">'
        f'<a href="#{html_lib.escape(heading.anchor, quote=True)}">'
        f"{html_lib.escape(heading.text)}</a></li>"
        for heading in headings
        if heading.level <= 3
    )
    return (
        f'<details class="toc"><summary>{html_lib.escape(title)}</summary><ul>{rows}</ul></details>'
    )


#: The one rule the legal pages need and the guide does not. Appended
#: after :data:`_CSS` on those pages only. The navigation row used to
#: live here too, back when the guide was the one page without one.
_DOC_CSS = """
/* The document body is the whole page here, so it starts right under
   the nav rather than under a section head. */
.doc { margin-top: 1.8rem; }
"""


def build_doc_shell(
    *,
    body_html: str,
    doc_nav_html: str,
    toc_html: str,
    lang: str,
    page_title: str,
    site_title: str,
    subtitle: str,
    lede: str,
    lang_nav_label: str,
    open_bot_label: str,
    home_url: str,
    url_ru: str,
    url_en: str,
    canonical: bool,
    tme_url: str,
    footer_line: str,
) -> str:
    """The document page — legal documents and the front page. Every
    argument is pre-escaped.

    ``home_url`` is the brand link, and is separate from ``url_ru`` on
    purpose: the RU/EN pair switches the language of *this* page, while
    the wordmark in the corner is the one affordance every visitor
    already expects to lead to the front page. They were the same value
    until the front page existed, which made the wordmark a link to the
    page you were already on.

    Deliberately the guide's chrome minus its machinery: same typography,
    same palette, same topbar — but no search box, no client-side script,
    no copy-to-clipboard. A privacy policy has nothing to filter, and a
    page whose only job is to be readable and permanently available
    should not depend on JavaScript running to be either.
    """
    html_lang = "ru" if lang != "en" else "en"
    lang_ru = "lang is-on" if lang != "en" else "lang"
    lang_en = "lang is-on" if lang == "en" else "lang"
    head_links = _head_links(lang=lang, url_ru=url_ru, url_en=url_en, canonical=canonical)
    return f"""<!DOCTYPE html>
<html lang="{html_lang}">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="color-scheme" content="dark light"/>
{_HEAD_ICON}{head_links}
<meta name="description" content="{page_title} — {site_title}"/>
<title>{page_title} — {site_title}</title>
<style>{_CSS}{_DOC_CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="wrap top-inner">
    <a class="brand" href="{home_url}">{_COIN_SVG}{site_title}</a>
    <nav class="nav" aria-label="{lang_nav_label}">
      <a class="{lang_ru}" href="{url_ru}" hreflang="ru">RU</a>
      <span class="sep" aria-hidden="true">/</span>
      <a class="{lang_en}" href="{url_en}" hreflang="en">EN</a>
    </nav>
  </div>
</header>

<div class="wrap masthead">
  <p class="tag">{subtitle}</p>
  <h1>{page_title}</h1>
  <p class="lede">{lede}</p>
  <a class="tg-link" href="{tme_url}" target="_blank" rel="noopener">{open_bot_label}
    <span class="arw" aria-hidden="true">&#8594;</span></a>
</div>

<main class="wrap">
{doc_nav_html}
{toc_html}
  <article class="guide doc">
{body_html}
  </article>
</main>

<footer>{footer_line}</footer>
</body>
</html>"""


def build_guide_shell(
    *,
    guide_html: str,
    index_html: str,
    plain_html: str,
    doc_nav_html: str,
    toc_html: str,
    labels: Mapping[str, str],
    lang: str,
    page_title: str,
    site_title: str,
    version: str,
    home_url: str,
    url_ru: str,
    url_en: str,
    canonical: bool,
    tme_url: str,
    i18n_json: str,
) -> str:
    """Assemble the page. Every argument is pre-escaped by the caller.

    ``doc_nav_html`` is the same row the legal pages carry, and is
    here for the same reason it is there: this is the page ``/help``
    and ``/faq`` send people to, so it is the likeliest place for
    someone to go looking for the privacy policy or a way to write to
    the operator.

    ``i18n_json`` is the JSON blob the client-side search reads for its
    two or three strings. It rides in a ``<script type="application/
    json">`` tag instead of being interpolated into the JS source,
    because a quote inside a translation would otherwise end the string
    literal and take the whole script with it.
    """
    html_lang = "ru" if lang != "en" else "en"
    lang_ru = "lang is-on" if lang != "en" else "lang"
    lang_en = "lang is-on" if lang == "en" else "lang"
    head_links = _head_links(lang=lang, url_ru=url_ru, url_en=url_en, canonical=canonical)
    return f"""<!DOCTYPE html>
<html lang="{html_lang}">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="color-scheme" content="dark light"/>
{_HEAD_ICON}{head_links}
<meta name="description" content="{labels["subtitle"]}"/>
<title>{page_title}</title>
<style>{_CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="wrap top-inner">
    <a class="brand" href="{home_url}">{_COIN_SVG}{site_title}</a>
    <nav class="nav" aria-label="{labels["lang_nav"]}">
      <a class="{lang_ru}" href="{url_ru}" hreflang="ru">RU</a>
      <span class="sep" aria-hidden="true">/</span>
      <a class="{lang_en}" href="{url_en}" hreflang="en">EN</a>
    </nav>
  </div>
</header>

<div class="wrap masthead">
  <p class="tag">{labels["subtitle"]} · v{version}</p>
  <h1>{page_title}</h1>
  <p class="lede">{labels["lede"]}</p>
  <a class="tg-link" href="{tme_url}" target="_blank" rel="noopener">{labels["open_bot"]}
    <span class="arw" aria-hidden="true">&#8594;</span></a>
</div>

<main class="wrap">
{doc_nav_html}
  <div class="searchbar">
    <div class="search-field" id="qfield" data-filled="0">
      <label for="q" class="sr-only">{labels["search_label"]}</label>
      <input id="q" type="search" autocomplete="off" autocorrect="off" spellcheck="false"
             enterkeyhint="search" placeholder="{labels["search_placeholder"]}"
             aria-label="{labels["search_label"]}"/>
      <button id="qclear" class="search-clear" type="button" aria-label="{labels["search_clear"]}">×</button>
    </div>
    <p class="search-count" id="qcount" role="status" aria-live="polite"></p>
  </div>

  <section aria-labelledby="cmd-h">
    <div class="section-head">
      <h2 id="cmd-h">{labels["commands_heading"]}</h2>
      <p>{labels["commands_sub"]}</p>
    </div>
{plain_html}
{index_html}
    <p class="empty" id="qempty" data-on="0">{labels["search_empty"]}</p>
  </section>

  <section aria-labelledby="guide-h">
    <div class="section-head">
      <h2 id="guide-h">{labels["guide_heading"]}</h2>
      <p>{labels["guide_sub"]}</p>
    </div>
{toc_html}
    <article class="guide">
{guide_html}
    </article>
  </section>
</main>

<div class="toast" id="toast" role="status" aria-live="polite"></div>
<footer>{site_title} · v{version}</footer>
<script id="i18n" type="application/json">{i18n_json}</script>
<script>{_JS}</script>
</body>
</html>"""
