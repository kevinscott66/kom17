"""The site's 404 page, and the exception handler that serves it.

Every other public page of this site exists because someone arrives at
an address they did not get from a link on the site itself: a reviewer
types the bare domain by hand (#130), a monitor sends a HEAD (#131), a
regulator follows a URL out of a document. A miss is the same story with
the address slightly wrong — a stale link, a typo, or a good guess at a
naming rule the site does not follow. ``/privacy/en`` is the English
policy, so ``/en/privacy`` is the first thing an English reader tries;
``/commands/en`` exists, so ``/commands/ru`` looks like it should.

Until this module, all of those answered ``{"detail":"Not Found"}`` as
``application/json``: no navigation row, no wordmark, no way back short
of retyping the domain. The reader who is hardest to get back is exactly
the one this site is written for, since they have no Telegram account to
fall back on.

Deliberately **not** a router. A 404 is what the routing table says when
nothing matched, so there is no path to mount; it is installed as an
exception handler over the whole app. And deliberately not a package
with a ``content`` module next to it, the way the front page and the
contact form are: this page is four strings, and splitting four strings
across two files buys nothing but a second file to keep in step.
"""

from __future__ import annotations

import html as html_lib
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Final

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import HTMLResponse

from telegram_invite_bot.cms.csp import csp_for_html
from telegram_invite_bot.cms.guide_site.markdown import md_to_html
from telegram_invite_bot.cms.guide_site.rendering import build_doc_shell
from telegram_invite_bot.cms.legal.documents import unwrap_paragraphs
from telegram_invite_bot.cms.nav import site_nav_html
from telegram_invite_bot.cms.paths import absolute, home_path
from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import Response

    from telegram_invite_bot.cms.legal.context import LegalContext

#: Substituted at render time, same convention as the other pages.
SLOT_HOME: Final[str] = "[[HOME]]"

#: A miss must not be cached. Paths on this site are created by deploys,
#: so an address that 404s today can be a real page an hour from now,
#: and an hour of edge cache would keep serving the miss to everyone who
#: followed the link that prompted the fix.
_CACHE_CONTROL: Final[str] = "no-store"


@dataclass(frozen=True, slots=True)
class _Copy:
    """The page in one language. ``title`` and ``lede`` are plain text."""

    title: str
    lede: str
    body_md: str


_RU = _Copy(
    title="Такого адреса нет",
    lede="Страница, которую вы открыли, на сайте не существует.",
    body_md="""
# Что случилось

Ссылка могла устареть, или в адресе опечатка. Все страницы сайта перечислены в строке
выше — юридические документы, поддержка и остальное.

Вернуться на [главную]([[HOME]]).
""",
)

_EN = _Copy(
    title="No such address",
    lede="The page you opened does not exist on this site.",
    body_md="""
# What happened

The link may be out of date, or the address may have a typo. Every page of the site is
listed in the row above — the legal documents, support and the rest.

Go back to the [front page]([[HOME]]).
""",
)


def _copy_for(lang: str) -> _Copy:
    """The page in ``lang``; anything but ``en`` reads Russian."""
    return _EN if lang == "en" else _RU


def language_for(path: str, accept_language: str = "") -> str:
    """Which language a reader who missed was probably reading.

    The path is asked first and the header second, because the path is
    evidence about *this* request while the header is a standing
    preference. An English reader on a Russian-configured browser who
    mistypes ``/privacy/en`` should still land on an English apology.

    A whole path segment has to equal ``en`` — a prefix test would read
    ``/enterprise`` as English. Both orders count: ``/privacy/en`` is
    the real English URL, and ``/en/privacy`` is the mirror-image guess
    that brought the reader here in the first place.
    """
    if "en" in (segment for segment in path.split("/") if segment):
        return "en"
    first = accept_language.split(",")[0].split(";")[0].strip().lower()
    return "en" if first == "en" or first.startswith("en-") else "ru"


def wants_html(accept: str) -> bool:
    """Whether this client is a reader rather than a program.

    The same app serves the Telegram webhook and three payment
    callbacks. Those are configured by URL, so the way they reach a 404
    is a misconfiguration — and the response that helps whoever debugs
    it is the machine-readable one their tooling already logs. Browsers
    name ``text/html`` explicitly in ``Accept``; API clients send
    ``*/*`` or ``application/json`` and keep the JSON body they had
    before this module existed.
    """
    return "text/html" in accept.lower()


