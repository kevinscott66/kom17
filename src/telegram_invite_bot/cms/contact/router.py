"""FastAPI ``APIRouter`` for ``/contact`` and ``/contact/en``.

Four routes: a GET (and HEAD) per language serving a page rendered once
at startup, and a POST per language that hands the submission to the
bot. Mounted only when the deployment has somewhere to deliver to — see
:mod:`telegram_invite_bot.webhook.server`; a form whose Send button does
nothing is worse than no form, and on the domain handed to an acquiring
bank it reads as a broken service rather than a disabled one.

The GET pages are prerendered for the same reason the legal documents
are: unauthenticated public endpoints on a small shared host. The POST
responses cannot be — they carry the sender's own text back — so they
are rendered per request and derive their own CSP, exactly as the guide
editor's POST does.

**No CSRF token.** There is no session, no cookie and no authenticated
identity: a cross-site POST here can do nothing a direct POST cannot,
so a token would only add state to protect an action that is already
anonymous. What the endpoint does need — a ceiling on how often it can
be made to send — is :mod:`telegram_invite_bot.cms.contact.throttle`.
"""

from __future__ import annotations

import html as html_lib
import time
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from telegram_invite_bot.cms.contact.content import copy_for
from telegram_invite_bot.cms.contact.form import (
    FIELD_MESSAGE,
    FIELD_REPLY_TO,
    HONEYPOT_FIELD,
    MAX_MESSAGE_CHARS,
    MAX_REPLY_TO_CHARS,
    Rejection,
    rejection_text,
    render_form,
    render_notice,
    validate,
)
from telegram_invite_bot.cms.contact.notification import build_admin_message
from telegram_invite_bot.cms.contact.throttle import ContactThrottle, client_key
from telegram_invite_bot.cms.csp import csp_for_html
from telegram_invite_bot.cms.guide_site.markdown import md_to_html
from telegram_invite_bot.cms.guide_site.rendering import build_doc_shell
from telegram_invite_bot.cms.legal.documents import unwrap_paragraphs
from telegram_invite_bot.cms.nav import CONTACT, site_nav_html
from telegram_invite_bot.cms.paths import absolute, contact_path, home_path
from telegram_invite_bot.cms.static_page import html_page
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.http_body import declared_body_too_large

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from telegram_invite_bot.cms.contact.content import ContactCopy
    from telegram_invite_bot.cms.legal.context import LegalContext

log = logger.bind(component="cms.contact")

#: The two languages the site speaks, in nav order.
_LANGS: Final[tuple[str, ...]] = ("ru", "en")

#: One hour at the edge for the GET page, same as every other static
#: page here. The POST response deliberately gets none — it is one
#: sender's own text and must never be handed to a second one.
_CACHE_CONTROL: Final[str] = "public, max-age=3600"

#: Refuse a body larger than this without parsing it. The form's own
#: ceilings are 2000 + 200 characters, so 64 KiB is already an order of
#: magnitude of slack for UTF-8 and percent-encoding; the point is that
#: nginx's 1 MB default should not be the first thing that says no, and
#: that a megabyte of multipart is not decoded into memory on a host
#: whose OOM policy names this process first.
_MAX_BODY_BYTES: Final[int] = 64 * 1024


