"""Every env var the source prose names must be one an operator can set.

#1995. ``observability/sentry.py`` told the reader, in a comment beside
the call, to "raise via ``SENTRY_TRACES_SAMPLE_RATE`` in incidents to
capture more spans". The line under it passed a hardcoded ``0.0`` and no
such setting existed anywhere. That is a worse defect than a wrong
comment: it is an instruction meant to be followed under pressure, by
someone who will set the variable, restart, see no traces, and conclude
the problem is elsewhere.

It is the same shape as #1993's rotten line citations — prose asserting
something about the code that nobody re-checks — so it gets the same
treatment: a test, not a one-off correction.

The discriminator is already in the settings module. ``Settings`` derives
:func:`_known_env_prefixes` from its own field aliases so that
:meth:`Settings._warn_on_stray_env_keys` can flag variables in
``os.environ`` that look like ours but match no field. A name in a
comment is the same question asked one step earlier, so this test points
the same machinery at the prose: a token shaped like an env var, sitting
in our namespace, that is not an alias, is either a knob someone forgot
to build or a name that needs saying differently.

Deliberately narrow. Only tokens carrying a known project prefix are
considered, because the package's prose is full of ``CAP_SYS_PTRACE``,
``RLIMIT_MEMLOCK`` and ``FIN_WAIT1`` — 170 of them — and none of those
are ours to implement.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import telegram_invite_bot
from telegram_invite_bot.config.settings import (
    _NON_SETTINGS_ENV_KEYS,
    Settings,
    _collect_known_aliases,
    _known_env_prefixes,
)

_ROOT = Path(telegram_invite_bot.__file__).parent

#: Env-var-shaped names that are correctly NOT settings. Each is here
#: with a reason, because "add it to the allow-list" is how a guard like
#: this rots into decoration.
_NOT_OUR_KNOBS: dict[str, str] = {
    # Legacy monolith variables, every one cited with its bot.py line as
    # a statement about what the OLD deployment did. Implementing them
    # here would be porting a feature, not fixing a comment.
    "PVP_OFFER_TTL_SEC": "legacy bot.py:3782, mirrored as a constant",
    "PVP_GAMES_ONLY": "legacy bot.py:17451 gate, deliberately not ported",
    "BOT_USER_IDS": "legacy blocklist, cited to explain our fail-open posture",
    "DEVELOPER_IDS": "the derived Settings.developer_ids set, not an alias",
    # Names appearing as examples of what the code does to OTHER
    # people's variables — redaction and stray-key detection.
    "STRIPE_SECRET_KEY": "redaction example in config/logging",
    "OPENWEATHER_API_KEY": "redaction example in config/logging",
    "DATABASE_DSN": "env-scan example in handlers/admin/envscan",
    "ENABLE_NEW_PIPELIN": "deliberate typo demoing the stray-key warning",
    # Not an env var at all.
    "BOT_WIN": "RpsOutcome enum member named in a docstring",
}

_TOKEN = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")


def _prose_and_names() -> tuple[dict[str, set[str]], set[str]]:
    """Return ``(token -> {file:line}, every name defined in the package)``.

    The second half is what keeps ordinary module constants out: a token
    the package defines is Python, not a promise to an operator.
    """
    prose: dict[str, set[str]] = {}
    defined: set[str] = set()
    for path in sorted(_ROOT.rglob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                defined.add(node.id)
            elif isinstance(node, ast.Attribute):
                defined.add(node.attr)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                defined.add(node.name)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
        chunks = [
            (number, text[text.index("#") :])
            for number, text in enumerate(source.splitlines(), 1)
            if "#" in text
        ]
        chunks += [
            (node.lineno, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        rel = path.relative_to(_ROOT).as_posix()
        for number, text in chunks:
            for match in _TOKEN.finditer(text):
                prose.setdefault(match.group(1), set()).add(f"{rel}:{number}")
    return prose, defined


def test_no_comment_promises_an_env_var_that_does_not_exist() -> None:
    aliases = {alias.upper() for alias in _collect_known_aliases(Settings)}
    aliases |= {key.upper() for key in _NON_SETTINGS_ENV_KEYS}
    prefixes = tuple(sorted(_known_env_prefixes()))
    prose, defined = _prose_and_names()

    phantom = sorted(
        f"{token} cited at {sorted(sites)}"
        for token, sites in prose.items()
        if token.startswith(prefixes)
        and token not in aliases
        and token not in defined
        and token not in _NOT_OUR_KNOBS
    )
    assert not phantom, (
        "the prose names env vars nothing reads — build the setting, or say"
        " what the code actually reads:\n" + "\n".join(phantom)
    )
