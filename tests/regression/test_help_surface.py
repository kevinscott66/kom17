"""RR-6 #62/#63 backstop: ``/help`` must advertise exactly what's wired.

A help card is a promise. Two ways to break it, and this file owns both:

* **Advertising a dead command.** The catalog in ``core/ranks`` is a
  verbatim legacy port and still lists rows the new pipeline never
  registered. Printing one sends the user to a silent no-op — the worst
  possible outcome for the surface whose entire job is discovery.
* **Hiding a live one.** :data:`HELP_HIDDEN_KEYS` is a *temporary*
  exclusion list. Once a hidden command is ported (``/city`` is next,
  RR #74) nothing would otherwise remind anyone to un-hide it, and the
  command would ship invisible — which is the regression this whole
  wave exists to undo.
* **Never mentioning it in the first place.** The one above only
  catches a command somebody deliberately hid. A command that was
  simply never added to the catalog is invisible by default and
  nothing complains — which is how 74 of them accumulated. So the
  audit runs in both directions: every advertised key must be live,
  and every live token must reach an advertised key.

Both directions are asserted against the **live router tree** (the
``registered_commands`` fixture in ``conftest.py``), never against a
hand-kept list, so the audit cannot agree with a stale copy of itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pytest

from telegram_invite_bot.core.ranks import COMMAND_ENTRIES, command_key_for
from telegram_invite_bot.handlers.help_catalog import (
    HELP_HIDDEN_KEYS,
    KOM_PREFIX_HINTS,
    NO_SLASH_TRIGGERS,
    OWNER_CATEGORIES,
    USER_CATEGORIES,
    visible_keys,
)
from telegram_invite_bot.middlewares.text_alias import _ALIAS_MAP

if TYPE_CHECKING:
    from collections.abc import Iterable

pytestmark = pytest.mark.integration

_ALL_CATEGORIES = USER_CATEGORIES + OWNER_CATEGORIES

#: The one surface ``/help`` deliberately does not index. ``/admin_*``
#: is the developer diagnostics console (~110 commands), documented by
#: its own ``/admin_help`` card — which *is* catalogued and advertised,
#: so the doorway is discoverable even though the rooms behind it are
#: not. Matched on the owning module rather than on a name prefix: a
#: handler that lives in the ops package but forgot the prefix is
#: exactly the kind of drift a prefix check would wave through.
_OPS_PACKAGE: Final[str] = "telegram_invite_bot.handlers.admin."


def _is_ops_only(owners: Iterable[str]) -> bool:
    """True when every handler behind a token is ops-console code."""
    owner_list = list(owners)
    return bool(owner_list) and all(_OPS_PACKAGE in owner for owner in owner_list)


def test_every_advertised_command_is_registered(registered_commands: set[str]) -> None:
    """No card line may point at a command the router doesn't answer."""
    dead = sorted(key for key in visible_keys(_ALL_CATEGORIES) if key not in registered_commands)
    assert not dead, (
        f"/help advertises {len(dead)} unregistered command(s): {dead}. "
        "Either port them or add them to HELP_HIDDEN_KEYS with a reason."
    )


def test_every_advertised_alias_is_registered(registered_commands: set[str]) -> None:
    """The test above checks catalog *keys*. The site prints *aliases*.

    ``cms/guide_site/command_index.py`` renders every non-``kom_`` alias
    of an advertised row as its own ``/name`` chip, so a row whose key
    is live can still put a dead name in front of the user — and 27 of
    them did (#115): ``/wedding``, ``/coin``, ``/rates``, ``/unmarry``,
    ``/couples``… all inherited from the legacy catalog table, most
    never registered by legacy either. The key-level audit waved every
    one of them through, because the key was fine.

    ``kom_*`` spellings are checked too even though the site hides them:
    they are the multi-bot addressing form, and a hidden alias that
    answers with silence is still a broken promise to the chat that
    needs it — five of them were dead and are registered now.

    Scope, established by falsifying this test: it asks "does *some*
    handler answer this name", not "does it answer everywhere the
    canonical does". Stripping ``kom_start`` from the private
    registration alone leaves the group one and this stays green. A
    per-chat-type gap is a different defect class and would need the
    registration's chat filter, which the router walk doesn't collect.
    """
    dead: list[str] = []
    advertised = set(visible_keys(_ALL_CATEGORIES))
    for entry in COMMAND_ENTRIES:
        if entry.key not in advertised:
            continue
        dead.extend(
            f"{alias} (row {entry.key})"
            for alias in entry.aliases
            if alias not in registered_commands
        )
    assert not dead, (
        f"{len(dead)} advertised alias(es) reach no handler: {sorted(dead)}. "
        "Either register the spelling on the owning handler or drop it "
        "from the CommandEntry — the site prints it either way."
    )


#: Routers every token falls through to once the owning handler has had
#: its turn — the "wrong chat type" refusal and the "wrong argument
#: shape" hint. They answer for /weather and /forecast alike, so leaving
#: them in the owner set would make every alias look like it shared a
#: handler with its row and the audit below would see nothing.
_FALLTHROUGH_MODULES: Final[tuple[str, ...]] = (
    "handlers.group_only",
    "handlers.unknown_form",
)


def _owning_handlers(token: str, handlers: dict[str, set[str]]) -> set[str]:
    """Who really runs for ``token``, fall-through routers excluded."""
    return {
        owner
        for owner in handlers.get(token, set())
        if not any(module in owner for module in _FALLTHROUGH_MODULES)
    }


