"""#1597: AI failure copy lives in i18n, not in the service.

``services/ai_service.py`` used to catch :class:`AiRequestError` in both
public wrappers and return one of seven hardcoded Russian sentences AS
THE ANSWER — an English reader got them verbatim, one of them named the
provider credential fault, and ``handlers/ai.py`` told a real answer
from a degraded one by testing ``answer.startswith("❌")`` before
writing it into the conversation memory.

These are source pins, not behaviour tests: the behaviour is covered in
``tests/e2e/handlers/test_ai.py``. What they defend is the shape — a
future edit that puts user-facing prose back into the service, or that
adds an ``AiRequestError`` reason nobody wrote a key for, fails here
instead of shipping a Russian sentence to an English reader again.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest
import yaml

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"
_SERVICE = (SRC_ROOT / "services" / "ai_service.py").read_text(encoding="utf-8")
_HANDLER = (SRC_ROOT / "handlers" / "ai.py").read_text(encoding="utf-8")

_KEYS = (
    "h_ai_error_timeout",
    "h_ai_error_network",
    "h_ai_error_http",
    "h_ai_error_empty",
    "h_ai_error_unknown",
    "h_ai_error_unavailable",
)

# ``bad_response`` and ``empty_content`` are absent from the handler's
# table ON PURPOSE: both mean "upstream answered, but with nothing we
# could use", which is one sentence (``h_ai_error_unknown``), not two.
_FALLS_THROUGH = frozenset({"bad_response", "empty_content"})


def _code(source: str) -> str:
    """The source with comments dropped, joined tight.

    Both pins below have to survive PROSE about the old shape: the
    ticket asked for the history to be written down, and the words
    ``startswith("❌")`` now live in the very comment that explains
    why the call is gone. A naive substring pin over the raw file
    would fail on its own documentation.
    """
    readline = io.StringIO(source).readline
    return "".join(
        tok.string for tok in tokenize.generate_tokens(readline) if tok.type != tokenize.COMMENT
    )


def _live_strings(source: str) -> list[str]:
    """Every string literal that is not a docstring."""
    tree = ast.parse(source)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr):
                first = body[0].value
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    docs.add(id(first))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
    ]


def _locale(name: str) -> dict[str, str]:
    path = SRC_ROOT / "i18n" / "data" / f"{name}.yaml"
    loaded: dict[str, str] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def test_the_service_carries_no_user_facing_copy() -> None:
    """No ``❌`` line survives as a VALUE in the service.

    The marker only ever appeared at the head of a sentence meant
    for a reader, so a live string carrying it is prose the service
    has no business owning. Docstrings are exempt: two of them now
    explain why the marker is gone.
    """
    assert not [s for s in _live_strings(_SERVICE) if "❌" in s]


def test_the_handler_no_longer_sniffs_for_the_marker() -> None:
    """The memory write used to be gated on ``not startswith("❌")``.

    The comment that says so is still there on purpose — this pin
    reads code only, so restoring the branch fails while keeping
    its obituary does not.
    """
    assert 'startswith("❌")' not in _code(_HANDLER)


def test_every_raised_reason_is_answerable() -> None:
    """A new ``AiRequestError("...")`` needs a key or a deliberate
    fall-through; otherwise it silently borrows another reason's line."""
    raised = set(re.findall(r'AiRequestError\("([a-z_]+)"', _SERVICE))
    assert raised, "the reason literals moved — this pin needs rewriting"
    table = set(re.findall(r'^    "([a-z_]+)": "h_ai_error_', _HANDLER, re.M))
    assert raised - table == _FALLS_THROUGH


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("key", _KEYS)
def test_both_locales_carry_the_key(key: str, lang: str) -> None:
    assert _locale(lang)[key].strip()


@pytest.mark.parametrize("key", _KEYS)
def test_the_english_line_has_no_cyrillic(key: str) -> None:
    value = _locale("en")[key]
    assert not any("Ѐ" <= ch <= "ӿ" for ch in value), value


def test_the_unavailable_line_names_no_credential_fault() -> None:
    """The whole point of collapsing 401 and 402 into one line.

    The reader is told to contact the administrator; WHICH of a bad key
    and an empty balance it was is the owner's business, and it reaches
    the owner through the ERROR-level log instead.
    """
    for lang, forbidden in (
        ("ru", ("ключ", "баланс")),
        ("en", ("key", "balance")),
    ):
        value = _locale(lang)["h_ai_error_unavailable"].lower()
        for word in forbidden:
            assert word not in value, (lang, word, value)
