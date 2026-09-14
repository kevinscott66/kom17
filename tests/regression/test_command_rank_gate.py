"""The rank gate must gate the command the router actually runs.

``CommandAccessMiddleware`` never sees a handler. It takes the typed
token, asks :func:`command_key_for` which catalog row owns it, and
refuses the update when the actor's rank is below that row's
``default_rank``. So a catalog row's alias list is not documentation —
it is a claim that every listed spelling reaches *the same command*.

When that claim is false the failure is invisible in review and silent
in production: the user types a perfectly ordinary command, a handler
they are entitled to run is standing right there, and the middleware
answers with a rank refusal on behalf of an unrelated privileged
command that merely borrowed the name.

That is exactly what ``/check`` did. Legacy registered ``check`` as a
second name for the developer-only *create* command
(``commands=['create_check', 'check']``, ``bot.py:25301``) and the
catalog row was ported verbatim at rank 5. The new pipeline meanwhile
gave the bare name to the thing an ordinary user does with a check —
claim one — on a **private-only** router, where the live-Telegram-admin
bypass in the middleware cannot apply. Result: nobody below rank 5
could redeem a voucher at all, and no e2e test noticed, because those
build schemas without ``ModerationBase``, which makes the gate's
override lookup raise and the middleware fail open.

Three guards, deliberately at different levels:

* :func:`test_no_alias_drags_a_foreign_command_under_a_rank_gate` is the
  structural one — it re-derives the whole invariant from the live
  router tree, so the next alias collision is caught the day it lands.
* :func:`test_check_is_not_gated_as_the_developer_create_command` names
  this specific regression, so a failure reads as the bug rather than
  as an abstract rule violation.
* :func:`test_every_catalog_alias_runs_the_command_its_row_names` covers
  the rank-0 rows the first guard skips. No gate is involved there, so
  nothing is *granted* by a mismatch — but the row still promises the
  user that two spellings are one command, and a rank-0 row breaking
  that promise is a plain lie in ``/help`` and on the site.

The end-to-end proof that a rank-0 user can really claim a check with
the gate wired lives in ``tests/e2e/handlers/test_checks.py``.
"""

from __future__ import annotations

from typing import Final

from telegram_invite_bot.core.ranks import (
    COMMAND_ENTRIES,
    command_key_for,
    default_min_rank,
)


def test_check_is_not_gated_as_the_developer_create_command() -> None:
    """``/check`` (claim) and ``/create_check`` (dev) are separate rows."""
    assert command_key_for("check") == "check"
    assert command_key_for("чек") == "check"
    assert default_min_rank("check") == 0

    # …and the developer command keeps its gate, on both of its names.
    assert command_key_for("create_check") == "create_check"
    assert command_key_for("создать_чек") == "create_check"
    assert default_min_rank("create_check") >= 5


def test_catalog_tokens_are_unique() -> None:
    """No two rows may claim the same spelling.

    ``command_key_for`` is a flat token→key map, so a collision does not
    error — the later row silently wins and the earlier row's rank stops
    applying to that spelling. Uniqueness is what makes the audit below
    meaningful at all.
    """
    seen: dict[str, str] = {}
    collisions: list[str] = []
    for entry in COMMAND_ENTRIES:
        for token in {entry.key, *entry.aliases}:
            previous = seen.get(token)
            if previous is not None:
                collisions.append(f"/{token}: {previous} vs {entry.key}")
            seen[token] = entry.key
    assert not collisions, "catalog rows share a spelling: " + "; ".join(sorted(collisions))


# Gated rows whose spellings really are one command even though the
# router registers a separate callback per spelling. Kept explicit (and
# asserted still-necessary below) rather than loosening the audit,
# because every entry here is a place the structural guard stops
# looking.
#: ``unknown_form`` (#158) registers *every* describable command word
#: in the tree behind one callback, so it is an owner of almost every
#: token. Left in, it makes any two spellings look like the same
#: command and quietly turns both audits below into no-ops. It is a
#: fallback for a *wrong argument form*, never the command itself, so
#: subtracting it is not a carve-out — it is refusing to count a
#: non-answer as an answer.
_HINT_FALLBACK: Final = "telegram_invite_bot.handlers.unknown_form.handle_unknown_form"


def _real_owners(owners: set[str]) -> set[str]:
    """Handlers that actually run the command, hint fallback excluded."""
    return owners - {_HINT_FALLBACK}


_SHARED_ENTRY_POINTS: Final[dict[str, str]] = {
    # ``/aliases`` is a thin front for ``/alias``: with arguments it
    # calls ``handle_alias`` verbatim, bare it lists. Same module, same
    # group-only router, same ``_require_admin`` check — the shared
    # rank 5 is exactly right (``handlers/group_aliases.py``).
    "alias": "/aliases delegates to handle_alias",
}


