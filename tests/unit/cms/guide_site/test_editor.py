"""Editor (GET/POST /commands/edit) — auth, bridge, fallback states.

Pins legacy parity, with one deliberate departure — an editor that
cannot save is not merely disabled, it is *absent* (404 on both verbs),
because this origin is also the address the acquiring bank was given:

* Missing bridge → 404, no editor markup in the body.
* Missing GUIDES_EDIT_SECRET env → 404, and the variable is not named.
* Wrong secret → 403 + "Неверный секрет".
* Right secret + bridge → 200 + bridge mutated with new values.
* GET falls back to file contents when override is empty.
* GET shows configured-override values verbatim (no file fallback) when
  bridge already has content.
* Bridge exception during save → 500 with the exception message
  surfaced to the operator (matches legacy ``Ошибка сохранения: {e}``).
* Constant-time secret comparison — empty submitted secret is rejected
  even when env is also empty (no "blank-blank" auth-bypass).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from telegram_invite_bot.cms.guide_site import GuideSiteContext, build_router
from telegram_invite_bot.cms.guide_site import editor as editor_mod
from telegram_invite_bot.cms.guide_site.editor import (
    CorruptSettingsError,
    EditorBridge,
    InMemoryEditorBridge,
    JsonFileEditorBridge,
    verify_secret,
)

#: The bridge module's logger, for :func:`caplog.at_level` — a
#: settings file that will not parse is reported to the operator
#: through the log, because the page it degrades to looks normal.
_EDITOR_LOGGER = editor_mod.log.name


def _ctx(
    tmp_path: Path,
    *,
    # The Protocol, not one implementation: these tests exercise both
    # bridges, and narrowing this to the in-memory one would make the
    # file-backed cases type-errors while still passing at runtime.
    bridge: EditorBridge | None,
    with_files: bool = True,
) -> GuideSiteContext:
    ru = tmp_path / "telegraph_guide_ru.md"
    en = tmp_path / "telegraph_guide_en.md"
    if with_files:
        ru.write_text("# RU file\n", encoding="utf-8")
        en.write_text("# EN file\n", encoding="utf-8")
    return GuideSiteContext(
        guide_file_ru=ru,
        guide_file_en=en,
        site_title="Bot",
        version="1.0",
        bot_username="b",
        url_prefix="",
        editor_bridge=bridge,
    )


def _client(ctx: GuideSiteContext) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(ctx))
    return TestClient(app)


# --- GET ------------------------------------------------------------


def test_get_without_a_bridge_is_not_discoverable(tmp_path: Path) -> None:
    """An editor that cannot save must not be browsable.

    This origin also serves the public offer and the privacy policy, so
    an admin form on it is read as an unsecured back office by anyone
    reviewing the domain — and no visitor can tell that the save button
    is inert.
    """
    resp = _client(_ctx(tmp_path, bridge=None)).get("/commands/edit")
    assert resp.status_code == 404
    assert "textarea" not in resp.text


def test_get_without_a_secret_is_not_discoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GUIDES_EDIT_SECRET", raising=False)
    resp = _client(_ctx(tmp_path, bridge=InMemoryEditorBridge())).get("/commands/edit")
    assert resp.status_code == 404
    # The 404 must not disclose why — naming the env var tells a prober
    # that the editor exists and is one setting away from being live.
    assert "GUIDES_EDIT_SECRET" not in resp.text


def test_get_empty_override_falls_back_to_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "s3cr3t")
    bridge = InMemoryEditorBridge(ru="", en="")
    client = _client(_ctx(tmp_path, bridge=bridge))
    resp = client.get("/commands/edit")
    # File content visible in the textarea — operator can edit it.
    assert "# RU file" in resp.text
    assert "# EN file" in resp.text


def test_get_uses_stored_override_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "s3cr3t")
    bridge = InMemoryEditorBridge(ru="# stored RU", en="# stored EN")
    client = _client(_ctx(tmp_path, bridge=bridge))
    resp = client.get("/commands/edit")
    assert "# stored RU" in resp.text
    assert "# stored EN" in resp.text
    # File contents are NOT used because override is non-empty.
    assert "# RU file" not in resp.text


def test_editor_responses_carry_noindex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#1480 — the admin form must never reach a search index.

    The GET is open by design: only the POST checks the secret. So the
    header, not the gate, is what keeps the form out of the index — on
    every editor response, because the wrong-secret 403 renders the same
    admin markup as the 200. The public guide page is left crawlable, or
    the fix would have hidden the thing the site exists to show.
    """
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "s3cr3t")
    client = _client(_ctx(tmp_path, bridge=InMemoryEditorBridge()))

    for resp in (
        client.get("/commands/edit"),
        client.post(
            "/commands/edit",
            data={"ru_text": "x", "en_text": "y", "secret": "wrong"},
        ),
    ):
        assert resp.headers["x-robots-tag"] == "noindex, nofollow"
        assert resp.headers["cache-control"] == "no-store"

    assert "x-robots-tag" not in client.get("/commands").headers


