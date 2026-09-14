"""End-to-end test of the FastAPI guide router (no aiogram/webhook).

We mount the guide router on a bare ``FastAPI`` app — same shape the
production webhook server uses, minus the Telegram surface — and hit
the routes with ``TestClient``. The router is fully synchronous from
the outside (HTML in, HTML out), so this verifies:

* both routes serve 200 + ``text/html``
* the active-language pill is on the correct side
* a missing source file degrades to the stub page (not a 5xx)
* URL prefix from settings ends up in nav links
* the per-request source read happens off the event loop (#1424),
  and so does everything the editor page reads and writes (#1635)
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_invite_bot.cms.csp import csp_for_html
from telegram_invite_bot.cms.guide_site import GuideSiteContext, build_router
from telegram_invite_bot.cms.guide_site import router as router_mod
from telegram_invite_bot.cms.guide_site.editor import InMemoryEditorBridge

#: A run of Cyrillic letters, optionally spanning the space inside a
#: two-word trigger («кто я»), so a failure names whole words rather
#: than a pile of single characters.
_CYRILLIC_RUN = re.compile(r"[\u0400-\u04FF]+(?: [\u0400-\u04FF]+)*")

#: The module logger's name, for :func:`caplog.at_level` — the stub path
#: is reported to the operator through the log, not through the page.
_ROUTER_LOGGER = router_mod.log.name


def _ctx(tmp_path: Path, *, with_files: bool = True, url_prefix: str = "") -> GuideSiteContext:
    ru = tmp_path / "telegraph_guide_ru.md"
    en = tmp_path / "telegraph_guide_en.md"
    if with_files:
        # A document title plus one section — the title is expected to
        # be dropped (the shell owns the page heading), the section is
        # expected to survive with an id the TOC can target.
        ru.write_text("# Гайд\n\n## Заголовок\n\n- пункт\n", encoding="utf-8")
        en.write_text("# Guide\n\n## Heading\n\n- item\n", encoding="utf-8")
    return GuideSiteContext(
        guide_file_ru=ru,
        guide_file_en=en,
        site_title="MyBot",
        version="1.2.3",
        bot_username="my_test_bot",
        url_prefix=url_prefix,
    )


def _client(ctx: GuideSiteContext) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(ctx))
    return TestClient(app)


def test_ru_route_serves_html_with_ru_active(tmp_path: Path) -> None:
    client = _client(_ctx(tmp_path))
    resp = client.get("/commands")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert '<h3 id="заголовок">Заголовок</h3>' in body
    assert "Гайд" not in body  # the document title is the shell's job
    assert "MyBot" in body
    # The generated index rides on the same page as the prose, and the
    # section heading reached the table of contents.
    assert 'data-copy="/help"' in body
    assert 'id="q"' in body
    assert '<a href="#заголовок">Заголовок</a>' in body
    # The active-language marker is on RU and only on RU. Asserted as
    # whole anchors rather than a bare class substring so the pair
    # cannot both light up (or both go dark) and still pass.
    assert '<a class="lang is-on" href="/commands" hreflang="ru">RU</a>' in body
    assert '<a class="lang" href="/commands/en" hreflang="en">EN</a>' in body
    # tg link picked up the username.
    assert "https://t.me/my_test_bot" in body


def test_en_route_serves_english_heading(tmp_path: Path) -> None:
    client = _client(_ctx(tmp_path))
    resp = client.get("/commands/en")
    assert resp.status_code == 200
    body = resp.text
    assert '<h3 id="heading">Heading</h3>' in body
    assert 'lang="en"' in body
    # English-side shell copy proves we picked the EN branch all the
    # way through the shell, not just the markdown body.
    assert "Open the bot in Telegram" in body
    assert "All commands" in body
    # Mirror of the RU assertion: the marker moved with the language.
    assert '<a class="lang is-on" href="/commands/en" hreflang="en">EN</a>' in body


def test_missing_source_file_renders_stub_not_500(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A partial deploy (one .md file missing) must keep the surviving
    language working, and the missing-language page must degrade to a
    stub rather than a 5xx.

    This test used to assert the opposite of its last line: that the
    filename is *visible on the page*, "so the operator can fix the
    deploy". The operator is not the audience — ``/commands/en`` is
    listed in ``sitemap.xml``, so that sentence was a crawlable page
    handing the internet a server-side path, and the operator was never
    watching the page anyway. The path goes to the log now (#212).
    """
    with caplog.at_level(logging.WARNING, logger=_ROUTER_LOGGER):
        client = _client(_ctx(tmp_path, with_files=False))
        resp = client.get("/commands")
    assert resp.status_code == 200
    assert "telegraph_guide_ru.md" not in resp.text
    assert "не найден на сервере" not in resp.text
    assert "telegraph_guide_ru.md" in caplog.text


