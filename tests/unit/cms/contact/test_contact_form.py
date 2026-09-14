"""The one endpoint on this site that a stranger can make the bot send.

Everything else the public site serves is a string decided at startup.
``POST /contact`` is different: an unauthenticated visitor supplies text
that is delivered into the owner's own Telegram chat. So the properties
defended here are the ones whose failure is either a hole (unescaped
text reaching the page or the wire, a limit that does not hold) or a
lie (a success page for a message that never arrived).
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_invite_bot.cms.contact import build_router
from telegram_invite_bot.cms.contact.content import copy_for
from telegram_invite_bot.cms.contact.form import (
    MAX_MESSAGE_CHARS,
    MAX_REPLY_TO_CHARS,
    Rejection,
    rejection_text,
    validate,
)
from telegram_invite_bot.cms.contact.notification import build_admin_message, fits_telegram
from telegram_invite_bot.cms.contact.throttle import ContactThrottle
from telegram_invite_bot.cms.legal.context import LegalContext
from telegram_invite_bot.cms.paths import contact_path

LANGS = ("ru", "en")


def _ctx(**overrides: object) -> LegalContext:
    base: dict[str, object] = {
        "site_title": "ком17",
        "operator": "ком17",
        "bot_username": "kom17_bot",
        "url_prefix": "https://tgbot.delabs.space",
        "contact_enabled": True,
    }
    base.update(overrides)
    return LegalContext(**base)  # type: ignore[arg-type]


class _Recorder:
    """A delivery callable that remembers, and optionally explodes."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def __call__(self, text: str) -> None:
        if self.fail:
            msg = "telegram is down"
            raise RuntimeError(msg)
        self.sent.append(text)


def _client(
    recorder: _Recorder | None = None,
    *,
    ctx: LegalContext | None = None,
    throttle: ContactThrottle | None = None,
    now: float = 0.0,
) -> tuple[TestClient, _Recorder]:
    rec = recorder or _Recorder()
    app = FastAPI()
    app.include_router(
        build_router(
            ctx or _ctx(),
            deliver=rec,
            now=lambda: now,
            # A limiter with a fresh bucket per test, so one test's
            # burst cannot make the next one flaky.
            throttle=throttle or ContactThrottle(),
        )
    )
    return TestClient(app), rec


# --- validation -----------------------------------------------------


def test_both_fields_are_required() -> None:
    assert validate(message="", reply_to="a@b.c", honeypot="") is Rejection.EMPTY
    assert validate(message="hi", reply_to="", honeypot="") is Rejection.EMPTY
    assert validate(message="   ", reply_to="   ", honeypot="") is Rejection.EMPTY


def test_a_complete_submission_passes() -> None:
    assert validate(message="hi", reply_to="a@b.c", honeypot="") is None


def test_the_limits_hold() -> None:
    assert validate(message="x" * MAX_MESSAGE_CHARS, reply_to="a", honeypot="") is None
    assert (
        validate(message="x" * (MAX_MESSAGE_CHARS + 1), reply_to="a", honeypot="")
        is Rejection.TOO_LONG
    )
    assert (
        validate(message="hi", reply_to="x" * (MAX_REPLY_TO_CHARS + 1), honeypot="")
        is Rejection.REPLY_TOO_LONG
    )


def test_the_honeypot_wins_over_every_other_complaint() -> None:
    """A bot must not be told which check it failed.

    An oversized payload with the decoy filled has to come back as the
    honeypot rejection — the caller answers that with the success page,
    while a length complaint would tell the sender exactly what to fix.
    """
    assert validate(message="x" * 99_999, reply_to="", honeypot="spam") is Rejection.HONEYPOT


def test_the_honeypot_has_no_sender_facing_text() -> None:
    with pytest.raises(ValueError, match="never shown"):
        rejection_text(Rejection.HONEYPOT, copy_for("ru"))


# --- the operator's notification ------------------------------------


def test_the_notification_escapes_sender_text() -> None:
    """It is sent with ``parse_mode="HTML"``.

    Unescaped, a ``<b>`` in a submission renders as markup in the
    owner's chat and an unbalanced ``<`` makes Telegram refuse the whole
    delivery — the sender would have silenced their own message.
    """
    out = build_admin_message(message="<b>bold</b> & <", reply_to="<script>", lang="ru")
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; &lt;" in out
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


@pytest.mark.parametrize(
    "body",
    [
        # Astral plane: one character in the form, two UTF-16 units on
        # the wire — 2000 of them are 4000 units before escaping.
        "\U0001f600" * MAX_MESSAGE_CHARS,
        # Every character escapes to five.
        "&" * MAX_MESSAGE_CHARS,
        "<" * MAX_MESSAGE_CHARS,
    ],
    ids=["astral", "ampersands", "angles"],
)
def test_the_notification_fits_telegram_for_adversarial_input(body: str) -> None:
    """The sender picks the input, so "it fits in practice" is not enough."""
    out = build_admin_message(message=body, reply_to="x" * MAX_REPLY_TO_CHARS, lang="ru")
    assert fits_telegram(out)


