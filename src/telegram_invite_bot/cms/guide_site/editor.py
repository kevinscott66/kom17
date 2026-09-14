"""Guide editor (POST ``/commands/edit``) — native port (T-026).

Persists the two RU/EN markdown override keys
(``guides_markdown_override_ru`` / ``..._en``) directly to the legacy
JSON settings file via :class:`JsonFileEditorBridge`. No dependency on
the legacy ``bot.py`` module — the file format is the only contract
shared across the strangler boundary.

Why a JSON file rather than the new pydantic-settings ``Settings``
object: pydantic-settings is read-only env-derived state. The guide
overrides are mutable runtime state that an operator edits from the
browser at any time; persisting to env / restarting the process is not
the workflow. Legacy stored these in ``database/settings.json`` (or
``$SETTINGS_FILE``). Nothing else reads that file any more — T-011 left
no legacy process to read it — but the file itself is still on the prod
disk with an operator's overrides already in it, so the same path and
the same key names are what makes those overrides keep rendering across
the cutover instead of silently reverting to the shipped text.

The bridge:

* :class:`EditorBridge` — the Protocol the router consumes. Two
  methods, no transaction semantics — matches the legacy form-POST
  flow.
* :class:`JsonFileEditorBridge` — the default impl. Reads the JSON
  file on each :meth:`load_overrides` (legacy may have rewritten it),
  merges the two override keys on save, writes atomically via tmp+
  rename so a crash mid-write never produces a half-truncated file.
  Returns empty strings on a missing file (legacy parity — the file is
  created on first save).
* :class:`InMemoryEditorBridge` — test/dev double, unchanged.

Security: edits gated on ``GUIDES_EDIT_SECRET`` env var matching the
``secret`` form field via :func:`hmac.compare_digest`. Missing env var
disables the editor, and both halves of it answer **404**, not the 503
this paragraph used to promise: a 503 carrying the editor's own page
would name the environment variable that unlocks it (``router.py``
``:436-437`` and ``:480-481`` say so at both gates). We intentionally
do NOT add CSRF tokens or session cookies —
the editor is single-page, single-user (the developer), behind a
shared-secret, and adding session machinery would change the cutover
surface without improving the real threat model. If the secret leaks,
both the legacy and the new path are equally compromised.
"""

from __future__ import annotations

import hmac
import html as html_lib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Protocol

from telegram_invite_bot.cms.paths import (
    COMMANDS_PATH_EDIT,
    COMMANDS_PATH_EN,
    COMMANDS_PATH_RU,
    HOME_PATH_RU,
)

# Aliases for the shared URL map, so the editor's links cannot drift from
# the routes the guide router actually registers.
_COMMANDS_EDIT_PATH = COMMANDS_PATH_EDIT
_COMMANDS_GUIDE_PATH_RU = COMMANDS_PATH_RU
_COMMANDS_GUIDE_PATH_EN = COMMANDS_PATH_EN

# JSON keys — fixed by the data, not by a second reader. Legacy wrote
# these exact names into the file that is still on disk; renaming them
# would not migrate anything, it would just stop finding the overrides
# already saved under the old names, and the guide would quietly fall
# back to shipped text.
_KEY_RU = "guides_markdown_override_ru"
_KEY_EN = "guides_markdown_override_en"

log = logging.getLogger(__name__)


class EditorBridge(Protocol):
    """Contract between the editor route and persistence.

    Two methods because the legacy flow is "show what's stored, save
    what the operator typed". No transactionality — the legacy code
    didn't have any either, and the persistence layer (a single JSON
    file) is atomic enough for this single-operator use case.
    """

    def load_overrides(self) -> tuple[str, str]:
        """Return ``(ru_markdown, en_markdown)``. Empty strings if
        no override is set — caller falls back to the file on disk.
        Must NOT raise on a missing-key — return empty strings instead.
        """
        ...

    def save_overrides(self, ru: str, en: str) -> None:
        """Persist new override values. May raise on I/O failure; the
        router catches the exception and renders a 500 with the
        message.
        """
        ...


class CorruptSettingsError(ValueError):
    """The settings file exists but does not hold a JSON object.

    Derived from ``ValueError`` so that one ``except`` clause covers it
    together with :class:`json.JSONDecodeError`, which is the same kind
    of problem noticed one step earlier.
    """


