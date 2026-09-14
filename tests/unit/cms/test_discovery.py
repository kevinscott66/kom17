"""``/robots.txt`` and ``/sitemap.xml``.

The sitemap's only real failure mode is drift: a page is added, or a
feature flag hides one, and the file goes on claiming the old set. A
hand-written list of expected URLs here would drift with it — updated
in the same commit, by the same person, for the same wrong reason — so
the central test does not have one. It renders the navigation row, the
row every page already carries and every reader already sees, and
asserts the sitemap lists exactly those addresses. The two can only
agree by both being right.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_invite_bot.cms import nav
from telegram_invite_bot.cms.discovery import (
    ROBOTS_PATH,
    SITEMAP_PATH,
    build_robots,
    build_router,
    build_sitemap,
)
from telegram_invite_bot.cms.legal.context import LegalContext

ORIGIN = "https://tgbot.delabs.space"
LANGS = ("ru", "en")

_SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "xhtml": "http://www.w3.org/1999/xhtml",
}

#: The navigation row is HTML, and this is the only thing needed out of
#: it. :func:`nav.site_nav_html` returns that row and nothing else — the
#: language switcher is built elsewhere — so every anchor here is a page.
_NAV_HREF_RE = re.compile(r'<a href="([^"]*)"')


def _ctx(*, guide: bool = True, contact: bool = True, origin: str = ORIGIN) -> LegalContext:
    return LegalContext(
        site_title="ком17",
        operator="ком17",
        url_prefix=origin,
        guide_enabled=guide,
        contact_enabled=contact,
    )


def _nav_urls(ctx: LegalContext) -> set[str]:
    """Every page address the site links to, from the row itself."""
    return {
        href
        for lang in LANGS
        for href in _NAV_HREF_RE.findall(
            nav.site_nav_html(
                url_prefix=ctx.url_prefix,
                lang=lang,
                guide_enabled=ctx.guide_enabled,
                contact_enabled=ctx.contact_enabled,
            )
        )
    }


def _locs(sitemap: str) -> list[str]:
    root = ElementTree.fromstring(sitemap)  # noqa: S314 (our own output)
    return [el.text or "" for el in root.findall("sm:url/sm:loc", _SITEMAP_NS)]


def test_the_sitemap_lists_exactly_what_the_site_links_to() -> None:
    """The whole point of the file, and the one thing that can rot.

    Not a count and not a hand-written list: the set of addresses in
    the sitemap must equal the set the navigation row offers a reader.
    A page added without a sitemap entry fails here, and so does a
    sitemap entry for a page nobody links to.
    """
    ctx = _ctx()
    locs = _locs(build_sitemap(ctx))
    assert len(locs) == len(set(locs)), locs
    assert set(locs) == _nav_urls(ctx)


@pytest.mark.parametrize(
    ("guide", "contact", "gone"),
    [
        (False, True, f"{ORIGIN}/commands"),
        (True, False, f"{ORIGIN}/contact"),
    ],
)
def test_a_switched_off_page_is_not_listed(guide: bool, contact: bool, gone: str) -> None:
    """A sitemap entry for a 404 is a machine-readable claim of breakage.

    Both flags are real: ``GUIDE_SITE_ENABLED`` is an env var, and the
    contact form is mounted only when an admin chat is configured. In
    either state the router that serves the page is not mounted at all,
    so an entry for it would point at the site's own apology page.
    """
    ctx = _ctx(guide=guide, contact=contact)
    locs = _locs(build_sitemap(ctx))
    assert gone not in locs
    assert f"{gone}/en" not in locs
    assert set(locs) == _nav_urls(ctx)


def test_every_entry_names_both_of_its_language_twins() -> None:
    """The same claim the ``<head>`` makes, restated for a crawler that
    starts here instead of following a link.

    Both halves of a pair must carry the identical block — an alternate
    declared on one side only is discarded — so the check is that the
    Russian entry and the English entry of each document are
    indistinguishable in their alternates.
    """
    root = ElementTree.fromstring(build_sitemap(_ctx()))  # noqa: S314
    blocks: dict[tuple[tuple[str, str], ...], list[str]] = {}
    for url in root.findall("sm:url", _SITEMAP_NS):
        loc_el = url.find("sm:loc", _SITEMAP_NS)
        assert loc_el is not None
        loc = loc_el.text or ""
        alternates = {
            link.attrib["hreflang"]: link.attrib["href"]
            for link in url.findall("xhtml:link", _SITEMAP_NS)
        }
        assert sorted(alternates) == ["en", "ru", "x-default"], loc
        assert alternates["x-default"] == alternates["ru"], loc
        assert loc in (alternates["ru"], alternates["en"]), loc
        blocks.setdefault(tuple(sorted(alternates.items())), []).append(loc)

    # Every document appears twice and both halves carry the same block.
    assert sorted(len(locs) for locs in blocks.values()) == [2] * len(blocks)


def test_the_locations_are_absolute() -> None:
    """A relative ``<loc>`` is invalid and the whole file is discarded.

    The origin comes from ``WEBHOOK_URL`` via
    :func:`cms.paths.absolute`, which falls back to the relative form
    when it is unset — harmless in a page, fatal here.
    """
    for loc in _locs(build_sitemap(_ctx())):
        assert loc.startswith(f"{ORIGIN}/"), loc


def test_robots_points_at_the_sitemap() -> None:
    """The pointer is most of what the file is for."""
    assert f"Sitemap: {ORIGIN}{SITEMAP_PATH}" in build_robots(_ctx(), sitemap=True)


def test_robots_maps_no_write_endpoints() -> None:
    """``robots.txt`` must not become a directory of POST endpoints.

    Adding ``Disallow: /yookassa-webhook`` and friends looks like
    tightening and is the opposite: crawlers issue GETs and would never
    reach them, so it protects nothing, while the file is public and the
    list would hand a stranger the map. This is here because the edit is
    a plausible one for someone to make later in good faith.
    """
    robots = build_robots(_ctx(), sitemap=True)
    for word in ("webhook", "admin", "metrics", "edit"):
        assert word not in robots.lower(), word


def test_without_an_origin_there_is_no_sitemap_and_nothing_claims_one() -> None:
    """Polling mode and every test run: no origin, so no valid sitemap.

    ``Sitemap:`` requires an absolute URL. Rather than emit a relative
    one that every crawler discards, the line is dropped — and the route
    is not mounted either, so the file's silence matches the site.
    """
    ctx = _ctx(origin="")
    assert "Sitemap:" not in build_robots(ctx, sitemap=False)

    app = FastAPI()
    app.include_router(build_router(ctx))
    with TestClient(app) as client:
        assert client.get(ROBOTS_PATH).status_code == 200
        assert client.get(SITEMAP_PATH).status_code == 404


def test_both_files_are_served_with_the_types_that_make_them_readable() -> None:
    """A sitemap served as ``text/html`` is not a sitemap.

    Also covers HEAD, which monitors and link previewers send and which
    this app returned 405 for until #131.
    """
    app = FastAPI()
    app.include_router(build_router(_ctx()))
    with TestClient(app) as client:
        robots = client.get(ROBOTS_PATH)
        sitemap = client.get(SITEMAP_PATH)
        assert client.head(ROBOTS_PATH).status_code == 200
        assert client.head(SITEMAP_PATH).status_code == 200

    assert robots.status_code == 200
    assert robots.headers["content-type"].startswith("text/plain")
    assert robots.text.startswith("User-agent: *\nAllow: /")

    assert sitemap.status_code == 200
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert ElementTree.fromstring(sitemap.text).tag == (  # noqa: S314
        "{http://www.sitemaps.org/schemas/sitemap/0.9}urlset"
    )