def test_no_alias_drags_a_foreign_command_under_a_rank_gate(
    registered_command_handlers: dict[str, set[str]],
) -> None:
    """Every gated catalog row's spellings must run the same handler.

    Read from the router side: group the live slash tokens by the
    catalog row that gates them; for any row that gates at all
    (``rank > 0``), the tokens grouped under it must have a handler in
    common. If two of them are served by entirely different handlers,
    the row is gating a command it does not describe.

    Rank-0 rows are exempt on purpose: an alias that resolves to an
    ungated row grants nothing, so a mismatch there is at worst untidy
    naming — this suite is about the gate, and a rank-0 row has none.

    A token owned by several handlers is normal and fine (chat-type
    splits, ``magic=F.args`` pairs), hence the intersection test rather
    than an equality one.
    """
    by_key: dict[str, list[str]] = {}
    for token in registered_command_handlers:
        key = command_key_for(token)
        if key is None or default_min_rank(key) <= 0:
            continue
        by_key.setdefault(key, []).append(token)

    flagged: dict[str, str] = {}
    for key, tokens in sorted(by_key.items()):
        if len(tokens) < 2:
            continue
        shared: set[str] | None = None
        for token in tokens:
            owners = _real_owners(registered_command_handlers[token])
            shared = owners if shared is None else (shared & owners)
        if not shared:
            spellings = ", ".join(f"/{token}" for token in sorted(tokens))
            flagged[key] = f"{key} (rank {default_min_rank(key)}) spans handlers: {spellings}"

    offenders = [text for key, text in sorted(flagged.items()) if key not in _SHARED_ENTRY_POINTS]
    assert not offenders, (
        "a catalog row is rank-gating a command it does not own — "
        "give the odd spelling its own row: " + "; ".join(offenders)
    )

    # …and the escape hatch may not outlive its reason.
    stale = sorted(set(_SHARED_ENTRY_POINTS) - set(flagged))
    assert not stale, (
        "_SHARED_ENTRY_POINTS lists rows the audit no longer flags; "
        f"drop them so the guard keeps its teeth: {stale}"
    )


#: Rows whose spellings really are several commands, kept on one row on
#: purpose. Each is a sub-command of the row it sits on: same module,
#: same feature, rank 0, and listing it separately would scatter one
#: feature across the help card for no reader's benefit.
#:
#: Asserted still-divergent below, so an entry cannot outlive its
#: reason and quietly become a hole in the audit.
_SUBCOMMAND_ROWS: Final[dict[str, str]] = {
    # ``/aliases`` is a thin front for ``/alias`` — see
    # ``_SHARED_ENTRY_POINTS`` above, which gates the same row.
    "alias": "/aliases lists what /alias sets",
    # ``/forecast`` is ``/weather`` over several days: one service, one
    # module, one card family.
    "weather": "/forecast is the multi-day form of /weather",
    # ``/cpc_cancel`` withdraws the challenge ``/cpc`` posts.
    "cpc": "/cpc_cancel withdraws a /cpc challenge",
}


def test_every_catalog_alias_runs_the_command_its_row_names(
    registered_command_handlers: dict[str, set[str]],
) -> None:
    """Every spelling on a row must reach the row's own command.

    The guard above stops at ``rank > 0`` on purpose — it is about the
    gate, and an ungated row has none. This one asks the other half of
    the same question: does the *catalog* tell the truth? A row lists
    its aliases, ``/help`` prints them and the site's command page
    renders them, so ``/сброс`` sitting next to ``/reset`` is a promise
    that typing either one does the same thing.

    Nothing checks that promise structurally otherwise:
    ``test_help_surface`` proves an advertised alias is *registered
    somewhere*, which a token wired to an unrelated handler satisfies
    just fine. A Cyrillic spelling added to the wrong ``Command(...)``
    call — the exact slip #163 could have made three times over — would
    ship silently and answer with somebody else's command.

    Aliases registered nowhere are not this test's business
    (``test_help_surface`` owns that), and a token owned by several
    handlers is normal (chat-type splits, ``magic=F.args`` pairs), so
    the check is an intersection, like the gated one.
    """
    divergent: dict[str, list[str]] = {}
    for entry in COMMAND_ENTRIES:
        key_owners = _real_owners(registered_command_handlers.get(entry.key, set()))
        if not key_owners:
            continue
        for alias in entry.aliases:
            if alias == entry.key:
                continue
            alias_owners = _real_owners(registered_command_handlers.get(alias, set()))
            if not alias_owners or (alias_owners & key_owners):
                continue
            divergent.setdefault(entry.key, []).append(
                f"/{alias} runs {sorted(alias_owners)} but row {entry.key!r} "
                f"runs {sorted(key_owners)}"
            )

    offenders = [
        text
        for key, texts in sorted(divergent.items())
        if key not in _SUBCOMMAND_ROWS
        for text in texts
    ]
    assert not offenders, (
        "a catalog row advertises a spelling that runs a different "
        "command — fix the handler's Command(...) filter, or give the "
        "spelling its own row: " + "; ".join(sorted(offenders))
    )

    stale = sorted(set(_SUBCOMMAND_ROWS) - set(divergent))
    assert not stale, (
        "_SUBCOMMAND_ROWS lists rows whose spellings now share a "
        f"handler; drop them so the guard keeps its teeth: {stale}"
    )