def render_page(
    ctx: LegalContext,
    lang: str,
    *,
    notice_html: str = "",
    form_html: str | None = None,
) -> str:
    """One complete HTML page.

    ``form_html`` defaults to an empty form; a POST passes the sender's
    values back in, or passes ``""`` to drop the form entirely — which
    is what the success page does, so a reload cannot re-send the same
    message by accident.
    """
    lang = "en" if lang.lower() == "en" else "ru"
    copy = copy_for(lang)
    body = md_to_html(unwrap_paragraphs(copy.intro_md))
    if form_html is None:
        form_html = render_form(copy, action=_action(ctx, lang))
    return build_doc_shell(
        body_html=body + notice_html + form_html,
        doc_nav_html=site_nav_html(
            url_prefix=ctx.url_prefix,
            lang=lang,
            current=CONTACT,
            guide_enabled=ctx.guide_enabled,
            contact_enabled=ctx.contact_enabled,
        ),
        # No table of contents: one short section and a form.
        toc_html="",
        lang=lang,
        # Escape AT the boundary — the shell interpolates straight into
        # HTML, exactly as it does for the legal and home routers.
        page_title=html_lib.escape(copy.title),
        site_title=html_lib.escape(ctx.site_title),
        subtitle=html_lib.escape(t("site_contact_kind", lang)),
        lede=html_lib.escape(copy.lede),
        lang_nav_label=html_lib.escape(t("site_lang_nav", lang), quote=True),
        open_bot_label=html_lib.escape(t("site_open_bot", lang)),
        home_url=html_lib.escape(absolute(ctx.url_prefix, home_path(lang)), quote=True),
        url_ru=html_lib.escape(absolute(ctx.url_prefix, contact_path("ru")), quote=True),
        url_en=html_lib.escape(absolute(ctx.url_prefix, contact_path("en")), quote=True),
        canonical=True,
        tme_url=html_lib.escape(ctx.tme_url, quote=True),
        footer_line=html_lib.escape(f"{ctx.site_title} · {copy.title}"),
    )


def _action(ctx: LegalContext, lang: str) -> str:
    """The form's ``action``, escaped for an attribute."""
    return html_lib.escape(absolute(ctx.url_prefix, contact_path(lang)), quote=True)


def _error_page(
    ctx: LegalContext,
    lang: str,
    copy: ContactCopy,
    text: str,
    *,
    message: str,
    reply_to: str,
) -> str:
    """A rejection, with the sender's text still in the form."""
    return render_page(
        ctx,
        lang,
        notice_html=render_notice(head=copy.err_title, body=text, ok=False),
        form_html=render_form(
            copy,
            action=_action(ctx, lang),
            # Echo back at most what the field would have accepted: a
            # rejected 60 KB paste must not be re-rendered into the page
            # that reports the rejection.
            message_value=message[: MAX_MESSAGE_CHARS + 1],
            reply_to_value=reply_to[: MAX_REPLY_TO_CHARS + 1],
        ),
    )


def _success_page(ctx: LegalContext, lang: str, copy: ContactCopy) -> str:
    """Delivered — and no form, so a reload cannot send it twice."""
    return render_page(
        ctx,
        lang,
        notice_html=render_notice(head=copy.ok_title, body=copy.ok_body, ok=True),
        form_html="",
    )


def _rendered(html: str, status_code: int = 200) -> HTMLResponse:
    """A per-request page under its own hash policy, never cached."""
    return HTMLResponse(
        html,
        status_code=status_code,
        headers={
            "Content-Security-Policy": csp_for_html(html),
            "Cache-Control": "no-store",
        },
    )


def _declared_body_too_large(request: Request) -> bool:
    """Whether the declared body is past the point of parsing it.

    The shared check, so this endpoint and the four payment webhooks
    cannot drift apart on what "too large" means or on the header
    parsing behind it; only the ceiling differs, and this one is far
    lower because a form with a 2200-character limit has no excuse for
    a large body.

    ``require_declared_length`` because this ceiling is the whole
    defence: the path ends in ``await request.form()``, whose
    urlencoded parser buffers the stream with no limit of its own, and
    unlike the payment webhooks there is no capped raw read behind it
    to catch a request that simply omits the header (#1633). A browser
    always declares the length of a form POST, so the only submissions
    this turns away are hand-built ones.
    """
    return declared_body_too_large(request, max_bytes=_MAX_BODY_BYTES, require_declared_length=True)


async def _read_fields(request: Request) -> tuple[str, str, str]:
    """The three form fields as text, ignoring anything else sent.

    ``request.form()`` yields ``UploadFile`` for a file part; those are
    coerced to ``""`` rather than read, so a multipart submission
    carrying an attachment is treated as an empty field instead of
    loading the file into memory.
    """
    form = await request.form()

    def field(name: str) -> str:
        value = form.get(name)
        return value if isinstance(value, str) else ""

    return field(FIELD_MESSAGE), field(FIELD_REPLY_TO), field(HONEYPOT_FIELD)


