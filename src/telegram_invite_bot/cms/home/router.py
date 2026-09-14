"""FastAPI ``APIRouter`` for the site's front page — ``/`` and ``/en``.

Until this router existed, nothing served ``/``: the guide answered on
``/commands``, the documents on ``/privacy``, ``/terms`` and
``/support``, and the bare domain returned a 404. That is the address a
compliance reviewer types by hand, the address anyone gets by trimming a
shared link, and the address the ``/commands/edit`` panel already linked
to — so the one page most likely to be opened cold was the only one that
did not exist.

Mounted **unconditionally**, like the legal router and unlike the guide:
a service whose home page can be switched off by an env var is not a
service anyone will underwrite. Which is also why the guide section of
the page is conditional instead — with ``GUIDE_SITE_ENABLED`` off, the
front page simply stops advertising ``/commands`` rather than sending
readers to a 404.

Both pages are rendered once, at router-build time, for the reasons
:mod:`telegram_invite_bot.cms.legal.router` sets out at length: these
are unauthenticated public endpoints on a small host shared with
other services, and a render that happens at startup fails as a
failed deploy rather than as a blank page.
"""

from __future__ import annotations

import html as html_lib
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from telegram_invite_bot.cms.csp import csp_for_html
from telegram_invite_bot.cms.guide_site.markdown import md_to_html
from telegram_invite_bot.cms.guide_site.rendering import build_doc_shell
from telegram_invite_bot.cms.home.content import (
    SLOT_COMMANDS,
    SLOT_CONTACT,
    SLOT_PRIVACY,
    SLOT_SERVICE,
    SLOT_SUPPORT,
    SLOT_TERMS,
    HomeCopy,
    copy_for,
)
from telegram_invite_bot.cms.legal.documents import unwrap_paragraphs
from telegram_invite_bot.cms.nav import HOME, site_nav_html
from telegram_invite_bot.cms.paths import (
    absolute,
    commands_path,
    contact_path,
    doc_path,
    home_path,
)
from telegram_invite_bot.cms.static_page import html_page
from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from telegram_invite_bot.cms.legal.context import LegalContext

#: The two languages the site speaks, in nav order.
_LANGS: Final[tuple[str, ...]] = ("ru", "en")

#: One hour at the edge, same as the legal documents: the page changes
#: only on deploy, and Cloudflare does not cache HTML unless told to.
_CACHE_CONTROL: Final[str] = "public, max-age=3600"


def _docs_md(copy: HomeCopy, ctx: LegalContext) -> str:
    """The documents list, with the contact form when it is mounted.

    The form used to be reachable from here only through the navigation
    row. Its own copy names its audience as an acquiring bank, a
    regulator, a personal-data request or a vulnerability report — which
    is the same reader this page is written for, arriving at the bare
    domain and looking for where to write. Leaving it out of the one
    list on the page headed "Documents" hid it from exactly them.

    Joined with a single newline, not the blank line that separates
    sections: this is a fourth bullet of an existing list, and a blank
    line would publish it as a list of its own.
    """
    section = copy.docs_md.strip("\n")
    if not ctx.contact_enabled:
        return section
    return f"{section}\n{copy.contact_md}"


def _body_md(copy: HomeCopy, ctx: LegalContext) -> str:
    """The sections, minus the ones the deployment cannot back."""
    parts = [copy.intro_md, copy.start_md]
    if ctx.guide_enabled:
        parts.append(copy.commands_md)
    parts.append(_docs_md(copy, ctx))
    return "\n\n".join(part.strip("\n") for part in parts)


def _fill(text: str, ctx: LegalContext, lang: str) -> str:
    """Substitute the copy's tokens.

    Links are deliberately **site-relative**: the guide's markdown
    renderer opens absolute URLs in a new tab (it cannot tell "our own
    domain" from a citation), and a reader sent to the privacy policy in
    a second tab has lost their place for no reason. The absolute form
    still appears where it is actually needed — the document nav, whose
    hrefs get copied out of the page and pasted into forms.
    """
    for token, value in (
        (SLOT_SERVICE, ctx.site_title),
        (SLOT_COMMANDS, commands_path(lang)),
        (SLOT_PRIVACY, doc_path("privacy", lang)),
        (SLOT_TERMS, doc_path("terms", lang)),
        (SLOT_SUPPORT, doc_path("support", lang)),
        (SLOT_CONTACT, contact_path(lang)),
    ):
        text = text.replace(token, value)
    return text