@pytest.mark.parametrize("path", ["/commands", "/commands/en"])
def test_the_stub_page_is_never_cached(tmp_path: Path, path: str) -> None:
    """The stub is a symptom, and a cached symptom outlives its cause.

    A real guide page is cacheable for five minutes because it is merely
    *editable*. The stub means the deploy did not carry a guide file, so
    it is about to stop being true — within minutes, by hand. An edge
    copy would keep serving the apology to everyone for the rest of its
    TTL after the fix landed, which is the one case where the cache is
    working against the operator (#212).
    """
    headers = _client(_ctx(tmp_path, with_files=False)).get(path).headers
    assert headers["cache-control"] == "no-store"


def test_the_english_stub_carries_no_russian_word(tmp_path: Path) -> None:
    """#176's rule does not get a pass on the failure path (#212).

    The stub was the one string on the English page that had never been
    translated: ``<html lang="en">`` with «Файл … не найден на сервере.»
    underneath it. It is exactly the scenario #177 produced in
    production — the deploy did not carry ``telegraph_guide_*.md`` — so
    the page nobody plans for was the page a reader was most likely to
    hit while something was already wrong.
    """
    body = _client(_ctx(tmp_path, with_files=False)).get("/commands/en").text
    cyrillic = sorted({match.group() for match in _CYRILLIC_RUN.finditer(body)})
    assert not cyrillic, f"Russian text on the English stub: {cyrillic[:20]}"


def test_url_prefix_flows_into_nav_links(tmp_path: Path) -> None:
    """When the bot is fronted by a reverse proxy that rewrites the
    request path, the nav links must point at the absolute public URL
    instead of bare ``/commands``.
    """
    client = _client(_ctx(tmp_path, url_prefix="https://bot.example.com"))
    body = client.get("/commands").text
    assert 'href="https://bot.example.com/commands"' in body
    assert 'href="https://bot.example.com/commands/en"' in body


@pytest.mark.parametrize("bad", ["", "  ", "name with space", "evil/path"])
def test_invalid_bot_username_falls_back_to_generic_link(tmp_path: Path, bad: str) -> None:
    """A misconfigured ``BOT_USERNAME`` (space, slash, empty) must NOT
    produce a broken Telegram link or — worse — an injection-shaped
    href. Falls back to plain ``https://t.me`` and lets the user pick
    the bot manually.
    """
    ctx = _ctx(tmp_path)
    bad_ctx = GuideSiteContext(
        guide_file_ru=ctx.guide_file_ru,
        guide_file_en=ctx.guide_file_en,
        site_title=ctx.site_title,
        version=ctx.version,
        bot_username=bad,
        url_prefix=ctx.url_prefix,
    )
    body = _client(bad_ctx).get("/commands").text
    assert 'href="https://t.me"' in body
    assert "https://t.me/" + bad not in body


def test_saved_override_is_what_the_page_serves(tmp_path: Path) -> None:
    """The editor's success message must be true.

    Saving in ``/commands/edit`` writes through the bridge and tells
    the operator the pages are updated. If the page kept rendering the
    ``.md`` file on disk, the save would be a silent no-op for every
    reader — the operator's only clue would be that nothing changed.
    """
    ctx = _ctx(tmp_path)
    bridge = InMemoryEditorBridge(ru="# Гайд\n\n## Из редактора\n", en="")
    edited = GuideSiteContext(
        guide_file_ru=ctx.guide_file_ru,
        guide_file_en=ctx.guide_file_en,
        site_title=ctx.site_title,
        version=ctx.version,
        bot_username=ctx.bot_username,
        url_prefix=ctx.url_prefix,
        editor_bridge=bridge,
    )
    client = _client(edited)
    ru = client.get("/commands").text
    assert "Из редактора" in ru
    assert "Заголовок" not in ru, "the file on disk must not win over a saved override"
    # An empty override for the other language is "not set", not "blank
    # page" — English still falls back to its file.
    assert "Heading" in client.get("/commands/en").text


