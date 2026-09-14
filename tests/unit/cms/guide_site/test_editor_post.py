"""``POST /commands/edit`` under load and under attack (#189, #211, #213).

The editor is the one write endpoint on the public site. It is
unauthenticated up to a single shared secret, it lives on the domain
handed to an acquiring bank, and it runs in the same process — and on
the same event loop — as every Telegram update the bot answers. Three
properties follow, and this module is where each is pinned:

* **A guess costs something** (#189). Wrong secrets are rationed per
  client and site-wide; correct ones are free, because an operator
  saving five revisions in a row is not an attack.
* **A body has a ceiling** (#211). Escaping a submission back into the
  page and hashing it for the CSP is work an anonymous caller was able
  to buy by the megabyte. Two ceilings now bound it — one before the
  parse, one after, the second covering chunked requests that declare
  no length.
* **Nothing here is cached** (#213). Every response carries the
  operator's draft or the outcome of a secret check.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_invite_bot.cms.guide_site import GuideSiteContext, build_router
from telegram_invite_bot.cms.guide_site import router as router_module
from telegram_invite_bot.cms.guide_site.editor import InMemoryEditorBridge
from telegram_invite_bot.cms.guide_site.router import (
    _MAX_EDIT_BODY_BYTES,
    _MAX_EDIT_FIELD_CHARS,
)
from telegram_invite_bot.cms.guide_site.throttle import EditorThrottle

#: Taken from the logger itself rather than spelled out, so moving the
#: module cannot quietly detach the assertion from what it watches.
_ROUTER_LOGGER = router_module.log.name

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import httpx

_SECRET = "s3cret"
_EDIT = "/commands/edit"

#: A marker only the echo path can put on the page. Every refusal that
#: must *not* reflect the submission is asserted against this rather
#: than against a status code alone — a 413 that still echoed would
#: pass a status assertion while doing the exact thing #211 is about.
_DRAFT = "ОПОЗНАВАТЕЛЬНАЯЛЕКСЕМА"


class _Clock:
    """A hand-cranked monotonic clock. Refill is arithmetic here."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _ctx(tmp_path: Path) -> GuideSiteContext:
    ru = tmp_path / "telegraph_guide_ru.md"
    en = tmp_path / "telegraph_guide_en.md"
    ru.write_text("# Гайд\n\n## Заголовок\n", encoding="utf-8")
    en.write_text("# Guide\n\n## Heading\n", encoding="utf-8")
    return GuideSiteContext(
        guide_file_ru=ru,
        guide_file_en=en,
        site_title="MyBot",
        version="1.2.3",
        bot_username="my_test_bot",
        url_prefix="",
        editor_bridge=InMemoryEditorBridge(),
    )


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def client(tmp_path: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("GUIDES_EDIT_SECRET", _SECRET)
    app = FastAPI()
    app.include_router(build_router(_ctx(tmp_path), now=clock, throttle=EditorThrottle()))
    return TestClient(app)


def _post(
    client: TestClient,
    *,
    secret: str = _SECRET,
    ru: str = _DRAFT,
    en: str = "en draft",
    ip: str = "203.0.113.7",
) -> httpx.Response:
    return client.post(
        _EDIT,
        data={"ru_text": ru, "en_text": en, "secret": secret},
        headers={"cf-connecting-ip": ip},
    )


def test_a_correct_secret_saves_and_echoes_the_draft(client: TestClient) -> None:
    """The happy path, asserted first so the failures below mean something."""
    response = _post(client)
    assert response.status_code == 200
    assert "Сохранено" in response.text
    assert _DRAFT in response.text


def test_a_wrong_secret_is_refused_but_keeps_the_draft(client: TestClient) -> None:
    """403 still reflects the text, and that is deliberate.

    A mistyped secret is the one failure an operator recovers from by
    resubmitting; throwing away 40 KB of edits over a typo would be a
    worse bug than the amplification it would avoid. It is safe here
    only because the two ceilings bound what can be echoed — hence the
    tests below.
    """
    response = _post(client, secret="wrong")
    assert response.status_code == 403
    assert "Неверный секрет" in response.text
    assert _DRAFT in response.text


def test_a_declared_body_over_the_ceiling_is_refused_unparsed(client: TestClient) -> None:
    """413 before ``request.form()`` runs — the point of #211.

    Asserted through the echo marker: the submission carried a correct
    secret and a recognisable draft, so a handler that had parsed the
    body would have saved it and shown it back. Neither happened.
    """
    response = _post(client, ru=_DRAFT + "я" * _MAX_EDIT_BODY_BYTES)
    assert response.status_code == 413
    assert _DRAFT not in response.text
    assert "Слишком большой запрос" in response.text


def test_a_body_that_declares_no_length_is_refused_unparsed(
    client: TestClient,
) -> None:
    """#1633: no declared length is itself the refusal.

    ``Content-Length`` is absent on a chunked upload, and the header
    gate used to answer False for it and hand the stream to
    ``request.form()``. The per-field ceiling below is a backstop, not
    a bound on memory: it can only speak once the whole stream has
    already been buffered, and Starlette's urlencoded parser buffers
    it without a ceiling of its own. The body here is small and
    perfectly valid — what earns the 413 is the missing declaration,
    and it has to be tested through a genuinely chunked request or it
    is testing nothing.
    """
    body = urlencode({"ru_text": _DRAFT, "en_text": "", "secret": _SECRET}).encode()

    def stream() -> Iterator[bytes]:
        yield body

    response = client.post(
        _EDIT,
        content=stream(),
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "cf-connecting-ip": "203.0.113.8",
        },
    )
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 413
    assert _DRAFT not in response.text
    assert "Слишком большой запрос" in response.text


