"""``.env.example`` documents every env-var that ``Settings`` reads.

Every time a stage adds a new ``Field(..., alias="FOO")`` to one of the
``Settings`` sub-classes, the example file should grow a matching
entry — otherwise operators have no canonical reference and a typo in
prod won't be cross-referenceable. Drift between code and the example
has happened twice during this migration (``LOGS_DIR`` / ``LOG_JSON``
were both missing until Stage 80 caught it), so we lock the invariant
in CI rather than hope for code-review vigilance.

The check tolerates commented-out entries (``# FOO=...``): documenting
an *optional* knob without forcing it active is the whole point of
``# ...`` lines in ``.env.example``.

A small allow-list covers genuinely-internal aliases (currently empty)
that we deliberately don't expose to operators.
"""

from __future__ import annotations

from pathlib import Path

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings

from telegram_invite_bot.config import settings as settings_module

# Aliases we deliberately don't document in .env.example. Keep this
# list minimal and add a WHY-comment whenever you extend it.
_ALLOWED_UNDOCUMENTED: frozenset[str] = frozenset()


def _all_aliases() -> set[str]:
    """Collect every ``alias=`` on every BaseSettings subclass exported
    from :mod:`telegram_invite_bot.config.settings`. We walk the module
    attribute namespace so newly-added settings classes are picked up
    automatically — no central registry to keep in sync.
    """
    aliases: set[str] = set()
    for name in dir(settings_module):
        obj = getattr(settings_module, name)
        if isinstance(obj, type) and issubclass(obj, BaseSettings) and obj is not BaseSettings:
            for field in obj.model_fields.values():
                assert isinstance(field, FieldInfo)
                if field.alias:
                    aliases.add(field.alias)
    return aliases


def test_env_example_documents_every_settings_alias() -> None:
    env_example = Path(__file__).resolve().parents[3] / ".env.example"
    contents = env_example.read_text(encoding="utf-8")

    aliases = _all_aliases() - _ALLOWED_UNDOCUMENTED
    missing: list[str] = []
    for alias in sorted(aliases):
        # Match either ``ALIAS=`` (set) or ``# ALIAS=`` (documented but
        # left for the operator to fill in). The trailing ``=`` anchors
        # against substrings like ``LOG_LEVEL`` appearing inside the
        # ``LOG_LEVEL=INFO`` line for ``LOG``.
        if f"{alias}=" not in contents:
            missing.append(alias)

    assert not missing, (
        ".env.example is missing entries for these Settings aliases: "
        f"{missing}. Add a documenting line (commented-out is fine) so "
        "operators have a single source of truth."
    )