def test_repeat_requests_reuse_the_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache-busting query string must not cost a fresh render.

    ``Cache-Control`` protects the origin only from readers who let the
    edge answer. ``?v=1``, ``?v=2``, … are distinct URLs to Cloudflare,
    so they arrive here every time — and this is a 74 KB page built
    from a 40 KB markdown document, on a box that also runs a mail
    server. The render has to be reused as long as the text has not
    changed.
    """
    client = _client(_ctx(tmp_path))
    first = client.get("/commands").text

    # From here on, rendering at all is a failure. Patched after the
    # first request so the cache is warm and the assertion is about
    # reuse rather than about lazy construction.
    def forbidden(ctx: GuideSiteContext, lang: str, source: str) -> str:
        raise AssertionError("page re-rendered despite unchanged source")

    monkeypatch.setattr(router_mod, "_render_page", forbidden)
    for v in range(3):
        assert client.get(f"/commands?v={v}").text == first


def test_saving_an_override_invalidates_the_render(tmp_path: Path) -> None:
    """The cache must never outlive the text it was built from.

    This is the failure mode that would make the previous test a
    liability: an operator saves in ``/commands/edit``, is told the
    pages are updated, and the reader keeps getting the render from
    before the save. Keying on the source itself is what prevents it —
    but only a test proves the key is actually consulted.
    """
    base = _ctx(tmp_path)
    bridge = InMemoryEditorBridge(ru="", en="")
    ctx = GuideSiteContext(
        guide_file_ru=base.guide_file_ru,
        guide_file_en=base.guide_file_en,
        site_title=base.site_title,
        version=base.version,
        bot_username=base.bot_username,
        url_prefix=base.url_prefix,
        editor_bridge=bridge,
    )
    client = _client(ctx)
    assert "Заголовок" in client.get("/commands").text  # warms the cache from disk

    bridge.save_overrides("# Гайд\n\n## После сохранения\n", "")
    body = client.get("/commands").text
    assert "После сохранения" in body
    assert "Заголовок" not in body


def test_repeat_opens_of_the_editor_reuse_the_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The editor shell is rendered once per version of the text.

    The same argument as ``/commands`` above, with the stakes raised:
    this page is the larger of the two — it carries both guides in
    full inside ``<textarea>`` elements — and nothing authenticates
    the GET, because the secret gates only the POST. Escaping ~64 KB
    and hashing the result for the CSP both run on the event loop that
    delivers Telegram updates, so an anonymous caller who knew the
    path could spend the bot's scheduler at the rate they could open
    sockets. Reusing the render leaves that flood costing what the
    already-public guide page costs.

    ``no-store`` still stands on every response (asserted separately):
    this is the origin declining to rebuild identical bytes, not the
    page becoming cacheable anywhere else.
    """
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "s3cret")
    base = _ctx(tmp_path)
    client = _client(
        GuideSiteContext(
            guide_file_ru=base.guide_file_ru,
            guide_file_en=base.guide_file_en,
            site_title=base.site_title,
            version=base.version,
            bot_username=base.bot_username,
            url_prefix=base.url_prefix,
            editor_bridge=InMemoryEditorBridge(),
        )
    )
    first = client.get("/commands/edit")
    assert first.status_code == 200

    def forbidden(ru: str, en: str, notice: str) -> str:
        raise AssertionError("editor re-rendered despite unchanged text")

    monkeypatch.setattr(router_mod, "render_editor_html", forbidden)
    for v in range(3):
        again = client.get(f"/commands/edit?v={v}")
        assert again.text == first.text
        # The policy is reused with the page, not recomputed from it —
        # the hashing is half of what the cache exists to avoid.
        assert (
            again.headers["content-security-policy"] == (first.headers["content-security-policy"])
        )