def build_router(
    ctx: LegalContext,
    *,
    deliver: Callable[[str], Awaitable[None]],
    now: Callable[[], float] = time.monotonic,
    throttle: ContactThrottle | None = None,
) -> APIRouter:
    """Construct the FastAPI router over a frozen context.

    ``deliver`` receives HTML-parse-mode text and must raise if the
    message did not reach the operator — the sender is told the truth
    either way, and a swallowed exception here is a promise the service
    cannot keep. It is injected so this module never imports aiogram and
    the tests never build a ``Bot``.

    ``now`` and ``throttle`` are injected so the tests can drive the
    limiter without sleeping; production takes the defaults.
    """
    router = APIRouter(tags=["contact"])
    limiter = throttle if throttle is not None else ContactThrottle()

    async def handle_post(request: Request, lang: str) -> HTMLResponse:
        copy = copy_for(lang)
        if _declared_body_too_large(request):
            log.info("contact: oversized body refused unparsed")
            return _rendered(
                _error_page(ctx, lang, copy, copy.err_too_long, message="", reply_to=""),
                status_code=413,
            )

        message, reply_to, honeypot = await _read_fields(request)

        rejection = validate(message=message, reply_to=reply_to, honeypot=honeypot)
        if rejection is Rejection.HONEYPOT:
            # Answered exactly like a delivered message. A bot that can
            # tell the difference is a bot that can adapt.
            log.debug("contact: honeypot filled, dropped")
            return _rendered(_success_page(ctx, lang, copy))
        if rejection is not None:
            return _rendered(
                _error_page(
                    ctx,
                    lang,
                    copy,
                    rejection_text(rejection, copy),
                    message=message,
                    reply_to=reply_to,
                ),
                status_code=400,
            )

        if not limiter.admit(client_key(request), now=now()):
            log.info("contact: throttled")
            return _rendered(
                _error_page(
                    ctx, lang, copy, copy.err_throttled, message=message, reply_to=reply_to
                ),
                status_code=429,
            )

        try:
            await deliver(build_admin_message(message=message, reply_to=reply_to, lang=lang))
        except Exception:
            # Every failure mode is the same to the sender: it did not
            # arrive, try again. The detail goes to the log, where it is
            # the operator's problem rather than an anonymous visitor's.
            log.exception("contact: delivery to the operator failed")
            return _rendered(
                _error_page(
                    ctx, lang, copy, copy.err_undeliverable, message=message, reply_to=reply_to
                ),
                status_code=502,
            )

        log.bind(lang=lang).info("contact: message delivered to the operator")
        return _rendered(_success_page(ctx, lang, copy))

    def _make_post(page_lang: str) -> Callable[[Request], Awaitable[HTMLResponse]]:
        """Bind one language into a POST handler that takes only a request.

        The language belongs to the route, not to the submission: it
        decides which copy the sender is answered in and which language
        the operator's notification is stamped with. Carrying it in a
        closure keeps it out of the handler's signature, and therefore
        out of the request (#1584).
        """

        async def _post(request: Request) -> HTMLResponse:
            return await handle_post(request, page_lang)

        return _post

    for lang in _LANGS:
        page = render_page(ctx, lang)
        headers = {
            "Cache-Control": _CACHE_CONTROL,
            "Content-Security-Policy": csp_for_html(page),
        }

        # Bind the language and the rendered page in factories —
        # without the binding both routes close over the last loop
        # value and ``/contact`` serves the English form. Same
        # late-binding trap the legal router documents at length.
        # Defaulted parameters bind just as well but are not private:
        # FastAPI reads a handler's parameters as request parameters,
        # so ``?_html=`` used to set the whole page and ``_lang`` let
        # the submitter pick the language of the operator's own
        # notification (#1584).
        path = contact_path(lang)
        router.add_api_route(
            path,
            html_page(page, headers),
            # HEAD as well as GET: this URL goes into the same places
            # the legal ones do, and a link checker that opens with HEAD
            # must not be told 405.
            methods=["GET", "HEAD"],
            response_class=HTMLResponse,
            include_in_schema=False,
            name=f"contact_{lang}",
        )
        router.add_api_route(
            path,
            _make_post(lang),
            methods=["POST"],
            response_class=HTMLResponse,
            include_in_schema=False,
            name=f"contact_{lang}_post",
        )

    return router
