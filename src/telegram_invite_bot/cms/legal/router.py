"""FastAPI ``APIRouter`` for the three public legal documents.

Six GET routes — ``/privacy``, ``/terms``, ``/support`` and their
``/en`` counterparts — mounted from :mod:`webhook.server` alongside the
guide. Same process, same certificate, same uptime as the bot itself:
the acquiring bank's requirement is that the documents be *permanently*
available, and the cheapest way to guarantee that is to make them fail
only when the bot itself is down.

Unlike the guide, these pages read nothing from disk and nothing from
the database. The text is in :mod:`telegram_invite_bot.cms.legal
.documents`; the only runtime input is the operator's identity and
contacts, from config. A page here cannot 404 because a file is missing
and cannot serve half a document because a deploy was partial.

Both inputs are frozen, so all six pages are rendered **once**, when the
router is built, and the handlers do nothing but hand back a string.
That is not a micro-optimisation: these are unauthenticated public
endpoints on a small host shared with other services, and this
process is configured to be the one that loses when memory runs
short. Re-parsing ~25 KB of Markdown per request is exactly the kind of
cheap-to-trigger allocation churn that turns a crawler into an outage.
Rendering up front also moves any failure to startup, where it is a
failed deploy, rather than to the bank reviewer's first visit.
"""

from __future__ import annotations

import html as html_lib
from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from telegram_invite_bot.cms.csp import csp_for_html
from telegram_invite_bot.cms.guide_site.markdown import extract_headings, md_to_html
from telegram_invite_bot.cms.guide_site.rendering import build_doc_shell, build_toc_html
from telegram_invite_bot.cms.legal.documents import (
    DOCUMENTS,
    LegalDoc,
    build_contacts_md,
    render_body,
)
from telegram_invite_bot.cms.nav import site_nav_html
from telegram_invite_bot.cms.paths import (
    absolute,
    contact_path,
    doc_path,
    home_path,
)
from telegram_invite_bot.cms.static_page import html_page
from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from telegram_invite_bot.cms.legal.context import LegalContext

#: One hour at the edge. ``tgbot.delabs.space`` is proxied through
#: Cloudflare, which does not cache HTML unless told to — without this
#: header every hit on a document reaches the origin. An hour is the
#: compromise: a corrected operator identity goes live within one, and a
#: reviewer refreshing the page is served by the edge rather than by the
#: shared box the origin lives on.
_CACHE_CONTROL: str = "public, max-age=3600"


def _absolute(ctx: LegalContext, path: str) -> str:
    """``path`` behind the configured origin, or as-is when unset.

    Relative URLs work fine for someone already on the page; they do not
    work when the link is pasted into a bank's onboarding form, which is
    the audience this whole surface exists for.
    """
    return absolute(ctx.url_prefix, path)


def render_document(ctx: LegalContext, doc: LegalDoc, lang: str) -> str:
    """One complete HTML page."""
    lang = "en" if lang.lower() == "en" else "ru"
    contacts_md = build_contacts_md(
        lang,
        support_url=ctx.support_url,
        support_email=ctx.support_email,
        operator=ctx.operator_name,
        operator_details=ctx.operator_details,
        # Absolute: this line gets copied out of the page and pasted
        # into a form somewhere, which is the whole point of naming a
        # contact channel in a policy.
        contact_url=(_absolute(ctx, contact_path(lang)) if ctx.contact_enabled else None),
    )
    body_md = render_body(
        doc,
        lang,
        service=ctx.site_title,
        operator=ctx.operator_name,
        contacts_md=contacts_md,
        updated=ctx.revision,
    )
    nav = site_nav_html(
        url_prefix=ctx.url_prefix,
        lang=lang,
        current=doc.slug,
        guide_enabled=ctx.guide_enabled,
        contact_enabled=ctx.contact_enabled,
    )
    return build_doc_shell(
        body_html=md_to_html(body_md),
        doc_nav_html=nav,
        # ``min_level=1``: a legal document's sections are written
        # with one ``#`` (they render as ``<h2>`` under the masthead
        # ``<h1>``), and its title comes from the shell, not from the
        # markdown — so there is no title line to drop here.
        toc_html=build_toc_html(
            extract_headings(body_md, min_level=1), t("site_toc_heading", lang)
        ),
        lang=lang,
        # Escape AT the boundary, exactly as the guide router does — the
        # shell builder interpolates straight into HTML.
        page_title=html_lib.escape(doc.title(lang)),
        site_title=html_lib.escape(ctx.site_title),
        subtitle=html_lib.escape(t("site_legal_kind", lang)),
        lede=html_lib.escape(t("site_legal_revision", lang, date=ctx.revision)),
        lang_nav_label=html_lib.escape(t("site_lang_nav", lang), quote=True),
        open_bot_label=html_lib.escape(t("site_open_bot", lang)),
        # The wordmark leads to the front page, in the language the
        # reader is already reading.
        home_url=html_lib.escape(_absolute(ctx, home_path(lang)), quote=True),
        url_ru=html_lib.escape(_absolute(ctx, doc_path(doc.slug, "ru")), quote=True),
        url_en=html_lib.escape(_absolute(ctx, doc_path(doc.slug, "en")), quote=True),
        canonical=True,
        tme_url=html_lib.escape(ctx.tme_url, quote=True),
        footer_line=html_lib.escape(
            f"{ctx.site_title} · {t('site_legal_kind', lang)} · {ctx.revision}"
        ),
    )


def build_router(ctx: LegalContext) -> APIRouter:
    """Construct the FastAPI router over a frozen context."""
    router = APIRouter(tags=["legal"])

    for doc in DOCUMENTS:
        for lang in ("ru", "en"):
            page = render_document(ctx, doc, lang)
            headers = {
                "Cache-Control": _CACHE_CONTROL,
                "Content-Security-Policy": csp_for_html(page),
            }

            # Bind the rendered page inside a factory. Without the
            # binding every route would close over the last page built
            # and all six paths would serve the English support
            # document — the classic late-binding bug, and one that a
            # smoke test hitting a single path would not catch. The
            # binding used to be a pair of defaulted parameters, which
            # FastAPI turned into a query parameter and a body field:
            # ``?_html=`` set the whole page (#1584).
            router.add_api_route(
                doc_path(doc.slug, lang),
                html_page(page, headers),
                # See the home router: FastAPI does not derive HEAD from
                # GET, and these are the URLs handed to a bank — the one
                # place a 405 on a link check is least affordable.
                methods=["GET", "HEAD"],
                response_class=HTMLResponse,
                include_in_schema=False,
                name=f"legal_{doc.slug}_{lang}",
            )

    return router