def test_saving_an_override_invalidates_the_editor_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell must never outlive the text it was built from.

    The mirror of the guide's invalidation test, and the failure that
    would make the reuse above a liability: an operator saves, reopens
    the editor and is handed their own pre-save text to edit — which
    would quietly undo the save on the next submission.
    """
    base = _ctx(tmp_path)
    bridge = InMemoryEditorBridge(ru="", en="")
    ctx = GuideSiteContext(
        guide_file_ru=base.guide_file_ru,
        guide_file_en=base.guide_file_en,
        site_title=base.site_title,
        version=base.version,
        bot_username=base.bot_username,
        url_prefix=base.url_prefix,
        editor_bridge=bridge,
    )
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "s3cret")
    client = _client(ctx)
    assert "Заголовок" in client.get("/commands/edit").text  # warms from disk

    bridge.save_overrides("# Гайд\n\n## После сохранения\n", "")
    body = client.get("/commands/edit").text
    assert "После сохранения" in body
    assert "Заголовок" not in body


@pytest.mark.parametrize("path", ["/commands", "/commands/en"])
def test_guide_pages_are_cacheable_at_the_edge(tmp_path: Path, path: str) -> None:
    """Cloudflare caches HTML only when told to.

    This is the heaviest public GET the process serves, on a host that
    also runs other services, so every hit the edge absorbs
    is one the origin does not pay for.
    """
    headers = _client(_ctx(tmp_path)).get(path).headers
    assert "max-age" in headers.get("cache-control", "")


@pytest.mark.parametrize("path", ["/commands", "/commands/en"])
def test_head_is_answered_not_rejected(tmp_path: Path, path: str) -> None:
    """FastAPI does not derive HEAD from GET.

    This is the page the bot's own ``/help`` links to, so a 405 here is
    what a link checker would report as the bot advertising a dead URL.
    """
    resp = _client(_ctx(tmp_path)).head(path)
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-length"] != "0"


@pytest.mark.parametrize("path", ["/commands", "/commands/en"])
def test_the_page_permits_its_own_search_script(tmp_path: Path, path: str) -> None:
    """The only page on the site that runs script.

    A policy that names no script hash would not error — the box would
    simply stop filtering, which is the kind of breakage nobody reports.
    """
    resp = _client(_ctx(tmp_path)).get(path)
    policy = resp.headers["content-security-policy"]
    assert "unsafe-inline" not in policy
    assert "script-src 'sha256-" in policy
    assert "style-src 'sha256-" in policy


def test_the_policy_describes_the_page_it_ships_with(tmp_path: Path) -> None:
    """The header is cached next to the HTML, so it can go stale.

    An override changes the prose but not the shell's inline blocks, so
    the two policies are expected to be *equal* here — what must hold is
    that each response's policy is the one derived from that response's
    own body. Comparing against a fresh derivation is what would catch
    a cache that hands out a header built from different markup.
    """
    base = _ctx(tmp_path)
    bridge = InMemoryEditorBridge(ru="", en="")
    ctx = GuideSiteContext(
        guide_file_ru=base.guide_file_ru,
        guide_file_en=base.guide_file_en,
        site_title=base.site_title,
        version=base.version,
        bot_username=base.bot_username,
        url_prefix=base.url_prefix,
        editor_bridge=bridge,
    )
    client = _client(ctx)
    before = client.get("/commands")
    bridge.save_overrides("# Гайд\n\n## После сохранения\n", "")
    after = client.get("/commands")

    assert before.text != after.text, "the override did not reach the page"
    for resp in (before, after):
        assert resp.headers["content-security-policy"] == csp_for_html(resp.text)


def test_the_editor_page_carries_a_policy_and_is_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It embeds the operator's draft and takes the edit secret.

    Both are reasons this page must not be cached anywhere, and the
    draft is also the one place on the site where text a human typed is
    rendered back into HTML — the policy is the backstop if it is ever
    interpolated somewhere the escaping does not reach.
    """
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "s3cret")
    base = _ctx(tmp_path)
    ctx = GuideSiteContext(
        guide_file_ru=base.guide_file_ru,
        guide_file_en=base.guide_file_en,
        site_title=base.site_title,
        version=base.version,
        bot_username=base.bot_username,
        url_prefix=base.url_prefix,
        editor_bridge=InMemoryEditorBridge(),
    )
    response = _client(ctx).get("/commands/edit")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == csp_for_html(response.text)
    assert "sha256-" in response.headers["content-security-policy"]
    # Explicitly ``no-store``, not merely a missing header (#213): a
    # response carrying no cache directives is still eligible for a
    # shared cache's heuristic freshness, and this one sits behind
    # Cloudflare with the operator's draft in it.
    assert response.headers["cache-control"] == "no-store"


def test_the_english_page_carries_no_russian_word(tmp_path: Path) -> None:
    """``/commands/en`` must be readable end to end by someone who does
    not read Cyrillic (#176).

    The catalog and the alias table are deliberately bilingual — «баланс»
    and ``balance`` reach the same handler — so *rendering* is the only
    place that can know which spelling a given reader can use. Before
    #176 neither ``synonym_aliases`` nor ``plain_triggers_by_command``
    took the page language into account, and the English page printed
    213 Cyrillic tokens: alias chips (``/вывод``, ``/мои_обращения``),
    plain triggers («кто я», «дейли») and the «бот » group prefix. Each
    one is a word an English reader cannot read, type or look up.

    Asserted over the whole response rather than over the index
    fragment, because the stylesheet is inlined into every page too and
    a Cyrillic example inside a CSS comment ships just as far as one in
    the markup. The site title is the single sanctioned exception: it is
    the operator's chosen brand, and transliterating a brand per page
    would be a different (and wrong) kind of fix — this fixture's title
    is Latin, so nothing needs excluding here.
    """
    body = _client(_ctx(tmp_path)).get("/commands/en").text
    cyrillic = sorted({match.group() for match in _CYRILLIC_RUN.finditer(body)})
    assert not cyrillic, f"Russian text on the English page: {cyrillic[:20]}"