def test_an_oversized_field_inside_the_body_ceiling_is_refused_after_parsing(
    client: TestClient,
) -> None:
    """The per-field backstop, exercised where it is the only gate.

    100_001 characters is far past ``_MAX_EDIT_FIELD_CHARS`` and far
    short of ``_MAX_EDIT_BODY_BYTES``, so the body gate passes it and
    this ceiling is what refuses it. Asserted through the echo marker:
    the draft was parsed and then thrown away, not saved.
    """
    response = _post(client, ru=_DRAFT + "x" * (_MAX_EDIT_FIELD_CHARS + 1))
    assert response.status_code == 413
    assert _DRAFT not in response.text
    assert "Слишком большой текст" in response.text


def test_wrong_secrets_run_out_after_five_tries(client: TestClient) -> None:
    """#189: the guess budget is finite, and running out says so."""
    for attempt in range(5):
        assert _post(client, secret="wrong").status_code == 403, f"attempt {attempt}"
    response = _post(client, secret="wrong")
    assert response.status_code == 429
    assert "Слишком много попыток" in response.text


def test_a_throttled_caller_is_told_nothing_about_their_secret(client: TestClient) -> None:
    """A refusal must not double as an oracle.

    Once the budget is gone, a *correct* secret gets the same 429 as a
    wrong one — checked here from the other direction: the response to
    the correct one is byte-identical to the response to a wrong one,
    so nothing in it distinguishes them. It also does not echo, which
    is what keeps the throttled path cheap.
    """
    for _ in range(5):
        _post(client, secret="wrong")
    refused_wrong = _post(client, secret="wrong")
    refused_right = _post(client, secret=_SECRET)
    assert refused_wrong.status_code == refused_right.status_code == 429
    assert refused_wrong.text == refused_right.text
    assert _DRAFT not in refused_right.text


def test_a_correct_secret_is_never_charged(client: TestClient) -> None:
    """An operator saving repeatedly must not throttle themselves.

    Twenty saves is four times the guess budget: if a success spent a
    token, this would have stopped at the sixth.
    """
    for attempt in range(20):
        assert _post(client).status_code == 200, f"save {attempt}"


def test_the_guess_budget_refills_with_time(client: TestClient, clock: _Clock) -> None:
    """One token a minute — a locked-out operator is not locked out for good."""
    for _ in range(5):
        _post(client, secret="wrong")
    assert _post(client, secret="wrong").status_code == 429

    clock.advance(59.0)
    assert _post(client, secret="wrong").status_code == 429
    clock.advance(2.0)
    assert _post(client, secret="wrong").status_code == 403


def test_one_clients_flood_does_not_lock_another_out(client: TestClient) -> None:
    """The per-client bucket is per client, or a shared NAT is a weapon."""
    for _ in range(6):
        _post(client, secret="wrong", ip="198.51.100.1")
    assert _post(client, secret="wrong", ip="198.51.100.1").status_code == 429
    assert _post(client, secret="wrong", ip="198.51.100.2").status_code == 403


def test_a_distributed_guesser_still_runs_out(client: TestClient) -> None:
    """The global bucket, which is the one that holds when the key is forged.

    Sixty wrong guesses spread one per address defeats the per-client
    limit entirely; the site-wide budget is what stops the sixty-first,
    and it stops a brand-new address that has spent nothing of its own.
    """
    for i in range(60):
        assert _post(client, secret="wrong", ip=f"192.0.2.{i}").status_code == 403
    assert _post(client, secret="wrong", ip="192.0.2.200").status_code == 429