def render_not_found(ctx: LegalContext, lang: str) -> str:
    """One complete HTML page."""
    lang = "en" if lang.lower() == "en" else "ru"
    copy = _copy_for(lang)
    body_md = unwrap_paragraphs(copy.body_md.replace(SLOT_HOME, home_path(lang)))
    return build_doc_shell(
        body_html=md_to_html(body_md),
        doc_nav_html=site_nav_html(
            url_prefix=ctx.url_prefix,
            lang=lang,
            # No entry is current: the reader is on a page that is not in
            # the row, and marking one anyway would tell assistive tech
            # they are somewhere they are not.
            current=None,
            guide_enabled=ctx.guide_enabled,
            contact_enabled=ctx.contact_enabled,
        ),
        toc_html="",
        lang=lang,
        # Escape AT the boundary — the shell builder interpolates
        # straight into HTML.
        page_title=html_lib.escape(copy.title),
        site_title=html_lib.escape(ctx.site_title),
        subtitle=html_lib.escape(t("site_notfound_kind", lang)),
        lede=html_lib.escape(copy.lede),
        lang_nav_label=html_lib.escape(t("site_lang_nav", lang), quote=True),
        open_bot_label=html_lib.escape(t("site_open_bot", lang)),
        home_url=html_lib.escape(absolute(ctx.url_prefix, home_path(lang)), quote=True),
        # The RU/EN pair leads to the front page in each language rather
        # than to this address in the other one: the address does not
        # exist in either, so offering to show it again in English would
        # be a link back to the same apology.
        url_ru=html_lib.escape(absolute(ctx.url_prefix, home_path("ru")), quote=True),
        url_en=html_lib.escape(absolute(ctx.url_prefix, home_path("en")), quote=True),
        # ...and for the same reason this page declares no canonical URL
        # and no hreflang pair. Those links say "this address names this
        # document, and here it is in the other language"; here the
        # address names nothing, and the pair above leads somewhere else
        # entirely.
        canonical=False,
        tme_url=html_lib.escape(ctx.tme_url, quote=True),
        footer_line=html_lib.escape(f"{ctx.site_title} · {ctx.revision}"),
    )


@lru_cache(maxsize=8)
def _page_and_policy(ctx: LegalContext, lang: str) -> tuple[str, str]:
    """The page and its policy, built once per language per deployment.

    A 404 is the one response an unauthenticated stranger can ask for
    without limit, and scanners ask constantly. Rendering markdown and
    hashing seventeen kilobytes on every miss would do that work on the
    same event loop that answers the Telegram webhook. The result
    depends only on the context — frozen at startup — and the language,
    of which there are two, so the cache is two entries that never go
    stale.
    """
    page = render_not_found(ctx, lang)
    return page, csp_for_html(page)


def not_found_response(ctx: LegalContext, request: Request) -> HTMLResponse:
    """The rendered page, with the headers it needs to look like itself.

    The policy is computed here rather than left to the middleware's
    fallback: that fallback is ``style-src 'none'``, which is right for
    an error page nobody styled and wrong for this one, whose entire
    appearance is a single inline ``<style>`` block. Without its own
    hash the page would arrive stripped to unstyled text — an error page
    that looks broken, on the one visit where the reader is already
    unsure the site works.
    """
    lang = language_for(request.url.path, request.headers.get("accept-language", ""))
    page, policy = _page_and_policy(ctx, lang)
    return HTMLResponse(
        page,
        status_code=404,
        headers={"Cache-Control": _CACHE_CONTROL, "Content-Security-Policy": policy},
    )


def install(app: FastAPI, ctx: LegalContext) -> None:
    """Serve the HTML page for a reader's 404, JSON for everyone else.

    Registered for ``StarletteHTTPException`` rather than for the bare
    404 status, because that is the class FastAPI raises for an unmatched
    path; every other status keeps the framework's default handler by
    being handed straight back to it.
    """
    from fastapi.exception_handlers import http_exception_handler

    @app.exception_handler(StarletteHTTPException)
    async def _handle(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404 and wants_html(request.headers.get("accept", "")):
            return not_found_response(ctx, request)
        return await http_exception_handler(request, exc)