def test_the_russian_page_keeps_the_latin_spellings(tmp_path: Path) -> None:
    """The filter is one-directional, and that asymmetry is deliberate.

    ``/balance`` genuinely works for a Russian speaker, so hiding it on
    ``/commands`` would delete a working trigger from the page that
    documents them. Guarded because "strip the other language" is the
    obvious symmetric refactor of #176, and it would be a regression.
    """
    body = _client(_ctx(tmp_path)).get("/commands").text
    assert 'data-copy="/balance"' in body
    assert "баланс" in body


def test_the_source_read_does_not_happen_on_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1424 — serving /commands must not read files inside the loop.

    The render cache is keyed on the markdown itself, so the source is
    read on every request, hit or miss: a settings-file read plus a
    file read. Run inline they stop the whole process for their
    duration, and this loop is the bot's own — on the small box that
    also runs other services, a slow disk delayed the webhook
    and not merely the reader who asked for the page. ``?v=1``, ``?v=2``
    is cached per full URL by Cloudflare, so those requests all reach
    the origin and none of them can be assumed rare.

    Asserted through the property rather than by looking for a call to
    ``asyncio.to_thread``: a worker thread has no running loop of its
    own, so ``get_running_loop`` raising inside the read IS "this ran
    off the loop", and the check keeps holding however the offload is
    spelled later.
    """
    real = router_mod._guide_markdown  # noqa: SLF001 — the unit under test
    off_loop: list[bool] = []

    def _spy(ctx: GuideSiteContext, lang: str) -> router_mod.GuideSource:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            off_loop.append(True)
        else:
            off_loop.append(False)
        return real(ctx, lang)

    monkeypatch.setattr(router_mod, "_guide_markdown", _spy)

    assert _client(_ctx(tmp_path)).get("/commands").status_code == 200
    assert off_loop == [True], "the guide source was read on the event loop"


class _LoopSpyBridge:
    """An editor bridge that remembers when it was called on the loop.

    Same trick as the spy above: a worker thread has no running loop
    of its own, so ``get_running_loop`` raising IS the property under
    test, and the assertion keeps holding however the offload is
    spelled later.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.on_loop: list[str] = []
        self._ru = ""
        self._en = ""

    def _record(self, what: str) -> None:
        self.calls.append(what)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.on_loop.append(what)

    def load_overrides(self) -> tuple[str, str]:
        self._record("load")
        return self._ru, self._en

    def save_overrides(self, ru: str, en: str) -> None:
        self._record("save")
        self._ru, self._en = ru.strip(), en.strip()


def test_the_editor_touches_the_bridge_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1635 — neither half of the editor blocks the loop.

    The GET used to do three synchronous calls inline — the override
    read plus a fallback read of each ``.md`` file, 39 813 and 24 258
    bytes on prod — and the POST did its save the same way. This is
    the loop that also delivers every Telegram update, so the cost
    was never paid by the operator alone.

    One assertion covers both file reads too: they live inside the
    same function the bridge read opens, so a hop that carried the
    bridge carried them with it.
    """
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "s3cret")
    ctx = _ctx(tmp_path)
    bridge = _LoopSpyBridge()
    edited = GuideSiteContext(
        guide_file_ru=ctx.guide_file_ru,
        guide_file_en=ctx.guide_file_en,
        site_title=ctx.site_title,
        version=ctx.version,
        bot_username=ctx.bot_username,
        url_prefix=ctx.url_prefix,
        editor_bridge=bridge,
    )
    client = _client(edited)

    assert client.get("/commands/edit").status_code == 200
    saved = client.post(
        "/commands/edit",
        data={"ru_text": "# Гайд\n", "en_text": "# Guide\n", "secret": "s3cret"},
    )
    assert saved.status_code == 200

    # Both halves reached the bridge: an empty ``on_loop`` would
    # otherwise also be the answer for a router that never called it.
    assert bridge.calls == ["load", "save"], bridge.calls
    assert bridge.on_loop == [], bridge.on_loop