def test_aliases_that_are_really_separate_commands_are_declared(
    registered_command_handlers: dict[str, set[str]],
) -> None:
    """#164: a row's alias must be a *spelling* of it, or say otherwise.

    ``CommandEntry.aliases`` is two things at once. The rank gate reads
    it as "every token this switch covers", which is why ``/cpc_cancel``
    has to be in there — take it out and the owner's ``/cmdcfg`` off
    switch stops covering it. The site and ``/cmdcfg show`` read it as
    "other ways to type this command", which about ``/cpc_cancel`` is
    simply false.

    Three rows carry such a token today and each one declares it in
    ``subcommands``, so the display layers can subtract them and print
    them with their own description. This audit stops a fourth from
    arriving unannounced: an alias whose handler is disjoint from its
    row's canonical handler is not a spelling of it and must say so.

    Handler identity, not the ``Command(...)`` call, is what decides —
    ``/ask`` is registered by its own call and is still a genuine alias
    of ``/ai``, because both reach ``_handle_ask``.
    """
    undeclared: list[str] = []
    for entry in COMMAND_ENTRIES:
        canonical = _owning_handlers(entry.key, registered_command_handlers)
        if not canonical:
            continue
        declared = {token for sub in entry.subcommands for token in sub.aliases}
        for alias in entry.aliases:
            if alias == entry.key or alias in declared:
                continue
            own = _owning_handlers(alias, registered_command_handlers)
            if own and not (own & canonical):
                undeclared.append(f"/{alias} (row /{entry.key} → {sorted(own)})")
    assert not undeclared, (
        f"{len(undeclared)} catalog alias(es) run a different handler than the row "
        f"advertising them: {sorted(undeclared)}. They are commands, not spellings — "
        "declare each one as a SubCommand on its row (keeping the token in `aliases` "
        "so the /cmdcfg gate still covers it) and write its h_subcmd_<key> copy."
    )


def test_hidden_keys_are_all_genuinely_dead(registered_commands: set[str]) -> None:
    """A hidden key that became live must be un-hidden, not left buried."""
    resurrected = sorted(key for key in HELP_HIDDEN_KEYS if key in registered_commands)
    assert not resurrected, (
        f"{resurrected} are registered but hidden from /help — drop them from "
        "HELP_HIDDEN_KEYS and add their h_cmd_<key> description."
    )


def test_every_registered_command_is_advertised(
    registered_command_handlers: dict[str, set[str]],
) -> None:
    """The other direction: nothing live may be missing from the card.

    ``test_every_advertised_command_is_registered`` only stops the card
    from *lying*; it says nothing about the far more common failure,
    which is a command that ships and is never mentioned. That is how
    the catalog got 74 rows behind the router — every command ported
    after the legacy table was copied landed outside it, so ``/help``
    and the generated site page between them advertised well under half
    the working bot, and ``/cmdcfg`` could not address the rest at all
    (it only accepts catalog keys).

    A token counts as advertised when its *catalog key* is on the card:
    ``/ник`` needs no row of its own, it needs to resolve to ``nick``.
    That is deliberately the same resolution
    :class:`CommandAccessMiddleware` performs, so a spelling that slips
    past this audit is also a spelling that slips past the rank gate —
    which is exactly what ``/бан`` and ``/модконфиг`` did while their
    latin twins sat at rank 2 and rank 5.
    """
    unadvertised = sorted(
        token
        for token, owners in registered_command_handlers.items()
        if command_key_for(token) not in set(visible_keys(_ALL_CATEGORIES))
        and not _is_ops_only(owners)
    )
    assert not unadvertised, (
        f"{len(unadvertised)} registered command(s) reach no catalog row: "
        f"{unadvertised}. Give each one a CommandEntry (a new row, or an "
        "alias on the row it is a spelling of) plus h_cmd_<key> copy in "
        "ru.yaml and en.yaml."
    )


def test_the_ops_console_exemption_still_matches_something(
    registered_command_handlers: dict[str, set[str]],
) -> None:
    """Guard the guard: an exemption nobody meets is a hole waiting to
    open. If ``handlers/admin`` is ever renamed, this fails loudly
    instead of the audit above silently starting to exempt nothing —
    or, worse, the module path drifting to a prefix that matches more
    than the ops console."""
    exempt = {
        token for token, owners in registered_command_handlers.items() if _is_ops_only(owners)
    }
    assert len(exempt) > 50, f"ops-console exemption matched only {len(exempt)} token(s)"
    assert "admin_help" in registered_command_handlers, (
        "the ops console has no advertised doorway left"
    )


def test_kom_prefixed_hints_are_registered(registered_commands: set[str]) -> None:
    """The multi-bot tail block names ``/kom_*`` duplicates explicitly;
    each must exist or the perk is a lie in a chat that needs it most."""
    missing = sorted(name for name in KOM_PREFIX_HINTS if name not in registered_commands)
    assert not missing, missing


@pytest.mark.parametrize("lang", sorted(NO_SLASH_TRIGGERS))
def test_no_slash_triggers_resolve(lang: str) -> None:
    """Every advertised slash-free shortcut must be in the alias map —
    a shortcut that exists only in the help card is worse than none."""
    unknown = sorted(word for word in NO_SLASH_TRIGGERS[lang] if word not in _ALIAS_MAP)
    assert not unknown, unknown


def test_no_slash_triggers_cover_both_locales() -> None:
    """en.yaml carries zero Cyrillic (i18n convergence guard), so the
    English card cannot reuse the Russian trigger words — both locales
    need their own list."""
    assert set(NO_SLASH_TRIGGERS) == {"ru", "en"}
    assert NO_SLASH_TRIGGERS["ru"] != NO_SLASH_TRIGGERS["en"]
