"""``/robots.txt`` and ``/sitemap.xml`` — the two files a crawler asks
for before it reads anything else.

Neither existed. ``/sitemap.xml`` answered 404, and ``/robots.txt`` was
answered at the edge by Cloudflare's managed content-signals block: a
thousand-odd bytes of legal boilerplate about AI training with not one
``User-agent:`` line in it, no ``Allow``, no ``Disallow``, and no
``Sitemap:``. So the first two requests every crawler makes were
answered by something that says nothing about this site, and the twelve
pages were left to be found by whatever links happen to exist.

That matters here more than it would on a blog. This is the surface an
acquiring bank reads — the front page, three legal documents, a contact
form and a command reference — and the whole point of it is to be
findable and to look like a service that is maintained. It is also
small, static and completely enumerable: a sitemap for it is not a
guess assembled from a crawl, it is the same list the navigation row
already builds, which is why the test beside this module checks the two
against each other rather than against a hand-written expectation.

Both files are built once, at router-build time, for the reason
:mod:`telegram_invite_bot.cms.legal.router` sets out at length: these
are unauthenticated public endpoints on a small host shared with
other services, and work done at startup fails as a failed
deploy rather than as a broken response.
"""

from __future__ import annotations

import html as html_lib
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

from telegram_invite_bot.cms.legal.documents import DOCUMENTS
from telegram_invite_bot.cms.paths import (
    absolute,
    commands_path,
    contact_path,
    doc_path,
    home_path,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from telegram_invite_bot.cms.legal.context import LegalContext

ROBOTS_PATH: Final[str] = "/robots.txt"
SITEMAP_PATH: Final[str] = "/sitemap.xml"

#: Both files change only when the site is redeployed, and both are
#: fetched by machines that will happily re-fetch them hourly. Same
#: value as the pages themselves so the whole surface expires together.
_CACHE_CONTROL: Final[str] = "public, max-age=3600"

_LANGS: Final[tuple[str, ...]] = ("ru", "en")


def page_paths(ctx: LegalContext) -> list[Callable[[str], str]]:
    """Every public page of this deployment, as ``lang -> path``.

    In the navigation row's reading order, and under the same two
    conditions: the guide only when ``GUIDE_SITE_ENABLED`` is on, the
    contact form only when an admin chat is configured. A sitemap that
    lists a page answering 404 is worse than no sitemap — it is a
    machine-readable claim that the site is broken — so the list has to
    follow what is actually mounted, not what the code can render.
    """
    paths: list[Callable[[str], str]] = [home_path]
    if ctx.guide_enabled:
        paths.append(commands_path)
    paths.extend(
        # Bind the slug now: a bare closure over the loop variable would
        # leave every entry pointing at the last document.
        (lambda lang, slug=doc.slug: doc_path(slug, lang))  # type: ignore[misc]
        for doc in DOCUMENTS
    )
    if ctx.contact_enabled:
        paths.append(contact_path)
    return paths


def build_sitemap(ctx: LegalContext) -> str:
    """The twelve public URLs, each with its language twins.

    One ``<url>`` per page *per language* — the sitemap protocol wants
    every address listed in its own entry, with the same block of
    alternates repeated on both halves of a pair. That is the same
    statement the ``<head>`` of each page already makes, and it is made
    twice on purpose: a crawler that reaches a page by following a link
    reads the head, one that starts from the sitemap reads this, and
    neither is guaranteed to do the other.

    No ``<lastmod>``. The legal documents carry a revision date, but the
    front page, the contact form and the command reference have no
    honest one — they change when the deployment changes. Stamping them
    all with the build time would tell a crawler that every page is
    revised on every deploy, which is how a sitemap teaches a crawler to
    stop believing its dates. Omitting the element is explicitly allowed
    and says nothing false.

    No ``<priority>`` or ``<changefreq>`` either: both are advisory,
    both are ignored by the major crawlers, and a made-up number is
    still a made-up number.
    """
    entries: list[str] = []
    for build in page_paths(ctx):
        alternates = "".join(
            f'\n    <xhtml:link rel="alternate" hreflang="{code}"'
            f' href="{html_lib.escape(absolute(ctx.url_prefix, build(lang)), quote=True)}"/>'
            for code, lang in (("ru", "ru"), ("en", "en"), ("x-default", "ru"))
        )
        for lang in _LANGS:
            loc = html_lib.escape(absolute(ctx.url_prefix, build(lang)), quote=True)
            entries.append(f"  <url>\n    <loc>{loc}</loc>{alternates}\n  </url>")
    body = "\n".join(entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        ' xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
        f"{body}\n"
        "</urlset>\n"
    )


def build_robots(ctx: LegalContext, *, sitemap: bool) -> str:
    """The crawl policy, which is: read everything.

    Every page here exists to be read by strangers, so the file is two
    lines and a pointer. It deliberately does **not** enumerate the
    webhook and payment-callback paths under ``Disallow``. Those are
    POST-only — a crawler issues GETs and would never reach them — so
    listing them would protect nothing, while ``robots.txt`` is a public
    file that anyone can fetch: the list would be a map of this
    service's write endpoints, handed out on request.

    The one GET-reachable page that must stay out of search — the guide
    editor at ``/commands/edit`` — is kept out by ``X-Robots-Tag:
    noindex, nofollow`` on its own response instead (#1480). A
    ``Disallow`` here would be worse than nothing: it stops the crawler
    fetching the page, so the header never gets read, and a URL that is
    disallowed but linked from somewhere still lands in the index as a
    bare address.

    ``sitemap`` is false when no origin is configured (polling mode, and
    every test). The ``Sitemap:`` directive requires an absolute URL, so
    the line is dropped rather than emitted in a relative form that
    every crawler would discard anyway — and in that same deployment the
    sitemap route is not mounted at all, so the file stays honest about
    what is there.
    """
    lines = ["User-agent: *", "Allow: /"]
    if sitemap:
        lines += ["", f"Sitemap: {absolute(ctx.url_prefix, SITEMAP_PATH)}"]
    return "\n".join(lines) + "\n"


def build_router(ctx: LegalContext) -> APIRouter:
    """Construct the router over a frozen context.

    Takes the same :class:`~cms.legal.context.LegalContext` as the
    documents, the front page and the 404 — a second context would only
    be a second place for the origin and the two feature flags to
    disagree with what is mounted.
    """
    router = APIRouter(tags=["discovery"])
    has_origin = bool(ctx.url_prefix.strip())

    robots = build_robots(ctx, sitemap=has_origin)
    headers = {"Cache-Control": _CACHE_CONTROL}

    async def _robots() -> PlainTextResponse:
        return PlainTextResponse(robots, headers=headers)

    router.add_api_route(
        ROBOTS_PATH,
        _robots,
        methods=["GET", "HEAD"],
        response_class=PlainTextResponse,
        include_in_schema=False,
        name="robots_txt",
    )

    # Without an origin every ``<loc>`` would be a relative path, which
    # the sitemap protocol does not allow. A deployment in that state
    # serves no sitemap and advertises none, rather than serving one
    # that is invalid on its face.
    if has_origin:
        sitemap = build_sitemap(ctx)

        async def _sitemap() -> Response:
            return Response(sitemap, media_type="application/xml", headers=headers)

        router.add_api_route(
            SITEMAP_PATH,
            _sitemap,
            methods=["GET", "HEAD"],
            include_in_schema=False,
            name="sitemap_xml",
        )

    return router