def test_a_drained_site_budget_locks_the_operator_out_loudly(
    client: TestClient, clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """The documented cost of the global bucket, pinned so it stays deliberate.

    While the site-wide budget is empty the *correct* secret is refused
    too. That is not an oversight to be fixed later: a refusal that
    still answered "wrong" would hand back the one bit the limit exists
    to ration, so the site-wide budget cannot make an exception for a
    right answer without ceasing to be a limit.

    What it does owe the operator is a reason. A client running out of
    its own guesses is routine and logs at INFO; the whole editor being
    shut is not, and has to be findable in the journal — otherwise the
    operator sees an editor that stopped working and nothing else.
    """
    for i in range(60):
        assert _post(client, secret="wrong", ip=f"192.0.2.{i}").status_code == 403

    with caplog.at_level(logging.WARNING, logger=_ROUTER_LOGGER):
        locked_out = _post(client, ip="198.51.100.9")

    assert locked_out.status_code == 429
    assert "site-wide guess budget exhausted" in caplog.text

    # Ten seconds buys back exactly one token, and the operator — whose
    # own bucket is untouched — gets the next attempt.
    clock.advance(10.0)
    assert _post(client, ip="198.51.100.9").status_code == 200


@pytest.mark.parametrize(
    ("secret", "ru", "expected"),
    [
        (_SECRET, _DRAFT, 200),
        ("wrong", _DRAFT, 403),
        (_SECRET, "я" * _MAX_EDIT_BODY_BYTES, 413),
    ],
    # Named, because the third case's parameter is a quarter-million
    # characters long and pytest would otherwise put all of it in the
    # test id — unreadable in a failure line and in the log.
    ids=["saved", "wrong-secret", "oversized"],
)
def test_every_editor_response_forbids_caching(
    client: TestClient, secret: str, ru: str, expected: int
) -> None:
    """#213: ``no-store`` on the success, the refusal and the rejection.

    The bug was reasoning that omitting ``Cache-Control`` keeps a page
    out of caches. It does not — a response with no directives is
    eligible for heuristic freshness in any shared cache, and this
    origin is behind one.
    """
    response = _post(client, secret=secret, ru=ru)
    assert response.status_code == expected
    assert response.headers["cache-control"] == "no-store"


class _RaisingBridge:
    """A bridge whose save fails the way the real one fails.

    ``JsonFileEditorBridge`` writes through the filesystem, so the
    exception an operator actually meets is an ``OSError`` whose text
    is the absolute path of the settings file on the server. That path
    is the payload #1485 is about, so the fake carries a real-looking
    one rather than a neutral message.
    """

    #: Spelled once so the assertions below cannot drift from the raise.
    path = "/srv/app/settings.json"

    def load_overrides(self) -> tuple[str, str]:
        return ("", "")

    def save_overrides(self, ru: str, en: str) -> None:
        raise OSError(f"[Errno 13] Permission denied: '{self.path}'")


def _raising_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("GUIDES_EDIT_SECRET", _SECRET)
    ctx = replace(_ctx(tmp_path), editor_bridge=_RaisingBridge())
    app = FastAPI()
    app.include_router(build_router(ctx, throttle=EditorThrottle()))
    return TestClient(app)


def test_a_failed_save_tells_the_operator_nothing_about_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1485: the 500 page says the save failed and stops there.

    It used to interpolate the exception straight into the form, which
    handed an anonymous caller — the secret is shared, and a correct
    guess is what gets here — the deployment's directory layout. The
    draft is still echoed, because losing the edits on top of a failed
    save would be the second bug.
    """
    client = _raising_client(tmp_path, monkeypatch)
    response = _post(client)

    assert response.status_code == 500
    assert "Ошибка сохранения" in response.text
    assert _DRAFT in response.text
    assert _RaisingBridge.path not in response.text
    assert "Errno" not in response.text
    assert "Permission denied" not in response.text


def test_a_failed_save_reaches_the_journal_with_its_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half of the split: dropping the text is only safe if
    the journal keeps it.

    ``log.exception`` and not ``log.error`` — the assertion is on
    ``exc_info``, because an ``error`` call would satisfy a message
    check and still lose the traceback that names the failing path.
    """
    client = _raising_client(tmp_path, monkeypatch)
    with caplog.at_level(logging.ERROR, logger=_ROUTER_LOGGER):
        assert _post(client).status_code == 500

    records = [r for r in caplog.records if r.name == _ROUTER_LOGGER]
    assert [r.getMessage() for r in records] == ["guide editor: saving overrides failed"]
    assert records[0].exc_info is not None
    assert _RaisingBridge.path in caplog.text


def test_the_editor_is_absent_when_no_secret_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 404 gate runs before every check added above.

    Ordering matters: a deployment that cannot save must not answer a
    probe with a throttle notice or a size complaint, both of which
    confirm the endpoint exists.
    """
    monkeypatch.delenv("GUIDES_EDIT_SECRET", raising=False)
    app = FastAPI()
    app.include_router(build_router(_ctx(tmp_path)))
    client = TestClient(app)
    for ru in (_DRAFT, "я" * _MAX_EDIT_BODY_BYTES):
        assert _post(client, ru=ru).status_code == 404