# --- POST ------------------------------------------------------------


def test_post_without_a_bridge_is_not_discoverable(tmp_path: Path) -> None:
    """The POST answers exactly like the GET on an unusable editor.

    If the form 404s but the submit endpoint replies with the editor's
    own HTML, probing the path still reveals the admin surface.
    """
    client = _client(_ctx(tmp_path, bridge=None))
    resp = client.post(
        "/commands/edit",
        data={"ru_text": "x", "en_text": "y", "secret": "anything"},
    )
    assert resp.status_code == 404
    assert "textarea" not in resp.text


def test_post_without_a_secret_is_not_discoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GUIDES_EDIT_SECRET", raising=False)
    bridge = InMemoryEditorBridge()
    client = _client(_ctx(tmp_path, bridge=bridge))
    resp = client.post(
        "/commands/edit",
        data={"ru_text": "x", "en_text": "y", "secret": ""},
    )
    assert resp.status_code == 404
    assert "GUIDES_EDIT_SECRET" not in resp.text
    # Bridge NOT touched — a post to a disabled editor must not
    # silently change persistent state.
    assert bridge.load_overrides() == ("", "")


def test_post_wrong_secret_403_bridge_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "correct-secret")
    bridge = InMemoryEditorBridge()
    client = _client(_ctx(tmp_path, bridge=bridge))
    resp = client.post(
        "/commands/edit",
        data={"ru_text": "new", "en_text": "new", "secret": "wrong"},
    )
    assert resp.status_code == 403
    assert "Неверный секрет" in resp.text
    assert bridge.load_overrides() == ("", "")


def test_post_correct_secret_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "s3cr3t")
    bridge = InMemoryEditorBridge()
    client = _client(_ctx(tmp_path, bridge=bridge))
    resp = client.post(
        "/commands/edit",
        data={"ru_text": "  # new RU  ", "en_text": "# new EN", "secret": "s3cr3t"},
    )
    assert resp.status_code == 200
    assert "Сохранено" in resp.text
    # strip() per legacy behaviour.
    assert bridge.load_overrides() == ("# new RU", "# new EN")


def test_post_bridge_raises_renders_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing bridge is a 500, and the reason stays on the server.

    This assertion was inverted by #1485. It used to require the
    exception text on the page, which is how the real bridge's
    ``OSError`` — whose text is the absolute path of the settings file
    — reached an anonymous caller. The page now says only that the
    save failed; ``tests/unit/cms/guide_site/test_editor_post.py``
    holds the other half, that the text still reaches the journal.
    """
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "s")

    class Boom(InMemoryEditorBridge):
        def save_overrides(self, ru: str, en: str) -> None:
            raise RuntimeError("disk full")

    client = _client(_ctx(tmp_path, bridge=Boom()))
    resp = client.post(
        "/commands/edit",
        data={"ru_text": "x", "en_text": "y", "secret": "s"},
    )
    assert resp.status_code == 500
    assert "Ошибка сохранения" in resp.text
    assert "disk full" not in resp.text


# --- secret helper ---------------------------------------------------


def test_verify_secret_blank_blank_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cry-wolf-but-inverted pin: an unset env var must NOT grant
    access just because the form field is also empty."""
    monkeypatch.delenv("GUIDES_EDIT_SECRET", raising=False)
    assert verify_secret("") is False


def test_verify_secret_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "abc")
    assert verify_secret("abc") is True


def test_verify_secret_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "abc")
    assert verify_secret("abd") is False


