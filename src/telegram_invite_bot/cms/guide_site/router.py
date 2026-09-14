"""FastAPI ``APIRouter`` for the guide pages.

Two GET routes — ``/commands`` (RU) and ``/commands/en`` (EN) — mirror
the legacy Flask paths. Mounted from :mod:`webhook.server` so the same
uvicorn process serves both Telegram webhook traffic AND the public
guide; no separate port, no separate TLS.

Each page is two halves with different lifecycles: a *generated*
command index (:mod:`command_index`, built from the same catalog
``/help`` reads, so it cannot go stale) and the *hand-written* guide
prose — whatever was last saved at ``/commands/edit``, falling back to
the ``.md`` files shipped with the deploy when nothing was. The
shell in :mod:`rendering` stitches them together and adds the search
box that spans the first half.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import time
from typing import TYPE_CHECKING, Final, NamedTuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from telegram_invite_bot.cms.client_ip import client_key
from telegram_invite_bot.cms.csp import csp_for_html
from telegram_invite_bot.cms.guide_site.command_index import (
    render_index_html,
    render_plain_note_html,
)
from telegram_invite_bot.cms.guide_site.editor import (
    render_editor_html,
    secret_configured,
    verify_secret,
)
from telegram_invite_bot.cms.guide_site.markdown import (
    extract_headings,
    md_to_html,
    strip_own_contents,
    strip_title,
)
from telegram_invite_bot.cms.guide_site.rendering import build_guide_shell, build_toc_html
from telegram_invite_bot.cms.guide_site.throttle import EditorThrottle, RefusedBy
from telegram_invite_bot.cms.nav import COMMANDS, site_nav_html
from telegram_invite_bot.cms.paths import (
    COMMANDS_PATH_EDIT,
    COMMANDS_PATH_EN,
    COMMANDS_PATH_RU,
    absolute,
    home_path,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.http_body import declared_body_too_large

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from telegram_invite_bot.cms.guide_site.context import GuideSiteContext
    from telegram_invite_bot.cms.guide_site.editor import EditorBridge

log = logging.getLogger(__name__)


# Aliases for the shared URL map in :mod:`telegram_invite_bot.cms.paths`,
# which is where the front page and the editor read the same three paths
# from. Kept as module-level names because they read better at the
# ``@router.get`` decorators below.
_PATH_RU = COMMANDS_PATH_RU
_PATH_EN = COMMANDS_PATH_EN
_PATH_EDIT = COMMANDS_PATH_EDIT

#: Five minutes at the edge. Cloudflare does not cache HTML unless the
#: response says so, and this page — command index plus markdown, on a
#: box that also runs other services — is a comparatively expensive
#: public GET the process serves. Shorter than the legal documents'
#: hour because this one *is* editable at runtime: an operator who
#: saves in ``/commands/edit`` should see the change without waiting
#: out a long TTL.
_CACHE_CONTROL: Final[str] = "public, max-age=300"

#: What the stub page gets instead (#212). The five minutes above are
#: right for a page that is merely *editable*; they are wrong for a page
#: that is a symptom. The stub means the deploy did not carry a guide
#: file, so the operator will fix it within minutes — and every second
#: of that fix is invisible for as long as an edge copy of the apology
#: survives. ``no-store`` and not a bare header: RFC 9111 §4.2.2 lets a
#: shared cache invent a freshness lifetime for a response that declares
#: none, and this origin sits behind Cloudflare (the same reasoning as
#: :func:`build_router.editor_response`, #213).
_STUB_CACHE_CONTROL: Final[str] = "no-store"

#: Shell chrome, keyed by the suffix of the ``site_<name>`` i18n key.
#: Listed explicitly rather than scanned from the catalog so a typo in
#: a template placeholder fails here, at render time, and not silently
#: as a missing word on a live page.
_LABEL_KEYS: Final[tuple[str, ...]] = (
    "subtitle",
    "lede",
    "lang_nav",
    "open_bot",
    "search_label",
    "search_placeholder",
    "search_clear",
    "search_empty",
    "commands_heading",
    "commands_sub",
    "guide_heading",
    "guide_sub",
    "toc_heading",
    "page_title",
)

#: Ceiling on a save, checked against ``Content-Length`` before the
#: body is parsed. The guides are ~40 KB (RU) and ~24 KB (EN) on disk,
#: and a browser form-encodes Cyrillic at six characters per two-byte
#: letter, so the largest honest submission is around 145 KB. 256 KiB
#: is under two times that and a quarter of the nginx default — this is
#: the layer that says no first.
_MAX_EDIT_BODY_BYTES: Final[int] = 256 * 1024

#: Per-field ceiling, checked after parsing. This is the backstop for a
#: chunked request, which declares no length and therefore slips past
#: the check above (the gap documented in
#: :mod:`telegram_invite_bot.utils.http_body`). Counted in characters
#: rather than bytes because that is what the form field holds by then;
#: the real guides are ~20k and ~24k characters, so this leaves room for
#: the text to grow several times over before an operator meets it.
_MAX_EDIT_FIELD_CHARS: Final[int] = 100_000

#: Strings the client-side search needs. Shipped as JSON, not baked
#: into the script source — see :func:`rendering.build_guide_shell`.
_JS_LABEL_KEYS: Final[dict[str, str]] = {
    "found": "site_js_found",
    "copied": "site_js_copied",
    "copyfail": "site_js_copyfail",
}


def _labels(lang: str) -> dict[str, str]:
    """Escaped shell strings. Escaping here, once, keeps the template a
    template — :func:`build_guide_shell` interpolates and nothing else.
    """
    return {name: html_lib.escape(t(f"site_{name}", lang), quote=True) for name in _LABEL_KEYS}


class GuideSource(NamedTuple):
    """The prose half of the page, plus whether it is the stub (#212).

    The flag travels with the text because the response headers depend
    on it and the caller cannot tell by looking: a stub is ordinary
    markdown by the time it reaches :func:`_render_page`.
    """

    markdown: str
    is_stub: bool


def _load_markdown(path: Path, lang: str) -> GuideSource:
    """Read a guide file, falling back to a stub on missing-file.

    A stub page (rather than a 404) matches legacy behaviour and means
    a half-applied deploy — one of the two .md files missing — still
    serves the surviving language. Operators see the stub in their
    browser and fix the deploy; users of the surviving language never
    notice.

    What the stub says is a *user-facing* string and is translated like
    every other one on the page (#212). It used to be a Russian sentence
    printed under ``<html lang="en">``, and it named the file it had
    failed to open — ``/commands/en`` is in ``sitemap.xml``, so that
    sentence was a crawlable page telling the internet a server-side
    path. The path belongs in the log, where the operator who can act
    on it is looking.

    The ``#`` title is kept although :func:`strip_title` removes it
    before render: it keeps the stub a well-formed document like the
    files it stands in for, rather than a bare paragraph that happens to
    survive the current strip.
    """
    if path.is_file():
        return GuideSource(path.read_text(encoding="utf-8"), is_stub=False)
    log.warning("guide markdown missing, serving stub: path=%s lang=%s", path, lang)
    title = "Guide" if lang == "en" else "Гайд"
    return GuideSource(f"# {title}\n\n{t('site_guide_unavailable', lang)}", is_stub=True)


def _guide_markdown(ctx: GuideSiteContext, lang: str) -> GuideSource:
    """The prose half of the page: stored override, else the file.

    The editor writes overrides through the bridge and then tells the
    operator "Страницы /commands обновлены". That sentence is only true
    if the page reads the override back — legacy rendered from the same
    settings store, so an override took effect immediately; here the
    override has to be consulted explicitly or the save would be a
    silent no-op for the reader.

    An empty override means "not set" and falls through to the file on
    disk, which is also how the editor's own preview treats it — the
    two must agree, or clearing the textarea would show the file in the
    editor and a blank page to everyone else.
    """
    path = ctx.guide_file_en if lang == "en" else ctx.guide_file_ru
    if ctx.editor_bridge is not None:
        ru, en = ctx.editor_bridge.load_overrides()
        override = en if lang == "en" else ru
        if override.strip():
            return GuideSource(override, is_stub=False)
    return _load_markdown(path, lang)


def _js_labels_json(lang: str) -> str:
    """The search/copy strings as a JSON literal safe inside ``<script>``.

    ``json.dumps`` alone is not enough: a translation containing
    ``</script>`` would close the tag and dump the rest of the JSON into
    the document as markup. Escaping ``<`` to ``\\u003c`` keeps the JSON
    valid and the tag intact.
    """
    payload = {name: t(key, lang) for name, key in _JS_LABEL_KEYS.items()}
    return json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")


def _render_page(ctx: GuideSiteContext, lang: str, source: str) -> str:
    lang = lang.lower()
    # Both strips remove what the page renders itself: the document
    # title (the shell's <h1>) and the hand-written contents list (the
    # generated <details> TOC). Applied before the TOC is extracted so
    # the removed section does not show up as an entry in its own
    # replacement.
    md = strip_own_contents(strip_title(source))
    labels = _labels(lang)
    url_ru = absolute(ctx.url_prefix, _PATH_RU)
    url_en = absolute(ctx.url_prefix, _PATH_EN)
    return build_guide_shell(
        guide_html=md_to_html(md),
        index_html=render_index_html(lang),
        plain_html=render_plain_note_html(lang),
        doc_nav_html=site_nav_html(
            url_prefix=ctx.url_prefix,
            lang=lang,
            current=COMMANDS,
            contact_enabled=ctx.contact_enabled,
        ),
        toc_html=build_toc_html(extract_headings(md), t("site_toc_heading", lang)),
        labels=labels,
        lang=lang,
        # Escape AT the boundary — the shell builder injects these
        # values directly into HTML attribute / text contexts. The
        # context object itself stores raw values so it stays useful
        # for non-HTML consumers (RSS, structured logs).
        page_title=labels["page_title"],
        site_title=html_lib.escape(ctx.site_title),
        version=html_lib.escape(ctx.version),
        # The wordmark goes to the front page, not to the RU copy of this
        # one: ``url_ru`` switches language, the brand goes home.
        home_url=html_lib.escape(absolute(ctx.url_prefix, home_path(lang)), quote=True),
        url_ru=html_lib.escape(url_ru, quote=True),
        url_en=html_lib.escape(url_en, quote=True),
        canonical=True,
        tme_url=html_lib.escape(ctx.tme_url, quote=True),
        i18n_json=_js_labels_json(lang),
    )


def _page_renderer(
    ctx: GuideSiteContext,
) -> Callable[[str], Awaitable[tuple[str, dict[str, str]]]]:
    """A render function that reuses its last output per language.

    Returns the page and the response headers that belong to it. The
    headers are cached alongside because one of them is derived from the
    page: the Content-Security-Policy names the hashes of the inline
    ``<style>`` and ``<script>`` blocks this exact render produced, so
    re-deriving it per request would mean hashing the whole page per
    request — the very work the cache exists to avoid.

    ``/commands`` is a comparatively expensive public GET: tens of
    kilobytes of markdown parsed into a page several times that size,
    measured rather than estimated (#1637: the numbers standing here
    were guesses, and the page has since outgrown the larger of them by
    three quarters, which understates exactly the cost this cache
    exists to argue about). ``Cache-Control`` keeps ordinary readers at
    the edge, but an edge cache is keyed on the full URL, so any
    request the edge has not seen before still reaches the origin, and
    an unauthenticated page has no other gate in front of it. Rendering
    per request turns that into real CPU; rendering once per *version
    of the text* turns it into a string comparison, which is what makes
    the origin cost independent of how often the page is asked for.

    Keyed on the markdown itself rather than on a timestamp or a
    counter, because that is the thing that actually decides the
    output: an editor save changes the source and the next request
    re-renders, a redeploy that rewrites the ``.md`` files does the
    same, and nothing has to remember to invalidate anything. Exactly
    one entry per language is kept, so the cache cannot grow — an
    operator who saves fifty revisions leaves fifty *replaced* entries,
    not fifty retained ones.

    Reading the source is still per request — the cache is keyed on the
    markdown, so the markdown has to be in hand before a hit can be
    declared — but it no longer happens on the event loop (#1424). It
    is a settings-file read plus a file read, and it ran inline in the
    coroutine that serves the page: every ``/commands`` GET, including
    the ones that then returned a cached string, stopped this whole
    process for the duration of two syscalls. The loop is shared with
    the bot itself, so the cost was never confined to the reader who
    paid it — a slow disk on a small box also delayed the webhook.

    Deliberately NOT also gated behind a time window. A window would
    remove the read from most requests, but it would also mean an
    operator who saves in ``/commands/edit``, is told "Страницы
    /commands обновлены" and reloads can be shown the text from before
    the save — which is the exact failure the markdown key was chosen
    to make impossible. The thread hop costs microseconds; the window
    would cost a promise.
    """
    cache: dict[str, tuple[str, str, dict[str, str]]] = {}

    async def render(lang: str) -> tuple[str, dict[str, str]]:
        source = await asyncio.to_thread(_guide_markdown, ctx, lang)
        cached = cache.get(lang)
        if cached is not None and cached[0] == source.markdown:
            return cached[1], cached[2]
        html = _render_page(ctx, lang, source.markdown)
        headers = {
            "Cache-Control": _STUB_CACHE_CONTROL if source.is_stub else _CACHE_CONTROL,
            "Content-Security-Policy": csp_for_html(html),
        }
        cache[lang] = (source.markdown, html, headers)
        return html, headers

    return render


def build_router(
    ctx: GuideSiteContext,
    *,
    now: Callable[[], float] = time.monotonic,
    throttle: EditorThrottle | None = None,
) -> APIRouter:
    """Construct the FastAPI router. Closes over ``ctx`` — recreate the
    router if the context ever needs to change at runtime (it doesn't:
    the context is frozen and built once at app startup).

    The render cache and the failed-secret limiter live in this closure
    rather than at module level so that two apps in one process — which
    the test suite builds routinely — cannot serve each other's pages
    or share each other's cooldowns.

    ``now`` and ``throttle`` are injected so the tests can drive the
    limiter without sleeping; production takes the defaults.
    """
    router = APIRouter(tags=["guide"])
    render = _page_renderer(ctx)
    limiter = throttle if throttle is not None else EditorThrottle()
    #: The untouched editor page, keyed on the texts it was built from.
    #: In this closure and not at module level for the reason the
    #: docstring above gives about the render cache: two apps in one
    #: process must not serve each other's pages.
    editor_shell: dict[str, tuple[tuple[str, str], str, str]] = {}

    def editor_response(
        html: str, status_code: int = 200, *, csp: str | None = None
    ) -> HTMLResponse:
        """The editor page under its own hash policy, never cached.

        The CSP is derived per response on every path that carries a
        draft: those bytes are genuinely different each time and there
        is nothing a cache could key on. What used to stand here said
        that of *all* responses, which was true of the ones reached by
        submitting the form and false of the one reached by opening the
        page — an untouched editor renders the same two stored texts to
        the same bytes for every anonymous caller. ``csp`` lets the GET
        hand in the policy it has already derived for that shell rather
        than re-hashing an identical page (see :func:`guide_edit_get`).
        The earlier version of this note also claimed there is "no
        crawler on the other end" — which the ``X-Robots-Tag``
        paragraph below spends twelve lines arguing is false, and the
        header would be pointless if it were true (#1638).

        ``Cache-Control: no-store`` and not merely the absence of a
        header (#213). A response with no cache directives is not
        uncacheable — RFC 9111 §4.2.2 lets a shared cache assign it a
        heuristic freshness lifetime, and this origin sits behind
        Cloudflare. The page carries the operator's unsaved draft and
        the outcome of a secret check; ``no-store`` is what actually
        says "do not keep a copy of this".

        ``X-Robots-Tag: noindex, nofollow`` (#1480). The GET is open —
        only the POST checks the shared secret — so when the editor is
        configured, anyone who reaches this path gets the full admin
        form. Writing is still impossible, but this origin is the
        address handed to an acquiring bank next to the offer and the
        privacy policy, and an admin form in a search index reads as an
        unsecured back office to a reviewer with no way to know the save
        button is inert.

        The header, and deliberately NOT ``Disallow: /commands/edit`` in
        ``robots.txt``. The two do not stack, they cancel: a crawler
        told not to fetch the page never sees this header, and Google
        indexes disallowed URLs it finds linked elsewhere — as a bare
        address with no snippet, which is precisely the outcome being
        avoided. ``robots.txt`` is also a public file, so the
        ``Disallow`` line would publish the path to everyone who asks
        for it. ``noindex`` on a crawlable response is the control that
        actually removes the page from the index;
        ``test_robots_maps_no_write_endpoints`` in
        ``tests/unit/cms/test_discovery.py`` pins the other half.
        """
        return HTMLResponse(
            html,
            status_code=status_code,
            headers={
                "Content-Security-Policy": csp if csp is not None else csp_for_html(html),
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )

    # GET and HEAD both: FastAPI does not derive one from the other, and
    # this is the page the bot's own /help links to — a link checker or
    # an uptime probe that opens with HEAD must not be told 405.
    @router.api_route(
        _PATH_RU, methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False
    )
    async def guide_ru() -> HTMLResponse:
        html, headers = await render("ru")
        return HTMLResponse(html, headers=headers)

    @router.api_route(
        _PATH_EN, methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False
    )
    async def guide_en() -> HTMLResponse:
        html, headers = await render("en")
        return HTMLResponse(html, headers=headers)

    # Editor endpoints. The route is mounted unconditionally, but a
    # deployment that cannot save — no bridge, or no GUIDES_EDIT_SECRET —
    # serves 404 rather than the disabled editor page.
    #
    # This deviates from legacy, which rendered a "secret not set"
    # notice. The deviation is deliberate: this origin is now the address
    # handed to an acquiring bank alongside the offer and the privacy
    # policy, and an admin form anyone can open on that domain reads as
    # an unsecured back office to a reviewer who has no way to know the
    # save button is inert. When the editor IS configured the page stays
    # reachable and the POST's shared secret is the gate, exactly as
    # before — this only removes a surface that could do nothing anyway.
    @router.get(_PATH_EDIT, response_class=HTMLResponse, include_in_schema=False)
    async def guide_edit_get() -> HTMLResponse:
        bridge = ctx.editor_bridge
        if bridge is None or not secret_configured():
            raise HTTPException(status_code=404)

        def initial_text(editor: EditorBridge) -> tuple[str, str]:
            """Stored overrides, falling back to disk, off the loop.

            Legacy parity in the fallback: an empty override field
            falls back to the ``.md`` files for preview, so the
            operator sees and edits the rendered text rather than a
            blank box on first open.

            Off the event loop for the same reason as the guide
            render above, and in ONE hop rather than three (#1635):
            ``load_overrides`` is a synchronous DB read and both
            fallbacks are synchronous file reads — 39 813 and 24 258
            bytes on prod — while this loop is also the one that
            delivers every Telegram update. Grouping them costs the
            operator's page one thread hand-off instead of one per
            source.

            The bridge arrives as an argument rather than being read
            from ``ctx``: the ``is None`` check above narrows the
            outer name, and that narrowing does not follow the name
            into a nested function.
            """
            ru, en = editor.load_overrides()
            if not ru.strip() and ctx.guide_file_ru.is_file():
                ru = ctx.guide_file_ru.read_text(encoding="utf-8")
            if not en.strip() and ctx.guide_file_en.is_file():
                en = ctx.guide_file_en.read_text(encoding="utf-8")
            return ru, en

        ru, en = await asyncio.to_thread(initial_text, bridge)

        # Rendered once per version of the text, for the same reason
        # ``/commands`` is (#1637) and with more force, because this
        # page is the more expensive of the two *and* the one nothing
        # authenticates: the secret gates the POST, so any anonymous
        # caller who knows the path can ask for this shell as fast as
        # they can open sockets. Escaping ~64 KB and then hashing the
        # result for the CSP both happen on the event loop — the loop
        # that also delivers every Telegram update — so per-request
        # rendering made an unauthenticated GET a way to spend the
        # bot's own scheduler. Cached, the same flood costs one read
        # off the loop and a tuple comparison on it, which is what the
        # already-public guide page costs, and there is no amplifier
        # left to aim at this path in particular.
        #
        # Keyed on the two texts themselves, exactly like the guide
        # cache, so a save invalidates by construction and nobody has
        # to remember to. One entry, because there is one shell: this
        # is the *untouched* editor, before any draft. Every response
        # that carries a draft — the 403 echo, the 413s, the save
        # confirmation — still renders and hashes per request, which is
        # correct, and all of them sit behind the limiter and the
        # secret rather than in front of them.
        cached = editor_shell.get("initial")
        if cached is not None and cached[0] == (ru, en):
            html, csp = cached[1], cached[2]
        else:
            html = render_editor_html(ru, en, "")
            csp = csp_for_html(html)
            editor_shell["initial"] = ((ru, en), html, csp)
        return editor_response(html, csp=csp)

    async def _read_fields(request: Request) -> tuple[str, str, str]:
        """The three form fields as text, ignoring anything else sent.

        ``request.form()`` yields ``UploadFile`` for a file part; those
        are coerced to ``""`` rather than read, so a multipart
        submission carrying an attachment is treated as an empty field
        instead of loading the file into memory. Reading the request
        directly, rather than declaring ``Form(...)`` parameters, is
        what lets the size checks above run *before* the body is
        parsed — FastAPI parses the whole form to bind those
        parameters, so a handler that declares them has already paid
        for the body by the time its first line runs.
        """
        form = await request.form()

        def field(name: str) -> str:
            value = form.get(name)
            return value if isinstance(value, str) else ""

        return field("ru_text"), field("en_text"), field("secret")

    @router.post(_PATH_EDIT, response_class=HTMLResponse, include_in_schema=False)
    async def guide_edit_post(request: Request) -> HTMLResponse:
        # Same gate as the GET, and for the same reason: on a
        # deployment that cannot save, the editor does not exist. A 503
        # carrying the editor's own HTML would hand anyone who probes
        # the path a description of the admin surface and the name of
        # the environment variable that unlocks it — an unnecessary
        # disclosure for a branch the operator can no longer reach
        # through the UI anyway.
        bridge = ctx.editor_bridge
        if bridge is None or not secret_configured():
            raise HTTPException(status_code=404)

        # ``require_declared_length`` for the same reason as the
        # contact form (#1633): the per-field ceiling below is a
        # backstop, not a bound on memory — it can only speak after
        # ``request.form()`` has already buffered the whole stream,
        # and Starlette's urlencoded parser buffers it without a
        # ceiling. A request that declares no length is refused here
        # instead. The editor is reached by a browser form, which
        # always declares one.
        if declared_body_too_large(
            request, max_bytes=_MAX_EDIT_BODY_BYTES, require_declared_length=True
        ):
            log.info("guide editor: oversized body refused unparsed")
            return editor_response(
                render_editor_html("", "", "Слишком большой запрос."),
                status_code=413,
            )

        client = client_key(request)
        stamp = now()
        # Asked before the body is read and before the secret is
        # compared. A caller who is out of guesses is told so without
        # learning anything about the secret they sent, and without
        # this process parsing a form on their behalf.
        refused_by = limiter.allow_attempt(client, now=stamp)
        if refused_by is not None:
            # One response for both refusals — telling them apart on the
            # wire would hand back the bit the limit exists to ration.
            # The journal is where they differ: a single client running
            # out is routine, whereas an empty site-wide budget also
            # locks the operator out, and an invisible lockout is a
            # defect nobody can diagnose.
            if refused_by is RefusedBy.SITE:
                log.warning(
                    "guide editor: site-wide guess budget exhausted; the editor is "
                    "shut for everyone, operator included, until it refills"
                )
            else:
                log.info("guide editor: too many failed secret attempts from one client")
            return editor_response(
                render_editor_html("", "", "Слишком много попыток. Подождите."),
                status_code=429,
            )

        ru_text, en_text, secret = await _read_fields(request)

        # The chunked-body backstop. Nothing legitimate reaches this,
        # and echoing an unbounded draft back through ``html.escape``
        # and the CSP hasher is the amplification #211 was about.
        if len(ru_text) > _MAX_EDIT_FIELD_CHARS or len(en_text) > _MAX_EDIT_FIELD_CHARS:
            log.info("guide editor: oversized field refused after parse")
            return editor_response(
                render_editor_html("", "", "Слишком большой текст."),
                status_code=413,
            )

        if not verify_secret(secret):
            limiter.note_failure(client, now=stamp)
            # The draft *is* echoed here, unlike the refusals above: a
            # mistyped secret is the one failure an operator recovers
            # from by resubmitting, and losing 40 KB of edits to a
            # typo would be a worse bug than the one this guards. It
            # is safe to echo because the two ceilings above bound it.
            return editor_response(
                render_editor_html(ru_text, en_text, "Неверный секрет."),
                status_code=403,
            )
        try:
            # The write goes off the loop too (#1635 covers only the
            # reads, but this is the same synchronous bridge on the
            # same loop, and a save is the slower of the two).
            await asyncio.to_thread(bridge.save_overrides, ru_text, en_text)
        except Exception:
            # #1485. The exception text is no longer echoed: an
            # ``OSError`` from ``JsonFileEditorBridge`` carries the
            # absolute server path of the settings file, and this was
            # the one raise in ``cms/`` that reached a response without
            # being logged at all. Same split :func:`_load_markdown`
            # already makes for a missing guide file — the path goes to
            # the journal, where the operator who can act on it is
            # looking, and the page says only that something is wrong.
            #
            # ``log.exception`` rather than ``log.error``: the whole
            # point of dropping the text from the response is that the
            # traceback has to survive somewhere.
            log.exception("guide editor: saving overrides failed")
            return editor_response(
                render_editor_html(ru_text, en_text, "Ошибка сохранения. Подробности в журнале."),
                status_code=500,
            )
        return editor_response(
            render_editor_html(ru_text, en_text, "Сохранено. Страницы /commands обновлены."),
        )

    return router
