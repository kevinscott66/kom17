"""Command registry — typed replacement for legacy ``command_aliases.py``.

.. warning::

   **Nothing in ``src/`` imports this module.** As of 2026-08-19 the only
   importers are its own tests (``tests/unit/commands/``).  It is *not*
   the catalog the bot runs on:

   * ``/help`` and the public ``/commands`` page are built from
     :data:`telegram_invite_bot.core.ranks.COMMAND_ENTRIES` plus
     :mod:`telegram_invite_bot.handlers.help_catalog`;
   * plain-text aliases (``баланс``, ``топ`` …) are resolved by
     :mod:`telegram_invite_bot.middlewares.text_alias`;
   * dispatch itself is aiogram's ``Command`` filter in each handler.

   Editing ``data/registry.yaml`` therefore changes nothing at runtime.
   An earlier version of this warning singled out "11 rows that name
   dev-only legacy commands with no handler at all" — that was wrong in
   both directions.  Every one of the 122 rows is handler-less *in
   ``src/``*, that being the whole point of the paragraph above; and the
   eleven that were named all do have legacy handlers (``sql``
   ``bot.py:25701``, ``backup`` ``:25657``, ``logs`` ``:25600``,
   ``reload`` ``:25538``, ``cancel_game`` ``rock_paper_scissors.py:454``
   …).  Fix a command in ``core/ranks.py`` and its handler, not here.

   The one advantage this module was written for — stripping the
   ``cmd@botname`` suffix Telegram appends in groups — is obsolete:
   aiogram's ``Command`` filter strips it natively.  Whether to delete the
   package outright is the owner's call; see ``docs/REMAINING_WORK.md``.

The legacy module was an 884-line file with a 751-line untyped dict
plus two helper functions. We split data and code:

* ``data/registry.yaml`` — declarative, easy to edit (translators /
  product can extend aliases without touching Python).
* ``registry.py`` (this module) — frozen :class:`CommandSpec` dataclass,
  case-insensitive lookup, ``@cache``-d loader.

API:
    ``find_canonical("/Время@somebot foo bar")`` → ``"time"`` or ``None``
    ``get_spec("time")`` → ``CommandSpec(...)``
    ``iter_commands()`` → iterator over (canonical, spec) pairs

Compared to the legacy helpers:

* ``find_canonical`` accepts the ``cmd@botname`` suffix Telegram appends
  in group chats, which the legacy ``get_canonical_command`` did *not*
  strip.  It never bit a user, though, and the claim that "that bug
  shipped to prod" — which stood here until #710 — was never true:
  ``bot.py:692`` imports ``get_canonical_command`` and never calls it,
  and its single call site anywhere is ``universal_handler.py:28``, a
  module no ``.py`` file in the repo imports.  Dispatch always went
  through pyTelegramBotAPI's own command matching.  Treat this as a
  latent defect we declined to inherit, not a fix.

* Returns a typed :class:`CommandSpec`, not a free-form dict, so a typo
  on ``spec.contexts`` is a mypy error rather than a silent KeyError at
  runtime.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

_DATA_FILE = Path(__file__).parent / "data" / "registry.yaml"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One row of the registry. Frozen so a caller can't mutate the
    cached singleton and accidentally corrupt every subsequent lookup.
    """

    canonical: str
    handler: str
    aliases: tuple[str, ...]
    contexts: tuple[str, ...]
    description: str = ""

    def matches(self, token: str) -> bool:
        """Case-insensitive membership in ``aliases`` (canonical name
        is always implicitly an alias)."""
        t = token.lower().lstrip("/")
        return t == self.canonical or t in self.aliases


@cache
def _load() -> Mapping[str, CommandSpec]:
    """Parse the YAML once. Empty file / missing file degrades to an
    empty mapping so a partial deploy still boots — every caller
    treats a missing canonical as "unknown command" anyway.
    """
    if not _DATA_FILE.is_file():
        return {}
    raw: Any = yaml.safe_load(_DATA_FILE.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, CommandSpec] = {}
    for canon, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        # Normalise to lower-case once on load so every lookup is O(1)
        # against a hashed set instead of a per-call list scan.
        aliases = tuple(sorted({str(a).lower() for a in payload.get("aliases", [])}))
        contexts = tuple(sorted({str(c) for c in payload.get("contexts", [])}))
        result[str(canon).lower()] = CommandSpec(
            canonical=str(canon).lower(),
            handler=str(payload.get("handler") or ""),
            aliases=aliases,
            contexts=contexts,
            description=str(payload.get("description") or ""),
        )
    return result


@cache
def _alias_index() -> Mapping[str, str]:
    """Reverse lookup: ``alias_lower → canonical``. Built once.

    The legacy lookup was a linear scan over 122 specs.  It was never
    hot — see the module docstring: ``get_canonical_command`` had no
    reachable call site at all — so the flame-graph story this docstring
    used to tell was invented (#710).  The index stays because O(1) is
    the right shape for a reverse lookup, not because it recovered any
    measured time.
    """
    idx: dict[str, str] = {}
    for canonical, spec in _load().items():
        idx[canonical] = canonical
        for alias in spec.aliases:
            idx[alias] = canonical
    return idx


def find_canonical(text: str | None) -> str | None:
    """Resolve any alias (with or without leading ``/``, with or
    without ``@botname`` suffix, any case) to a canonical command.

    Returns ``None`` for empty input or unknown alias.
    """
    if not text:
        return None
    head = text.strip().split(maxsplit=1)
    if not head:
        return None
    token = head[0].lstrip("/").lower()
    # Strip ``@botname`` that Telegram appends in group chats.
    at = token.find("@")
    if at >= 0:
        token = token[:at]
    if not token:
        return None
    return _alias_index().get(token)


def get_spec(canonical: str) -> CommandSpec | None:
    """Direct lookup by canonical name. Use after :func:`find_canonical`."""
    return _load().get(canonical.lower())


def iter_commands() -> Iterator[tuple[str, CommandSpec]]:
    """Yield ``(canonical, spec)`` pairs in registry order."""
    return iter(_load().items())
