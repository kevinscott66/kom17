"""``answerCallbackQuery.text`` is capped at 200 characters.

Past the cap Telegram rejects the call outright. There is no truncation
and no error the user can see: the spinner on the button stops and the
tap did nothing. The handler logged a 400 and moved on, so the only
person who knows the button is dead is the person pressing it.

Toasts reuse the same i18n copy as the cards they summarise, and copy
grows in translation — ``h_groupstats_boost_hint`` is 179 characters in
English today. One more sentence and that button goes quiet. So measure
every toast whose text is a literal ``t()`` key against both
catalogues, the way :func:`utils.html.visible_len` measures a card: tags
stripped and entities un-escaped, because that is the text Telegram
counts.

Only literal keys are measured. A toast built from an f-string or a
variable is out of reach of a static scan — but every parameterised
toast in the tree today interpolates a number, an id, a currency code or
a mode name, never free user text, so the base length IS the length.
"""

from __future__ import annotations

import ast
import html
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

_SRC = Path(__file__).resolve().parents[2] / "src/telegram_invite_bot"
_DATA = _SRC / "i18n/data"

#: Bot API limit for ``answerCallbackQuery.text``.
_TOAST_LIMIT = 200

#: Identifiers a ``CallbackQuery`` is bound to in this tree. The scan is
#: only as good as this set, which is why
#: :func:`test_every_answer_receiver_is_accounted_for` refuses to let a
#: new name slip in unclassified.
_CALLBACK_RECEIVERS = frozenset({"callback", "call"})

#: ``.answer()`` receivers that are NOT callback queries: ``Message``
#: (4096 chars) and ``PreCheckoutQuery`` (``ok=``/``error_message=``).
_OTHER_RECEIVERS = frozenset({"message", "target", "event", "pre_checkout"})

_TAG = re.compile(r"<[^>]+>")


def _catalogue(lang: str) -> dict[str, str]:
    raw = yaml.safe_load((_DATA / f"{lang}.yaml").read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in raw.items()}


def _visible(text: str) -> str:
    """What Telegram counts: markup parses into entities and costs nothing."""
    return html.unescape(_TAG.sub("", text))


def _literal_key(node: ast.AST) -> str | None:
    """The i18n key of ``t("x", …)``, seen through ``plain_text(…)``."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "plain_text" and node.args:
            return _literal_key(node.args[0])
        if node.func.id == "t" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
    return None


def _answer_calls() -> list[tuple[str, ast.Call, str]]:
    """``(where, call node, receiver name)`` for every ``x.answer(…)``."""
    out: list[tuple[str, ast.Call, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "answer"):
                continue
            if not isinstance(func.value, ast.Name):
                continue
            out.append((f"{path.relative_to(_SRC)}:{node.lineno}", node, func.value.id))
    return out


def _toast_sites() -> list[tuple[str, str]]:
    """``(where, i18n key)`` for every callback toast with a literal key."""
    sites: list[tuple[str, str]] = []
    for where, node, receiver in _answer_calls():
        if receiver not in _CALLBACK_RECEIVERS:
            continue
        text: ast.AST | None = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "text":
                text = keyword.value
        if text is None:
            continue
        key = _literal_key(text)
        if key is not None:
            sites.append((where, key))
    return sites


def test_no_toast_can_exceed_the_telegram_limit() -> None:
    catalogues = {lang: _catalogue(lang) for lang in ("ru", "en")}
    over: list[str] = []
    measured = 0
    for where, key in _toast_sites():
        for lang, catalogue in catalogues.items():
            value = catalogue.get(key)
            if value is None:
                continue
            measured += 1
            length = len(_visible(value))
            if length > _TOAST_LIMIT:
                over.append(f"{where}  {key} [{lang}] = {length} > {_TOAST_LIMIT}")
    assert not over, (
        "Telegram refuses a toast this long — the button goes dead with no "
        "message to the user:\n" + "\n".join(sorted(set(over)))
    )
    # A catalogue that failed to load would make the loop above measure
    # nothing at all and still report success.
    assert measured >= 150, measured


def test_every_answer_receiver_is_accounted_for() -> None:
    """Guard the guard: the scan hinges on recognising the receiver.

    Rename ``callback`` to ``cq`` in a handler and the toast check would
    quietly stop covering that file. Forcing the two sets to partition
    every receiver in the tree turns that rename into a failing test
    instead of a silent hole.
    """
    seen = {receiver for _where, _node, receiver in _answer_calls()}
    unclassified = seen - _CALLBACK_RECEIVERS - _OTHER_RECEIVERS
    assert not unclassified, (
        "new .answer() receiver — is it a CallbackQuery (200-char toast) or "
        f"a Message (4096)? classify it: {sorted(unclassified)}"
    )
    assert seen >= _CALLBACK_RECEIVERS, sorted(_CALLBACK_RECEIVERS - seen)


def test_the_scan_actually_finds_the_toasts() -> None:
    """Guard the guard: an empty site list would pass every assertion."""
    sites = _toast_sites()
    assert len(sites) >= 90, len(sites)
    assert len({key for _where, key in sites}) >= 60, sites
    # The one toast wrapped in ``plain_text`` — an alert has no
    # parse_mode, so p2p strips the card's markup before showing it. If
    # the unwrapping breaks, that site drops out unmeasured.
    assert "h_p2p_buy_above_max" in {key for _where, key in sites}