# --- JsonFileEditorBridge -------------------------------------------
#
# These pin the T-026 port: the native bridge speaks the same JSON
# file format the legacy ``bot.py`` settings loader does, so during
# the strangler window both layers see the same override values.


def test_json_bridge_missing_file_returns_empty(tmp_path: Path) -> None:
    bridge = JsonFileEditorBridge(tmp_path / "settings.json")
    assert bridge.load_overrides() == ("", "")


def test_json_bridge_malformed_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    bridge = JsonFileEditorBridge(path)
    # Legacy parity: a corrupt file degrades to empty rather than 500.
    assert bridge.load_overrides() == ("", "")


def test_json_bridge_non_dict_file_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('["not", "a", "dict"]', encoding="utf-8")
    bridge = JsonFileEditorBridge(path)
    assert bridge.load_overrides() == ("", "")


def test_json_bridge_save_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    bridge = JsonFileEditorBridge(path)
    bridge.save_overrides("  # RU md  ", "# EN md")
    # strip() applied per legacy behaviour.
    assert bridge.load_overrides() == ("# RU md", "# EN md")
    # And on disk, with the legacy key names.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["guides_markdown_override_ru"] == "# RU md"
    assert on_disk["guides_markdown_override_en"] == "# EN md"


def test_json_bridge_save_preserves_unknown_keys(tmp_path: Path) -> None:
    """Legacy ``settings.json`` holds dozens of unrelated keys —
    saving an override MUST NOT clobber them. Pin the read-merge-write
    cycle by seeding a foreign key and asserting it survives a save."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"foreign_key": "keep me", "guides_markdown_override_ru": "old"}),
        encoding="utf-8",
    )
    bridge = JsonFileEditorBridge(path)
    bridge.save_overrides("new RU", "new EN")
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["foreign_key"] == "keep me"
    assert on_disk["guides_markdown_override_ru"] == "new RU"
    assert on_disk["guides_markdown_override_en"] == "new EN"


def test_json_bridge_save_refuses_to_overwrite_a_file_it_could_not_read(
    tmp_path: Path,
) -> None:
    """#1423 — an unreadable settings file must abort the save.

    The read half is forgiving by design and returns ``{}`` for a
    corrupt file. Feeding that ``{}`` into the save half was a
    data-loss bug rather than a lenience: the save merges into what it
    read and writes the result atomically, so ``{}`` plus two keys
    replaced the whole file — including the keys the legacy ``bot.py``
    owns and this module does not model. One corrupt byte deleted
    settings nothing here had ever written, silently.

    Pinned by the bytes on disk, not by the exception: the promise is
    that the file is untouched, and an implementation that raised
    *after* writing would satisfy a ``pytest.raises`` alone.
    """
    path = tmp_path / "settings.json"
    corrupt = '{"foreign_key": "keep me", NOT JSON'
    path.write_text(corrupt, encoding="utf-8")
    bridge = JsonFileEditorBridge(path)

    with pytest.raises(ValueError, match="Expecting|Invalid|delimiter|value"):
        bridge.save_overrides("new RU", "new EN")

    assert path.read_text(encoding="utf-8") == corrupt


def test_json_bridge_save_refuses_a_file_that_is_not_an_object(tmp_path: Path) -> None:
    """The same guard one step later: valid JSON of the wrong shape.

    A list parses fine, so the merge would not raise — it would just
    have nothing to merge into, and the write would turn an array into
    an object. Separated from the parse failure above because it is a
    different mistake by whoever produced the file, and only this one
    reaches :class:`CorruptSettingsError`.
    """
    path = tmp_path / "settings.json"
    path.write_text('["not", "a", "dict"]', encoding="utf-8")
    bridge = JsonFileEditorBridge(path)

    with pytest.raises(CorruptSettingsError):
        bridge.save_overrides("new RU", "new EN")

    assert path.read_text(encoding="utf-8") == '["not", "a", "dict"]'


def test_json_bridge_load_still_degrades_and_says_so_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The display half keeps the old lenience, and gains a log line.

    The asymmetry with the two tests above is the whole fix: showing
    an operator an empty textarea is recoverable, writing an empty
    file over the legacy process's state is not. The log line is
    asserted because empty fields look exactly like "no override is
    set" — without it the operator has no way to tell the two apart.
    """
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    bridge = JsonFileEditorBridge(path)

    with caplog.at_level(logging.WARNING, logger=_EDITOR_LOGGER):
        assert bridge.load_overrides() == ("", "")

    assert any("settings file unreadable" in record.message for record in caplog.records)