def test_a_clamped_message_says_so() -> None:
    out = build_admin_message(message="\U0001f600" * MAX_MESSAGE_CHARS, reply_to="a", lang="ru")
    assert "обрезано" in out


def test_a_short_message_is_not_marked_as_clamped() -> None:
    out = build_admin_message(message="привет", reply_to="a", lang="ru")
    assert "обрезано" not in out


def test_the_page_language_is_reported() -> None:
    assert "<b>Язык страницы:</b> EN" in build_admin_message(message="hi", reply_to="a", lang="en")
    assert "<b>Язык страницы:</b> RU" in build_admin_message(message="hi", reply_to="a", lang="ru")


# --- the throttle ---------------------------------------------------


def test_a_client_gets_three_then_waits() -> None:
    t = ContactThrottle()
    assert [t.admit("1.2.3.4", now=0.0) for _ in range(4)] == [True, True, True, False]


def test_a_bucket_refills() -> None:
    t = ContactThrottle()
    for _ in range(3):
        t.admit("1.2.3.4", now=0.0)
    assert t.admit("1.2.3.4", now=599.0) is False
    assert t.admit("1.2.3.4", now=601.0) is True


def test_one_client_cannot_spend_the_shared_bucket() -> None:
    """A per-client reject must not touch the global bucket.

    Otherwise three impatient retries from one person would drain the
    allowance of everyone else on the site.
    """
    t = ContactThrottle()
    for _ in range(20):
        t.admit("1.2.3.4", now=0.0)
    # The global bucket holds 30 and has seen only this client's first
    # three, so 27 other clients still get through.
    assert all(t.admit(f"10.0.0.{i}", now=0.0) for i in range(27))


def test_a_global_refusal_does_not_charge_the_sender() -> None:
    """Being caught in someone else's flood must not cost a token.

    If it did, a distributed flood would also lock out every individual
    person for ten minutes each — turning a shared limit into a targeted
    one.
    """
    t = ContactThrottle()
    for i in range(30):
        assert t.admit(f"10.0.0.{i}", now=0.0) is True
    victim = "1.2.3.4"
    assert t.admit(victim, now=0.0) is False  # global bucket empty
    # One minute later the global bucket has a token, and the victim
    # still has all three of theirs.
    assert [t.admit(victim, now=61.0), t.admit(victim, now=121.0)] == [True, True]


# --- routes ---------------------------------------------------------


def test_both_languages_are_routed() -> None:
    client, _ = _client()
    for lang in LANGS:
        assert client.get(contact_path(lang)).status_code == 200
        assert client.head(contact_path(lang)).status_code == 200


@pytest.mark.parametrize("lang", LANGS)
def test_each_route_serves_its_own_language(lang: str) -> None:
    """The late-binding guard: both routes closing over the loop variable
    is a mistake whose only symptom is the wrong language on ``/contact``.
    """
    client, _ = _client()
    body = client.get(contact_path(lang)).text
    assert copy_for(lang).title in body
    other = "en" if lang == "ru" else "ru"
    assert copy_for(other).submit_label not in body


def test_the_get_page_is_cacheable_and_carries_its_own_policy() -> None:
    client, _ = _client()
    resp = client.get(contact_path("ru"))
    assert resp.headers["cache-control"] == "public, max-age=3600"
    assert "unsafe-inline" not in resp.headers["content-security-policy"]
    assert "sha256-" in resp.headers["content-security-policy"]


# --- the POST -------------------------------------------------------


def test_a_valid_submission_is_delivered() -> None:
    client, rec = _client()
    resp = client.post(contact_path("ru"), data={"message": "вопрос", "reply_to": "a@b.c"})
    assert resp.status_code == 200
    assert len(rec.sent) == 1
    assert "вопрос" in rec.sent[0]
    assert "a@b.c" in rec.sent[0]


def test_the_success_page_drops_the_form() -> None:
    """So a reload cannot re-send the same message by accident."""
    client, _ = _client()
    resp = client.post(contact_path("ru"), data={"message": "вопрос", "reply_to": "a@b.c"})
    assert "<form" not in resp.text
    assert copy_for("ru").ok_title in resp.text


def test_a_post_response_is_never_cached() -> None:
    """It carries one sender's own text; the edge must not hand it on."""
    client, _ = _client()
    resp = client.post(contact_path("ru"), data={"message": "вопрос", "reply_to": "a@b.c"})
    assert resp.headers["cache-control"] == "no-store"


def test_a_rejection_keeps_the_sender_text_and_escapes_it() -> None:
    client, rec = _client()
    resp = client.post(
        contact_path("ru"), data={"message": "<script>alert(1)</script>", "reply_to": ""}
    )
    assert resp.status_code == 400
    assert rec.sent == []
    assert "<script>" not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text


def test_a_rejection_panel_is_headed_by_the_verdict_not_the_page_title() -> None:
    """The page title over a red panel reads as a section, not as a refusal."""
    client, _ = _client()
    copy = copy_for("ru")
    resp = client.post(contact_path("ru"), data={"message": "вопрос", "reply_to": ""})
    assert resp.status_code == 400
    assert f'<p class="head">{copy.err_title}</p>' in resp.text
    assert f'<p class="head">{copy.title}</p>' not in resp.text


