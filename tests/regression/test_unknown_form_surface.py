"""Regression guard: the unknown-form hint must not become an oracle (#158).

``handlers/unknown_form`` is the last child of the router tree. It
matches a command word the tree itself registers, with no argument
guard at all, so it answers exactly the invocations every real handler
declined — the mistyped ``/roll 7`` that used to fall through to legacy
and now falls through to nothing.

A router built that way has two ways to go wrong, and neither shows up
in a per-command e2e test:

* **It can shadow a working handler.** It is deliberately promiscuous:
  it accepts *any* argument shape. If it were ever included before the
  module that owns a form, that module would stop running and the user
  would be told their correct invocation was unrecognised.
* **It can confirm a command the user was never meant to find.** The
  word list is derived from the tree, and the tree registers the ~110
  ``/admin_*`` console commands, ``/payment_keys``,
  ``/set_crypto_token``. Replying "🤔 /admin_disk — …" to whoever
  guessed the name hands over the map. The rank check inherited from
  #123 does *not* cover them: none of those words has a catalog row, so
  ``default_min_rank`` reports 0 for all of them, i.e. "ordinary user
  command". The filter that actually holds is "``/help`` can describe
  it", and this suite is what keeps that filter honest.

Everything here is asserted against the assembled production tree, not
against a list — a hand-kept copy of "which words are safe" would drift
the day a command is added, which is precisely the failure it would be
there to prevent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest
from aiogram.filters import Command

from telegram_invite_bot.core.ranks import (
    RankLevel,
    command_entry,
    command_key_for,
    default_min_rank,
)
from telegram_invite_bot.handlers.help_catalog import (
    HELP_HIDDEN_KEYS,
    OWNER_CATEGORIES,
    USER_CATEGORIES,
    visible_keys,
)
from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from aiogram import Dispatcher, Router

pytestmark = pytest.mark.integration

#: Name given to the tail router in ``build_unknown_form_router``.
_TAIL: Final[str] = "unknown_form"

#: The developer console. Same constant, same reasoning as
#: ``test_help_surface._OPS_PACKAGE``: matched on the owning module
#: rather than on a ``/admin_`` name prefix, because a console handler
#: that forgot the prefix is exactly the drift a prefix check waves
#: through — and it is the one that would leak.
_OPS_PACKAGE: Final[str] = "telegram_invite_bot.handlers.admin."

#: Guard-the-guard sample. Ordinary, catalogued, user-facing commands
#: whose off-contract forms are the ones #158 was opened for. If the
#: tail ever stops covering these, the audits below would all still
#: pass over an empty word list.
_MUST_COVER: Final[frozenset[str]] = frozenset(
    {"roll", "dice", "flip", "daily", "balance", "stats", "achievements", "top"}
)


def _words(router: Router) -> set[str]:
    """Every command word registered directly on ``router``."""
    return {
        token
        for handler in router.message.handlers
        for flt in handler.filters or []
        # Bound to a local first: aiogram types ``FilterObject.callback``
        # as a bare ``Callable``, so the ``isinstance`` narrowing has to
        # land on something mypy can carry into the inner loop.
        for callback in [getattr(flt, "callback", None)]
        if isinstance(callback, Command)
        for token in callback.commands
        if isinstance(token, str)
    }


def _owners(router: Any, acc: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """``{word: {"module.func", …}}`` over ``router`` and its children."""
    served = {} if acc is None else acc
    observer = getattr(router, "message", None)
    if observer is not None:
        for handler in observer.handlers:
            fn = handler.callback
            owner = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', '?')}"
            for flt in handler.filters or []:
                callback = getattr(flt, "callback", None)
                if isinstance(callback, Command):
                    for token in callback.commands:
                        if isinstance(token, str):
                            served.setdefault(token, set()).add(owner)
    for sub in getattr(router, "sub_routers", []):
        _owners(sub, served)
    return served


@pytest.fixture
def tail(production_dispatcher: Dispatcher) -> Router:
    """The assembled tail router, found by name rather than by index.

    By name because the assertion that it comes *last* is a test below,
    not a premise the other tests get to assume.
    """
    root = production_dispatcher.sub_routers[0]
    matches = [sub for sub in root.sub_routers if sub.name == _TAIL]
    assert len(matches) == 1, f"expected exactly one {_TAIL!r} router, got {len(matches)}"
    return matches[0]


def test_the_hint_is_the_last_router_that_can_match_a_message(
    production_dispatcher: Dispatcher,
    tail: Router,
) -> None:
    """Nothing that answers a message may be included after the tail.

    aiogram walks children in include order and stops at the first
    match. The tail carries no argument guard, so any command handler
    behind it is dead code — the user's *correct* invocation would be
    answered with "I did not understand that form".

    The errors router is the one legitimate thing after it: it observes
    the ``error`` event, never ``message``, so it cannot intercept.
    """
    root = production_dispatcher.sub_routers[0]
    names = [sub.name for sub in root.sub_routers]
    after = root.sub_routers[names.index(_TAIL) + 1 :]
    intercepts = [sub.name for sub in after if _owners(sub)]
    assert intercepts == [], (
        f"these routers are included after the unknown-form hint and would never run: {intercepts}"
    )
    assert _words(tail), "the tail router registers nothing at all"


def test_the_hint_only_names_commands_help_prints(tail: Router) -> None:
    """Every word in the tail is one ``/help`` already shows.

    ``/help`` is the discovery surface; a word it prints is public by
    construction. Deriving the tail's vocabulary from the same catalog
    means the hint cannot be used to probe for commands — the answer it
    gives is one the user could have read off ``/help`` anyway.
    """
    printable = set(visible_keys(USER_CATEGORIES + OWNER_CATEGORIES))
    leaked = sorted(w for w in _words(tail) if command_key_for(w) not in printable)
    assert leaked == [], (
        f"the unknown-form hint would confirm commands that /help does not advertise: {leaked}"
    )


def test_the_hint_never_names_the_developer_console(
    tail: Router,
    production_dispatcher: Dispatcher,
) -> None:
    """No console word, and no owner-tier word, reaches the tail.

    Stated against the *tree's* ownership rather than against a name
    pattern, so a console command that ships without the ``/admin_``
    prefix is caught too. ``default_min_rank`` is checked as well, but
    it is the weaker of the two: it reports 0 for every word missing a
    catalog row, which is all of the console.
    """
    owners = _owners(production_dispatcher)
    offenders = sorted(
        word
        for word in _words(tail)
        if all(owner.startswith(_OPS_PACKAGE) for owner in owners.get(word, {""}))
        or default_min_rank(command_key_for(word)) >= RankLevel.OWNER
        or command_key_for(word) in HELP_HIDDEN_KEYS
    )
    assert offenders == [], f"the hint would advertise privileged commands: {offenders}"


def test_every_hinted_word_is_describable(tail: Router) -> None:
    """The hint interpolates ``h_cmd_<key>``; a miss renders the raw key.

    :func:`~telegram_invite_bot.i18n.t` returns its key unchanged when
    there is no translation, so a word without a description would put
    the literal text "h_cmd_something" in front of a user. Both catalog
    membership and both languages are checked, because the row and the
    translation are edited in different files.
    """
    missing = []
    for word in sorted(_words(tail)):
        key = command_key_for(word)
        if command_entry(key) is None:
            missing.append(f"/{word}: no catalog row")
            continue
        for lang in ("ru", "en"):
            if t(f"h_cmd_{key}", lang) == f"h_cmd_{key}":
                missing.append(f"/{word}: no {lang} description")
    assert missing == [], "\n  ".join(["undescribable words in the hint:", *missing])


def test_the_hint_covers_the_commands_it_was_opened_for(tail: Router) -> None:
    """Guard the guard: an empty tail satisfies every audit above."""
    covered = _words(tail)
    assert covered >= _MUST_COVER, sorted(_MUST_COVER - covered)


def test_the_hint_word_list_is_derived_not_hand_kept(
    tail: Router,
    production_dispatcher: Dispatcher,
) -> None:
    """Every word in the tail is registered by a real handler too.

    The tail exists to answer forms of commands the bot *has*. A word
    only it registers would be a command that exists solely to say it
    was typed wrong — the sign that the list stopped being read off the
    tree and started being maintained by hand.
    """
    owners = _owners(production_dispatcher)
    itself = f"telegram_invite_bot.handlers.{_TAIL}.handle_unknown_form"
    orphans = sorted(word for word in _words(tail) if not (owners.get(word, set()) - {itself}))
    assert orphans == [], f"the hint claims words no handler owns: {orphans}"