def test_json_bridge_creates_parent_directory(tmp_path: Path) -> None:
    """A fresh deployment may not have ``database/`` yet — the bridge
    must create the parent on the first save rather than 500."""
    path = tmp_path / "fresh" / "deep" / "settings.json"
    bridge = JsonFileEditorBridge(path)
    bridge.save_overrides("x", "y")
    assert path.is_file()
    assert bridge.load_overrides() == ("x", "y")


def test_json_bridge_end_to_end_via_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E: the real native bridge wired into the FastAPI router
    persists an edit to disk, and a follow-up GET shows the saved
    value back to the operator. Equivalent of the legacy
    ``LegacyBotEditorBridge`` parity check, against the JSON file."""
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "edit-pw")
    settings_path = tmp_path / "settings.json"
    bridge = JsonFileEditorBridge(settings_path)
    ctx = _ctx(tmp_path, bridge=bridge, with_files=False)
    # Note: with_files=False so GET fallback to disk is empty and we
    # can assert the textarea shows the persisted override verbatim.
    client = _client(ctx)

    # Happy-path POST.
    resp = client.post(
        "/commands/edit",
        data={"ru_text": "# persisted RU", "en_text": "# persisted EN", "secret": "edit-pw"},
    )
    assert resp.status_code == 200
    assert "Сохранено" in resp.text

    # File written with the legacy key names.
    on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert on_disk["guides_markdown_override_ru"] == "# persisted RU"
    assert on_disk["guides_markdown_override_en"] == "# persisted EN"

    # A second GET (fresh bridge instance, same file) sees the saved values —
    # pins that load_overrides reads from disk, not from a cache.
    fresh_bridge = JsonFileEditorBridge(settings_path)
    ctx2 = _ctx(tmp_path, bridge=fresh_bridge, with_files=False)
    resp = _client(ctx2).get("/commands/edit")
    assert resp.status_code == 200
    assert "# persisted RU" in resp.text
    assert "# persisted EN" in resp.text


def test_router_post_wrong_content_type_no_auth_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JSON body posted to a form endpoint must NOT auth-bypass.

    The legacy Flask endpoint silently treated missing form fields as
    empty strings; the new endpoint uses ``Form(default="")`` for
    parity. Pin that even with a totally wrong-shape body
    (application/json instead of form-urlencoded), the secret gate
    still rejects with 403 and the bridge stays untouched — i.e. the
    "empty secret + secret configured" path is the 403 branch, not a
    silent save."""
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "correct")
    bridge = InMemoryEditorBridge()
    client = _client(_ctx(tmp_path, bridge=bridge))
    # JSON payload — Form() params fall back to their defaults ("").
    resp = client.post(
        "/commands/edit",
        json={"ru_text": "x", "en_text": "y", "secret": "anything"},
    )
    # verify_secret("") → False, so 403.
    assert resp.status_code == 403
    assert bridge.load_overrides() == ("", "")


def test_verify_secret_non_ascii_submission_is_false_not_typeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Cyrillic guess must be a plain rejection, not a 500.

    ``hmac.compare_digest`` on two ``str`` raises ``TypeError`` when
    either side is non-ASCII. Nothing registers an ``Exception``
    handler on this app, so an unguarded raise escaped ``/commands/edit``
    as a 500 with a full uvicorn traceback — and, because
    ``router.py`` charges the attempt with ``limiter.note_failure``
    on the line AFTER ``verify_secret``, the raise also made the failed
    attempt FREE. One byte, unmetered log amplification.
    """
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "correct-secret")
    assert verify_secret("секрет") is False


def test_verify_secret_non_ascii_configured_denies_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Cyrillic passphrase must lock the editor CLOSED, not 500 it.

    This value is read straight from ``os.getenv`` with no ``Settings``
    validator, so nothing stops an operator picking one. Before the
    guard every submission raised — including the operator's own
    correct secret — while the form kept rendering normally.
    """
    monkeypatch.setenv("GUIDES_EDIT_SECRET", "секрет")
    assert verify_secret("секрет") is False
    assert verify_secret("anything") is False