class JsonFileEditorBridge:
    """Default :class:`EditorBridge` that reads/writes a JSON file.

    Same file the legacy ``bot.py`` used (``$SETTINGS_FILE`` or
    ``database/settings.json``), same key names. That used to buy
    cross-layer visibility; since T-011 there is no other layer, so the
    file is already private to this bridge and what the shared naming
    buys now is continuity — the overrides an operator saved before the
    cutover are still the ones being served. Migrating to a more focused
    store is therefore a data move, not just a rename.

    Robustness posture:

    * ``load_overrides`` is forgiving — a missing file, malformed JSON,
      or unexpected types all degrade to empty-string overrides rather
      than 500ing the page. The legacy editor had the same fallback
      (``getattr(bot, "settings", {}) or {}``) and rendering an empty
      textarea is far less alarming to an operator than a stack trace.
      ``save_overrides`` is NOT forgiving, and the asymmetry is the
      point (#1423): showing nothing is recoverable, writing nothing
      over a file the legacy process also owns is not.
    * ``save_overrides`` writes via tmp-file + ``os.replace`` so a
      crash mid-write never produces a half-truncated JSON. Other keys
      in the file (legacy state we don't model) are preserved by
      reading-then-merging before write.
    """

    __slots__ = ("_path",)

    def __init__(self, path: Path) -> None:
        self._path = path

    def load_overrides(self) -> tuple[str, str]:
        data = self._read_or_empty()
        ru = str(data.get(_KEY_RU) or "")
        en = str(data.get(_KEY_EN) or "")
        return ru, en

    def save_overrides(self, ru: str, en: str) -> None:
        # The strict read, on purpose (#1423). This file is shared with
        # the legacy ``bot.py``, and the merge below is the only thing
        # that preserves the keys this module does not model. While the
        # read was forgiving, one unreadable byte — corrupt JSON, a
        # transient EIO, a half-written file from the other process —
        # became ``{}``, and the atomic write then replaced the entire
        # settings file with just these two keys. Losing the overrides
        # would have been bad; deleting legacy state on the way is
        # worse, and neither left a word in the log. Raising here is
        # what the :class:`EditorBridge` contract already promises the
        # router, which turns it into a 500 the operator can see.
        data = self._read()
        data[_KEY_RU] = ru.strip()
        data[_KEY_EN] = en.strip()
        self._write(data)

    def _read(self) -> dict[str, object]:
        """Read the JSON file. A missing file is ``{}``; the rest raises.

        Missing is not a failure: the file is created on first save and
        legacy behaved the same way. Everything else — unreadable,
        unparsable, or parsing to something that is not an object — is
        a failure, and only the caller knows what to do about it. The
        display half wants empty fields; the read-modify-write half
        wants to not write at all.
        """
        if not self._path.is_file():
            return {}
        raw = self._path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            msg = f"settings file is not a JSON object: {type(parsed).__name__}"
            raise CorruptSettingsError(msg)
        return parsed

    def _read_or_empty(self) -> dict[str, object]:
        """:meth:`_read` with every failure degraded to ``{}``.

        Legacy parity, and only for the display half: the original
        ``getattr(bot, "settings", {}) or {}`` lookup never raised, and
        an empty textarea is far less alarming to an operator than a
        stack trace. Logged rather than swallowed, though — empty
        fields where the operator expects their own text is the
        symptom, and the journal has to be where the cause is.
        """
        try:
            return self._read()
        except (OSError, ValueError):
            log.warning(
                "guide editor: settings file unreadable, showing empty overrides: path=%s",
                self._path,
                exc_info=True,
            )
            return {}

    def _write(self, data: dict[str, object]) -> None:
        """Atomically write ``data`` as JSON to ``self._path``.

        ``os.replace`` is atomic on POSIX and Windows for same-volume
        renames — we keep the temp file in the destination directory
        to guarantee that. The parent directory is created on demand
        so a fresh deployment (no ``database/`` yet) doesn't fail the
        first save.

        #1940: ``delete=False`` means NOTHING removes the temp file if
        the write or the rename raises — a full disk, a serialisation
        error, an interrupted save. Every failed attempt used to leave
        one ``<name>.<random>.tmp`` next to the real file, forever, in
        a directory nobody lists. The ``finally`` below turns that into
        the intended lifetime: the temp path exists only between its
        creation and the rename that consumes it.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # ``delete=False`` because we hand off the path to ``replace``;
        # the temp file's lifetime ends with the rename.
        tmp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self._path.parent),
                prefix=self._path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_name = tmp.name
                json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self._path)
            # Consumed by the rename — there is no longer a path to clean.
            tmp_name = ""
        finally:
            if tmp_name:
                Path(tmp_name).unlink(missing_ok=True)


class InMemoryEditorBridge:
    """Test/dev double — stores overrides in process memory.

    Not used in production; the public guide router instantiates the
    JSON-file bridge by default. Useful in test fixtures and for ad-hoc
    local exploration without touching the on-disk settings file.
    """

    def __init__(self, ru: str = "", en: str = "") -> None:
        self._ru = ru
        self._en = en

    def load_overrides(self) -> tuple[str, str]:
        return self._ru, self._en

    def save_overrides(self, ru: str, en: str) -> None:
        self._ru = ru.strip()
        self._en = en.strip()


def render_editor_html(ru: str, en: str, msg: str = "") -> str:
    """Render the editor HTML form.

    Lifted verbatim from legacy ``guide_site._editor_html`` — same DOM,
    same styles, same form layout — so an operator who used the legacy
    editor sees no UI difference after the flag flips. The only change
    is that the POST target is now the FastAPI handler instead of the
    Flask handler.
    """
    m = html_lib.escape(msg) if msg else ""
    notice = f'<p class="ok">{m}</p>' if msg else ""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Редактор гайда</title>
<style>
body {{ font-family: system-ui, sans-serif; background:#0c0e12; color:#e8eaef; padding:1.5rem; max-width:900px; margin:0 auto; }}
h1 {{ font-size:1.1rem; }}
label {{ display:block; margin-top:1rem; font-size:0.85rem; color:#8b95a8; }}
textarea {{ width:100%; min-height:220px; background:#141820; color:#e8eaef; border:1px solid rgba(255,255,255,.1); border-radius:8px; padding:0.75rem; font-size:13px; }}
input[type=password] {{ width:100%; max-width:360px; padding:0.5rem; border-radius:8px; border:1px solid rgba(255,255,255,.12); background:#141820; color:#e8eaef; }}
button {{ margin-top:1rem; padding:0.6rem 1.2rem; border-radius:10px; border:none; background:#5b8def; color:#fff; font-weight:600; cursor:pointer; }}
.ok {{ color:#3dd68c; }}
.err {{ color:#f66; }}
.hint {{ font-size:0.8rem; color:#8b95a8; margin-top:0.5rem; }}
/* In the stylesheet rather than on each anchor: the page's
   Content-Security-Policy allows inline blocks by hash, and a hash
   cannot cover a per-element style attribute — the browser would drop
   those and the links would come out unstyled. */
.hint a {{ color:#5b8def; }}
</style>
</head>
<body>
<h1>Редактор гайдов (RU / EN)</h1>
<p class="hint">Пустое поле = брать текст из файла <code>telegraph_guide_*.md</code> на сервере. После сохранения откройте /commands и /commands/en.</p>
{notice}
<form method="post" action="{_COMMANDS_EDIT_PATH}">
<label>Russian (Markdown)</label>
<textarea name="ru_text">{html_lib.escape(ru)}</textarea>
<label>English (Markdown)</label>
<textarea name="en_text">{html_lib.escape(en)}</textarea>
<label>Секрет (GUIDES_EDIT_SECRET из .env)</label>
<input type="password" name="secret" autocomplete="off" placeholder="Секрет"/>
<button type="submit">Сохранить</button>
</form>
<p class="hint"><a href="{HOME_PATH_RU}">← На главную</a> · <a href="{_COMMANDS_GUIDE_PATH_RU}">Просмотр RU</a> · <a href="{_COMMANDS_GUIDE_PATH_EN}">EN</a></p>
</body>
</html>"""


