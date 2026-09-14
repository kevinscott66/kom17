"""The parts of the ``<head>`` that every page on the site carries.

Five packages render pages here — the front page, the three legal
documents, the contact form, the command guide and the 404 — and every
one of them reaches the browser through one of the two shell builders
in :mod:`cms.guide_site.rendering`. That makes the shell the only place
where "every page has this" can be made true, and this file the only
place it can be checked: a test that lives beside one router proves
that router's page and says nothing about the other four.

So the fixture below renders one page of every kind, in both
languages, and each test asserts across all of them at once. A sixth
page kind added later without its chrome fails here.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ElementTree
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_invite_bot.cms import notfound
from telegram_invite_bot.cms.contact.router import render_page as render_contact
from telegram_invite_bot.cms.csp import csp_for_html
from telegram_invite_bot.cms.guide_site import GuideSiteContext
from telegram_invite_bot.cms.guide_site import build_router as build_guide_router
from telegram_invite_bot.cms.home.router import render_home
from telegram_invite_bot.cms.legal.context import LegalContext
from telegram_invite_bot.cms.legal.documents import BY_SLUG
from telegram_invite_bot.cms.legal.router import render_document
from telegram_invite_bot.cms.paths import commands_path

if TYPE_CHECKING:
    from pathlib import Path

LANGS = ("ru", "en")

#: The origin the fixture pages are rendered behind. A real one, so a
#: URL that leaks a relative path stands out.
ORIGIN = "https://tgbot.delabs.space"


def _ctx() -> LegalContext:
    return LegalContext(
        site_title="ком17",
        operator="ком17",
        operator_details=None,
        support_url="https://t.me/kom17_support",
        support_email="support@example.com",
        bot_username="kom17_bot",
        url_prefix=ORIGIN,
        contact_enabled=True,
    )


def _guide_pages(tmp_path: Path) -> dict[str, str]:
    """The command reference, through its router.

    The guide is the one page kind whose renderer wants files on disk,
    and the only user of the *second* shell builder — so leaving it out
    would leave half the chrome untested.
    """
    ru = tmp_path / "guide_ru.md"
    en = tmp_path / "guide_en.md"
    ru.write_text("# Гайд\n\n## Раздел\n\n- пункт\n", encoding="utf-8")
    en.write_text("# Guide\n\n## Section\n\n- item\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(
        build_guide_router(
            GuideSiteContext(
                guide_file_ru=ru,
                guide_file_en=en,
                site_title="ком17",
                version="1.2.3",
                bot_username="kom17_bot",
                url_prefix=ORIGIN,
            )
        )
    )
    client = TestClient(app)
    return {f"commands.{lang}": client.get(commands_path(lang)).text for lang in LANGS}


@pytest.fixture
def pages(tmp_path: Path) -> dict[str, str]:
    """One rendered page of every kind the site serves, in both languages."""
    ctx = _ctx()
    rendered = {
        f"{kind}.{lang}": render(ctx, lang)
        for kind, render in (
            ("home", render_home),
            ("privacy", lambda c, ln: render_document(c, BY_SLUG["privacy"], ln)),
            ("contact", render_contact),
            ("notfound", notfound.render_not_found),
        )
        for lang in LANGS
    }
    rendered.update(_guide_pages(tmp_path))
    return rendered


def test_the_fixture_covers_every_page_kind(pages: dict[str, str]) -> None:
    """A guard on the guard: ten pages, or the sweeps below prove less.

    Without this, a fixture entry that silently stopped rendering — a
    renamed helper, a router that started returning an error page —
    would turn every test in this file green by having nothing to
    check.
    """
    assert sorted(pages) == [
        "commands.en",
        "commands.ru",
        "contact.en",
        "contact.ru",
        "home.en",
        "home.ru",
        "notfound.en",
        "notfound.ru",
        "privacy.en",
        "privacy.ru",
    ]
    for name, page in pages.items():
        assert page.startswith("<!DOCTYPE html>"), name


# --- the icon (#183) ------------------------------------------------


#: The icon link as the shell writes it. Read back out of the rendered
#: page rather than imported from the renderer, so these tests describe
#: what a browser receives instead of restating a constant.
_ICON_RE = re.compile(r'<link rel="icon" type="image/svg\+xml" href="([^"]*)"/>')


def _icon_hrefs(pages: dict[str, str]) -> dict[str, str]:
    """The one icon URI each page carries, keyed by page."""
    found: dict[str, str] = {}
    for name, page in pages.items():
        hits = _ICON_RE.findall(page)
        assert len(hits) == 1, (name, len(hits))
        found[name] = hits[0]
    return found


def test_every_page_declares_one_icon(pages: dict[str, str]) -> None:
    """Until #183 none of them did, and every tab showed a blank sheet.

    A page with no ``rel="icon"`` is not merely plain: the browser goes
    and asks for ``/favicon.ico`` on its own, gets the 404 page for its
    trouble, and shows the generic document glyph in the tab, the
    bookmark bar and the history list.

    All ten are checked together because both shell builders have to
    carry it. Editing one head and not the other is the likely mistake,
    and it would leave the command reference — the page ``/help`` sends
    people to — as the only blank tab on the site.
    """
    assert len(_icon_hrefs(pages)) == len(pages)
    assert len(set(_icon_hrefs(pages).values())) == 1, "pages disagree on the icon"


def test_the_icon_is_in_the_head(pages: dict[str, str]) -> None:
    """Not merely present — present where a browser looks for it.

    A ``<link>`` that drifted below ``<body>`` is still honoured by
    every real browser, so this cannot be caught by looking at a tab.
    It can be caught here, before it becomes the reason one client in
    ten shows no icon.
    """
    for name, page in pages.items():
        assert page.index('<link rel="icon"') < page.index("<body>"), name


def test_the_icon_is_a_drawable_image(pages: dict[str, str]) -> None:
    """The payload has to survive being a URI and still parse as SVG.

    An icon that fails to parse fails silently — the tab falls back to
    the same blank sheet as having no icon at all, and nothing is
    logged anywhere. So the bytes a browser would decode are decoded
    here and handed to a real XML parser.
    """
    prefix = "data:image/svg+xml,"
    for name, href in _icon_hrefs(pages).items():
        assert href.startswith(prefix), name
        svg = urllib.parse.unquote(href[len(prefix) :])
        root = ElementTree.fromstring(svg)
        assert root.tag == "{http://www.w3.org/2000/svg}svg", name
        # A standalone SVG is not inline SVG: with no namespace
        # declared, a browser renders nothing at all.
        assert "xmlns=" in svg, name


def test_the_colours_survive_the_uri(pages: dict[str, str]) -> None:
    """The one encoding mistake that yields a plausible-looking icon.

    Every colour in the mark is a ``#rrggbb`` literal, and an unencoded
    ``#`` starts the URI's *fragment*: the browser would keep the bytes
    up to the first colour, discard everything after it, and draw
    whatever that truncated document happens to mean. That is not a
    blank tab and not an error — it is a wrong icon, silently, which is
    why it gets a test of its own rather than riding along with the
    parse check above.
    """
    for name, href in _icon_hrefs(pages).items():
        assert "#" not in href, name
        svg = urllib.parse.unquote(href[len("data:image/svg+xml,") :])
        # The background, and the same colour again for the C struck
        # into the coin; then the gold of the coin itself.
        assert svg.count("#0d0c0a") == 2, name
        assert svg.count("#e8b437") == 1, name


def test_the_policy_still_lets_the_icon_load(pages: dict[str, str]) -> None:
    """The page's own CSP has to permit the scheme its icon is written in.

    ``default-src 'none'`` denies images too, so the icon depends on
    the ``img-src 'self' data:`` line in :mod:`cms.csp` staying there.
    Tightening that line to ``'self'`` alone is a one-word edit that
    blanks every tab on the site and breaks nothing else — no error, no
    log, no failing request, because the icon never leaves the page it
    is written in. This is what would notice.
    """
    for name, page in pages.items():
        directives = {part.split(" ", 1)[0]: part for part in csp_for_html(page).split("; ")}
        assert "data:" in directives["img-src"], name


#: The links that say which document a page is. Read out of the
#: rendered page for the same reason as the icon above: what matters is
#: what a crawler receives, not what a constant in the renderer says.
_CANONICAL_RE = re.compile(r'<link rel="canonical" href="([^"]*)"/>')
_ALTERNATE_RE = re.compile(r'<link rel="alternate" hreflang="([^"]*)" href="([^"]*)"/>')

#: The eight pages that name a document, and the address each one is
#: the canonical copy of. Written out rather than derived from
#: :mod:`cms.paths`: these are the site's public addresses, the ones
#: already printed in the bot, in the legal documents and in whatever
#: the acquirer has on file. Deriving them would let a renamed route
#: rename the expectation with it and pass, which is the one thing a
#: canonical URL is not allowed to do quietly.
_CANONICAL_URLS = {
    "home.ru": f"{ORIGIN}/",
    "home.en": f"{ORIGIN}/en",
    "privacy.ru": f"{ORIGIN}/privacy",
    "privacy.en": f"{ORIGIN}/privacy/en",
    "contact.ru": f"{ORIGIN}/contact",
    "contact.en": f"{ORIGIN}/contact/en",
    "commands.ru": f"{ORIGIN}/commands",
    "commands.en": f"{ORIGIN}/commands/en",
}


def test_every_real_page_is_the_canonical_copy_of_its_own_address(
    pages: dict[str, str],
) -> None:
    """One canonical link per page, naming that page.

    A canonical that names some *other* page is worse than none at all
    — it asks for this page to be dropped in favour of that one — so
    the address is checked, not merely the tag's presence.
    """
    for name, expected in _CANONICAL_URLS.items():
        hits = _CANONICAL_RE.findall(pages[name])
        assert hits == [expected], (name, hits)


def test_the_twins_agree_on_the_pair(pages: dict[str, str]) -> None:
    """Both halves of a document declare the same two addresses.

    This is the guarantee ``hreflang`` exists for, and it only holds if
    the declaration is mutual: a page that lists a twin which does not
    list it back is ignored. So for each document, the Russian page's
    ``hreflang="ru"`` must be the Russian page's own canonical, and its
    ``hreflang="en"`` must be the English page's canonical — and the
    English page must say exactly the same thing.
    """
    for kind in ("home", "privacy", "contact", "commands"):
        expected = {
            "ru": _CANONICAL_URLS[f"{kind}.ru"],
            "en": _CANONICAL_URLS[f"{kind}.en"],
            "x-default": _CANONICAL_URLS[f"{kind}.ru"],
        }
        for lang in LANGS:
            name = f"{kind}.{lang}"
            assert dict(_ALTERNATE_RE.findall(pages[name])) == expected, name


def test_the_default_is_the_russian_page(pages: dict[str, str]) -> None:
    """``x-default`` is the Russian copy, on both halves of every pair.

    It names the version for a reader whose language matches neither,
    and this is a bot whose entire interface, catalogue and support are
    Russian: handing that reader the English translation would be a
    friendlier-looking lie. Flipping it to the English page is a
    one-word edit with no other visible effect, which is why it gets a
    test of its own rather than living inside the sweep above.
    """
    for name in _CANONICAL_URLS:
        alternates = dict(_ALTERNATE_RE.findall(pages[name]))
        assert alternates["x-default"] == _CANONICAL_URLS[f"{name.split('.')[0]}.ru"], name


def test_the_missing_page_claims_no_identity(pages: dict[str, str]) -> None:
    """The 404 declares neither a canonical URL nor a language twin.

    It has a language row like every other page, so it has a ``url_ru``
    and a ``url_en`` to hand — but they lead to the front page, because
    the address the reader typed does not exist in either language.
    Passing that pair through as ``hreflang`` would claim the front page
    is the English translation of this apology; a self-canonical would
    invite the address to be indexed under a URL that will never serve
    anything. The shell takes ``canonical=False`` for exactly this page,
    and this is what holds the caller to it.
    """
    for lang in LANGS:
        page = pages[f"notfound.{lang}"]
        assert _CANONICAL_RE.findall(page) == [], lang
        assert _ALTERNATE_RE.findall(page) == [], lang


def test_the_identity_links_are_in_the_head(pages: dict[str, str]) -> None:
    """Both kinds of link land before ``<body>``.

    ``<link>`` outside the head is dropped by the parser, and the page
    renders identically either way — nothing on screen changes, so only
    a test looking at position can tell.
    """
    for name in _CANONICAL_URLS:
        page = pages[name]
        body = page.index("<body>")
        assert page.index('<link rel="canonical"') < body, name
        assert page.index('<link rel="alternate"') < body, name
