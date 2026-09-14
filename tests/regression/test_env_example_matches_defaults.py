"""#1962: ``.env.example`` must not promise a default the code does not hold.

The file documents every knob twice — once in a prose line naming the
defaults ("deepseek-chat, 60s, 1024 tokens, 0.7") and once as a
commented-out assignment an operator can uncomment. Neither half is
checked against :mod:`telegram_invite_bot.config.settings` by anything,
so a default that moves in code leaves the example behind silently.

That is exactly what happened to ``DEEPSEEK_MAX_TOKENS``: the example
said 1024 while :class:`AiConfig` had been 1000 for some time. Small in
itself — a 2.4% shorter completion budget than advertised, visible as
unexplained truncation to whoever sized a prompt against the documented
number — but the commented line is worse than the prose one, because
uncommenting it CHANGES behaviour while looking like it preserves it.

The value of pinning this is the next drift, not that one: the same
silence would cover a cost ceiling or a security default.

Only commented-out assignments are compared, and only where the field
has a real default: an uncommented line is the operator's own value
(``DEEPSEEK_API_KEY=sk-...``) and a ``None`` default means the
commented line is an EXAMPLE of a value, not a statement about one.
Comparison is by parsed type, so ``60`` matching ``60.0`` and ``true``
matching ``True`` are not drift — the env layer parses both.

#2000 asks a different question of the same file: not whether a
documented default still matches the code, but whether the variable
exists at all. ``OPENWEATHER_API_KEY`` and ``ENABLE_NEW_PIPELINE``
were both still there, both UNCOMMENTED, long after the weather
service moved to keyless Open-Meteo and T-011 removed the strangler
bridge. They survived because they sat in the gap between the two
guards that existed: :mod:`tests.unit.config.test_env_example_completeness`
only checks alias → documented, and everything above only reads
COMMENTED lines.

The project already has an opinion about such a key, and it is not a
mild one: :func:`~telegram_invite_bot.config.settings._warn_on_stray_env_keys`
logs a WARNING at boot for any env var that matches a project prefix
and is not a Settings alias, and ``_SEED_ENV_PREFIXES`` keeps
``OPENWEATHER_`` and ``ENABLE_`` alive *on purpose* so exactly these
two are caught. So the file whose whole job is to tell an operator
what to put in ``.env`` — the one ``docs/DEPLOY.md`` calls "the list
of keys, not of values" — was telling them to set two things the bot
would then complain about.

The check below is that opinion turned around and pointed at the
example: load every name the file mentions into an otherwise empty
environment and ask the real ``_find_stray_env_keys`` what it makes
of them. Commented lines count here, unlike above — a commented
assignment is an invitation to uncomment it, and uncommenting a dead
one is how the operator ends up with the warning.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from pydantic_settings import BaseSettings

from telegram_invite_bot.config import settings as settings_module

_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
_ASSIGNMENT = re.compile(r"^#\s*([A-Z][A-Z0-9_]*)=(.*)$")
#: The same line, with the comment marker optional: #2000 cares about
#: the NAME, and a name is equally wrong whether or not it is live.
_ANY_ASSIGNMENT = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _alias_defaults() -> dict[str, list[tuple[str, Any, type]]]:
    """Map every settings alias to its declared default and type."""
    found: dict[str, list[tuple[str, Any, type]]] = {}
    for name in dir(settings_module):
        obj = getattr(settings_module, name)
        if not (isinstance(obj, type) and issubclass(obj, BaseSettings)):
            continue
        for field in obj.model_fields.values():
            if field.alias is None:
                continue
            found.setdefault(field.alias, []).append(
                (name, field.default, field.annotation)  # type: ignore[arg-type]
            )
    return found


def _matches(written: str, default: Any, annotation: Any) -> bool:
    """Does the example's text parse to the declared default?"""
    if isinstance(default, bool):
        lowered = written.strip().lower()
        if lowered in _TRUE:
            return default is True
        if lowered in _FALSE:
            return default is False
        return False
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        try:
            return float(written) == float(default)
        except ValueError:
            return False
    return written == str(default)