def _read_edit_secret() -> str:
    """Centralised env lookup — both GET (to surface the not-configured
    warning) and POST (to authenticate) need this. Trimmed because
    operators copy-paste with trailing newlines."""
    return (os.getenv("GUIDES_EDIT_SECRET") or "").strip()


def secret_configured() -> bool:
    return bool(_read_edit_secret())


def verify_secret(submitted: str) -> bool:
    """Constant-time comparison against the configured secret.

    Returns False if either side is empty — an unset env var must NOT
    grant access just because the operator also leaves the field blank.

    Both sides are screened for non-ASCII first, because
    ``hmac.compare_digest`` on two ``str`` RAISES ``TypeError`` when
    either one is not ASCII rather than returning False. This is the
    same defect ``webhook/security.py:44-74`` fixes on the other public
    route, and it bites here in two distinct ways:

    * Attacker side: nothing registers an ``Exception`` handler on this
      app (the only one is ``cms/notfound.py:224-230``, and it is bound
      to ``StarletteHTTPException``), so the ``TypeError`` escapes as a
      500 with a full uvicorn traceback. Worse, the caller's budget is
      spent by ``limiter.note_failure``, which ``guide_edit_post`` in
      ``cms/guide_site/router.py`` runs on the line AFTER the one that
      calls this function — so a raise makes the failed attempt free,
      and one
      Cyrillic byte becomes unmetered journald amplification on a small
      host whose nginx vhost carries no rate limit.
    * Operator side: unlike ``WEBHOOK_SECRET_TOKEN`` this value is read
      straight from ``os.getenv`` with no ``Settings`` validator, so a
      Russian-speaking operator who picks a Cyrillic passphrase locks
      themselves out permanently — every submission 500s, including the
      correct one, while the form keeps rendering normally.

    Returning False rather than raising is what keeps ``note_failure``
    on the path, so a probe is charged like any other wrong secret.
    """
    expected = _read_edit_secret()
    if not expected or not submitted:
        return False
    if not expected.isascii():
        log.error(
            "GUIDES_EDIT_SECRET contains non-ASCII characters; refusing all edits "
            "(compare_digest would raise TypeError on every submission)"
        )
        return False
    if not submitted.isascii():
        return False
    return hmac.compare_digest(expected, submitted)