def test_a_rejected_oversized_paste_is_not_echoed_whole() -> None:
    client, _ = _client()
    resp = client.post(contact_path("ru"), data={"message": "x" * 60_000, "reply_to": "a@b.c"})
    assert resp.status_code == 400
    assert "x" * (MAX_MESSAGE_CHARS + 2) not in resp.text


def test_the_honeypot_is_answered_like_a_success_and_delivers_nothing() -> None:
    client, rec = _client()
    resp = client.post(
        contact_path("ru"),
        data={"message": "buy pills", "reply_to": "a@b.c", "website": "http://spam"},
    )
    assert resp.status_code == 200
    assert copy_for("ru").ok_title in resp.text
    assert rec.sent == []


def test_an_oversized_body_is_refused_without_parsing() -> None:
    client, rec = _client()
    resp = client.post(
        contact_path("ru"),
        content=b"x" * 100,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-length": str(128 * 1024),
        },
    )
    assert resp.status_code == 413
    assert rec.sent == []


def test_a_body_that_declares_no_length_is_refused_too() -> None:
    """#1633: the ceiling has to hold for a chunked request as well.

    Without ``Content-Length`` the header gate used to answer False
    and hand the stream to ``request.form()``, whose urlencoded parser
    accumulates it into a ``bytearray`` with no ceiling of its own —
    an anonymous caller choosing how much RSS one process spends. The
    body below is tiny and perfectly valid; what earns the 413 is the
    missing declaration, and a browser never omits it.
    """
    client, rec = _client()

    def stream() -> Iterator[bytes]:
        yield urlencode({"message": "m" * 20, "reply_to": "a@b.c"}).encode()

    resp = client.post(
        contact_path("ru"),
        content=stream(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert "content-length" not in {k.lower() for k in resp.request.headers}
    assert resp.status_code == 413
    assert rec.sent == []


def test_a_throttled_submission_is_refused_with_429() -> None:
    client, rec = _client()
    for _ in range(3):
        client.post(contact_path("ru"), data={"message": "m", "reply_to": "a@b.c"})
    resp = client.post(contact_path("ru"), data={"message": "m", "reply_to": "a@b.c"})
    assert resp.status_code == 429
    assert copy_for("ru").err_throttled in resp.text
    assert len(rec.sent) == 3


def test_a_failed_delivery_is_reported_honestly() -> None:
    """No success page for a message nobody will read."""
    client, _ = _client(_Recorder(fail=True))
    resp = client.post(contact_path("ru"), data={"message": "m", "reply_to": "a@b.c"})
    assert resp.status_code == 502
    assert copy_for("ru").err_undeliverable in resp.text
    assert copy_for("ru").ok_title not in resp.text


def test_a_failed_delivery_keeps_the_form_and_the_text() -> None:
    client, _ = _client(_Recorder(fail=True))
    resp = client.post(contact_path("ru"), data={"message": "важное", "reply_to": "a@b.c"})
    assert "<form" in resp.text
    assert "важное" in resp.text


# --- the rest of the site knows about it ----------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_documents_name_the_form_when_it_is_mounted(lang: str) -> None:
    """A policy has to name a channel that works without the bot.

    And it has to be the absolute URL — that line gets copied out of the
    page and pasted into a bank's onboarding form.
    """
    from telegram_invite_bot.cms.legal.documents import PRIVACY
    from telegram_invite_bot.cms.legal.router import render_document

    html = render_document(_ctx(), PRIVACY, lang)
    assert f"https://tgbot.delabs.space{contact_path(lang)}" in html


@pytest.mark.parametrize("lang", LANGS)
def test_the_documents_stay_silent_when_it_is_not(lang: str) -> None:
    """A promised page that answers 404 is worse than no promise."""
    from telegram_invite_bot.cms.legal.documents import PRIVACY
    from telegram_invite_bot.cms.legal.router import render_document

    html = render_document(_ctx(contact_enabled=False), PRIVACY, lang)
    assert contact_path(lang) not in html


@pytest.mark.parametrize("lang", LANGS)
def test_the_front_page_links_to_it(lang: str) -> None:
    from telegram_invite_bot.cms.home import render_home

    html = render_home(_ctx(), lang)
    assert f"https://tgbot.delabs.space{contact_path(lang)}" in html


def test_the_client_key_comes_from_the_cloudflare_header() -> None:
    """Every real visitor arrives through the edge.

    Without this the socket address is Cloudflare's and the whole
    internet shares one bucket — the site would rate-limit itself.
    """
    client, rec = _client()
    for i in range(6):
        resp = client.post(
            contact_path("ru"),
            data={"message": f"m{i}", "reply_to": "a@b.c"},
            headers={"cf-connecting-ip": f"10.0.0.{i}"},
        )
        assert resp.status_code == 200
    assert len(rec.sent) == 6