def _documented_defaults() -> list[tuple[int, str, str, Any, str]]:
    """Commented assignments in the example that state a real default."""
    aliases = _alias_defaults()
    rows: list[tuple[int, str, str, Any, str]] = []
    for lineno, raw in enumerate(_ENV_EXAMPLE.read_text().splitlines(), start=1):
        match = _ASSIGNMENT.match(raw.strip())
        if match is None:
            continue
        alias, written = match.group(1), match.group(2).strip()
        if not written:
            continue
        for owner, default, annotation in aliases.get(alias, []):
            # ``None`` default → the commented line is an example value.
            if default is None or repr(default) == "PydanticUndefined":
                continue
            rows.append((lineno, alias, written, (default, annotation), owner))
    return rows


def test_the_example_documents_at_least_the_known_knobs() -> None:
    """A guard on the guard: an empty sweep would pass vacuously."""
    assert len(_documented_defaults()) >= 15


@pytest.mark.parametrize(
    ("lineno", "alias", "written", "declared", "owner"),
    _documented_defaults(),
    ids=lambda value: str(value) if isinstance(value, str) else "",
)
def test_a_commented_default_equals_the_code_default(
    lineno: int, alias: str, written: str, declared: tuple[Any, Any], owner: str
) -> None:
    """Uncommenting the line must be a no-op, not a behaviour change."""
    default, annotation = declared
    assert _matches(written, default, annotation), (
        f".env.example:{lineno} says {alias}={written}, but {owner}.{alias} defaults to {default!r}"
    )


def _named_variables() -> dict[str, int]:
    """Every variable the example names, commented or not → first line."""
    names: dict[str, int] = {}
    for lineno, raw in enumerate(_ENV_EXAMPLE.read_text().splitlines(), start=1):
        match = _ANY_ASSIGNMENT.match(raw.strip())
        if match is not None:
            names.setdefault(match.group(1), lineno)
    return names


def _stray_among(names: dict[str, int]) -> set[str]:
    """What the bot would warn about if ``.env`` held exactly ``names``.

    The real function, not a re-implementation of it: the whole value of
    this test is that it cannot drift away from the boot-time check.

    The instance is hollow on purpose. :func:`_find_stray_env_keys`
    reads nothing off it but ``type(settings)``, while a real
    ``Settings`` cannot be built here at all — every sub-config carries
    ``env_file=".env"``, so validating one would read the developer's
    own file and make the answer depend on whose machine ran the test.
    Passing an explicit value for every field keeps the default
    factories (which are what would do that reading) from ever running.
    """
    hollow = settings_module.Settings.model_construct(
        **dict.fromkeys(settings_module.Settings.model_fields, None)
    )
    with mock.patch.dict(os.environ, dict.fromkeys(names, ""), clear=True):
        return settings_module._find_stray_env_keys(hollow)


def test_the_stray_check_still_notices_a_dead_variable() -> None:
    """Guard the guard: prove the question is not answered vacuously."""
    assert _stray_among({"ENABLE_NEW_PIPELIN": 0}) == {"ENABLE_NEW_PIPELIN"}


def test_the_example_never_names_a_variable_the_bot_calls_stray() -> None:
    """An example key with no field behind it is worse than an absent one."""
    names = _named_variables()
    assert len(names) >= 50, "the sweep found almost nothing — it broke, not the file"
    stray = _stray_among(names)
    assert not stray, (
        ".env.example names variables the bot itself would warn about at"
        " boot: they match a project-owned prefix but no Settings alias"
        " backs them, so setting one configures nothing. Either restore"
        " the field or take the line out:\n"
        + "\n".join(f"  .env.example:{names[key]} {key}" for key in sorted(stray))
    )