def render_home(ctx: LegalContext, lang: str) -> str:
    """One complete HTML page."""
    lang = "en" if lang.lower() == "en" else "ru"
    copy = copy_for(lang)
    body_md = unwrap_paragraphs(_fill(_body_md(copy, ctx), ctx, lang))
    return build_doc_shell(
        body_html=md_to_html(body_md),
        doc_nav_html=site_nav_html(
            url_prefix=ctx.url_prefix,
            lang=lang,
            current=HOME,
            guide_enabled=ctx.guide_enabled,
            contact_enabled=ctx.contact_enabled,
        ),
        # No table of contents: four short sections, all of them visible
        # on a phone within two swipes. A <details> block above them
        # would be navigation for a page that does not need any.
        toc_html="",
        lang=lang,
        # Escape AT the boundary — the shell builder interpolates
        # straight into HTML.
        page_title=html_lib.escape(copy.title),
        site_title=html_lib.escape(ctx.site_title),
        subtitle=html_lib.escape(t("site_home_kind", lang)),
        lede=html_lib.escape(_fill(copy.lede, ctx, lang)),
        lang_nav_label=html_lib.escape(t("site_lang_nav", lang), quote=True),
        open_bot_label=html_lib.escape(t("site_open_bot", lang)),
        # The wordmark goes to *this* page's language. Every other
        # page kind already did that; this one sent an English reader
        # back to the Russian front page. The RU/EN pair below is the
        # one place a link is supposed to cross languages.
        home_url=html_lib.escape(absolute(ctx.url_prefix, home_path(lang)), quote=True),
        url_ru=html_lib.escape(absolute(ctx.url_prefix, home_path("ru")), quote=True),
        url_en=html_lib.escape(absolute(ctx.url_prefix, home_path("en")), quote=True),
        canonical=True,
        tme_url=html_lib.escape(ctx.tme_url, quote=True),
        footer_line=html_lib.escape(f"{ctx.site_title} · {ctx.revision}"),
    )


def build_router(ctx: LegalContext) -> APIRouter:
    """Construct the FastAPI router over a frozen context.

    Takes the same context as the legal router rather than one of its
    own: everything this page needs — the bot's public name, its
    username, the origin — is already there, and a second copy of those
    three values is a second place for them to disagree.

    ``guide_enabled`` used to be the exception, threaded in as a keyword
    while the same flag also sat on the context the legal and contact
    pages read. Nothing made the two agree, so the front page could hide
    the guide from its own body and its own nav row while every other
    page went on linking it. That is the divergence #179 removed from
    the row; leaving one boolean outside the context would have left the
    door open for it.
    """
    router = APIRouter(tags=["home"])

    for lang in _LANGS:
        page = render_home(ctx, lang)
        headers = {"Cache-Control": _CACHE_CONTROL, "Content-Security-Policy": csp_for_html(page)}

        # Bind the rendered page in a factory, or both routes close over
        # the last value of ``page`` and ``/`` serves the English page —
        # the same late-binding trap the legal router documents. Not a
        # defaulted parameter: FastAPI reads those as request
        # parameters, and ``?_html=`` used to set the page (#1584).
        router.add_api_route(
            home_path(lang),
            html_page(page, headers),
            # HEAD as well as GET: uptime monitors, link checkers and
            # Telegram's own preview fetcher all reach for it first, and
            # FastAPI does not add it on its own — a bare ``["GET"]``
            # answers those with a 405.
            methods=["GET", "HEAD"],
            response_class=HTMLResponse,
            include_in_schema=False,
            name=f"home_{lang}",
        )

    return router
