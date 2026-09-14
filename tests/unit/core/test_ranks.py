"""Unit tests for :mod:`telegram_invite_bot.core.ranks` (ranks epic R1).

Pins the verbatim-legacy constants so a later "cleanup" can't silently
drift from the audited bot.py values:

* default matrix cells (bot.py:2611-2712), incl. the quirky-but-real
  facts: rank 1 has NO ``can_pin`` key, ranks 0/-1 have no row at all;
* command catalog defaults (bot.py:42340-42436) — ALL moderation
  commands are rank 2 (incl. ban and unwarn), admin tools rank 5;
* alias resolution and the unknown-command fallback (rank 0).
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from telegram_invite_bot.core.ranks import (
    ALL_PERMISSION_KEYS,
    COMMAND_ALIAS_TO_KEY,
    COMMAND_CATALOG,
    COMMAND_CATEGORIES,
    COMMAND_ENTRIES,
    DEFAULT_RANK_PERMISSIONS,
    KNOWN_PERMISSIONS,
    MANAGE_RANKS_PERMISSION,
    ORDERED_PERMISSION_KEYS,
    PERMISSION_CATEGORIES,
    RankLevel,
    command_entry,
    command_key_for,
    default_min_rank,
    entries_for_id,
    entries_in_category,
    rank_name,
    synonym_aliases,
)
from telegram_invite_bot.i18n import t

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_rank_level_values_match_legacy() -> None:
    assert RankLevel.BANNED == -1
    assert RankLevel.USER == 0
    assert RankLevel.JUNIOR_MOD == 1
    assert RankLevel.MODERATOR == 2
    assert RankLevel.SENIOR_MOD == 3
    assert RankLevel.ADMIN == 4
    assert RankLevel.OWNER == 5
    assert RankLevel.DEVELOPER == 6


def test_from_int_unknown_maps_to_user() -> None:
    assert RankLevel.from_int(42) is RankLevel.USER
    assert RankLevel.from_int(-1) is RankLevel.BANNED
    assert RankLevel.from_int(3) is RankLevel.SENIOR_MOD


def test_default_matrix_has_exactly_ranks_1_to_6() -> None:
    # Legacy settings defaults carry only ranks 1..6; 0 and -1 resolve
    # to {} via the .get fallback (bot.py:6618).
    assert sorted(DEFAULT_RANK_PERMISSIONS) == [1, 2, 3, 4, 5, 6]


def test_default_matrix_spot_cells_verbatim_legacy() -> None:
    m = DEFAULT_RANK_PERMISSIONS
    # Rank 6/5: everything True (15 keys each).
    assert all(m[6].values()) and len(m[6]) == 15
    assert all(m[5].values()) and len(m[5]) == 15
    # Rank 4: settings/broadcast/achievements/bypass off.
    assert m[4]["can_change_settings"] is False
    assert m[4]["can_broadcast"] is False
    assert m[4]["can_manage_achievements"] is False
    assert m[4]["can_bypass_limits"] is False
    assert m[4]["can_manage_mods"] is True
    # Rank 3 may ban; rank 2 may NOT ban but may kick/warn/mute/pin.
    assert m[3]["can_ban"] is True
    assert m[3]["can_remove_warn"] is False
    assert m[2]["can_ban"] is False
    assert m[2]["can_warn"] is True
    assert m[2]["can_kick"] is True
    assert m[2]["can_pin"] is True
    # Rank 1: warn only — and legacy genuinely omits can_pin here.
    assert m[1]["can_warn"] is True
    assert m[1]["can_mute"] is False
    assert "can_pin" not in m[1]
    assert len(m[1]) == 14


def test_permission_vocabulary() -> None:
    # 24 keys from RankPermissions.PERMISSIONS (bot.py:6566-6603) plus
    # the matrix-only spelling can_remove_warn.
    assert len(KNOWN_PERMISSIONS) == 24
    assert "can_warn" in KNOWN_PERMISSIONS
    assert "can_see_hidden" in KNOWN_PERMISSIONS
    assert "can_remove_warn" not in KNOWN_PERMISSIONS
    assert "can_remove_warn" in ALL_PERMISSION_KEYS
    # Every key the default matrix uses is in the validation set.
    for perms in DEFAULT_RANK_PERMISSIONS.values():
        assert set(perms) <= ALL_PERMISSION_KEYS


def test_permission_categories_cover_the_vocabulary_exactly() -> None:
    """Both vocabularies are DERIVED from the category table (RR-4 #45),
    so a key added to one category and forgotten elsewhere is impossible
    — but a key listed in TWO categories would render twice in /perm and
    silently shrink the derived sets."""
    flat = [key for keys in PERMISSION_CATEGORIES.values() for key in keys]
    assert flat == list(ORDERED_PERMISSION_KEYS)
    assert len(flat) == len(set(flat)) == len(ALL_PERMISSION_KEYS) == 25
    assert set(flat) == ALL_PERMISSION_KEYS
    assert ALL_PERMISSION_KEYS - {"can_remove_warn"} == KNOWN_PERMISSIONS
    # Every category is non-empty: an empty one renders a header with no
    # keys under it.
    assert all(keys for keys in PERMISSION_CATEGORIES.values())
    # A grant sits next to its inverse — the pairing is the point of the
    # ordering, and alphabetising would separate can_ban from can_unban.
    for grant, inverse in (("can_warn", "can_unwarn"), ("can_ban", "can_unban")):
        assert ORDERED_PERMISSION_KEYS.index(inverse) - ORDERED_PERMISSION_KEYS.index(grant) == 1


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_permission_category_has_copy(lang: str) -> None:
    """A missing key would render as the raw key string in /perm."""
    for category in PERMISSION_CATEGORIES:
        rendered = t(f"h_rankadm_cat_{category}", lang)
        assert rendered != f"h_rankadm_cat_{category}"
        assert "<b>" in rendered


def test_command_catalog_defaults_verbatim_legacy() -> None:
    # ALL moderation-category commands default to rank 2 in the legacy
    # catalog — including ban and unwarn (bot.py:42401-42411).
    for cmd in (
        "warn",
        "kick",
        "pin",
        "unpin",
        "mute",
        "unmute",
        "ban",
        "unban",
        "unwarn",
        "warnings",
        "clear",
    ):
        assert COMMAND_CATALOG[cmd] == 2, cmd
    for cmd in (
        "dev",
        "admin_help",
        "maintenance",
        "cfg_button",
        "alias",
        "modcfg",
        "perm",
        "cmdcfg",
        "create_check",
        "clearlogs",
        "test_logs",
    ):
        assert COMMAND_CATALOG[cmd] == 5, cmd
    # ``admin`` sat in the list above until it stopped being the
    # developer panel. It is the multi-group admin panel now
    # (``handlers/mygroups.py``), a rank-0 command whose every row is
    # re-scoped to the caller's own groups — see its catalog row.
    assert COMMAND_CATALOG["admin"] == 0
    assert COMMAND_CATALOG["start"] == 0
    assert COMMAND_CATALOG["roulette"] == 0
    # No other ranks appear in the catalog at all.
    assert set(COMMAND_CATALOG.values()) == {0, 2, 5}


def test_command_key_resolution() -> None:
    assert command_key_for("/warn@SomeBot") == "warn"
    assert command_key_for("WARN") == "warn"
    assert command_key_for("кик") == "kick"  # legacy Russian alias
    assert command_key_for("rankperm") == "perm"
    assert command_key_for("kom_balance") == "balance"
    # Unknown commands map to themselves and default to rank 0.
    assert command_key_for("frobnicate") == "frobnicate"
    assert default_min_rank("frobnicate") == 0
    assert default_min_rank("ban") == 2
    assert default_min_rank("perm") == 5
    # The alias map itself is lowercase-keyed.
    assert all(alias == alias.lower() for alias in COMMAND_ALIAS_TO_KEY)


def test_rank_name_reuses_legacy_yaml_keys() -> None:
    assert rank_name(2, "ru") == t("rank_level_2", "ru")
    assert rank_name(6, "en") == t("rank_level_6", "en")
    # In-group display masks DEVELOPER as OWNER (bot.py:6743-6744).
    assert rank_name(6, "ru", in_group=True) == t("rank_level_5", "ru")
    # Unknown/banned levels fall back to the member title (bot.py:6745).
    assert rank_name(-1, "ru") == t("rank_level_0", "ru")
    assert rank_name(99, "en") == t("rank_level_0", "en")


# -- catalog table (RR-4 #40/#41) ---------------------------------------------------
#
# COMMAND_CATALOG and COMMAND_ALIAS_TO_KEY are now *derived* from
# COMMAND_ENTRIES. That kills three-way drift but introduces a new
# failure mode: a duplicated key or alias would be swallowed silently by
# the dict comprehensions instead of raising. These pin it.


def test_catalog_keys_are_unique() -> None:
    keys = [entry.key for entry in COMMAND_ENTRIES]
    assert len(keys) == len(set(keys))
    assert len(COMMAND_CATALOG) == len(COMMAND_ENTRIES)


def test_catalog_aliases_are_unique_across_entries() -> None:
    """A repeated alias would reroute a command to the wrong catalog row
    — and the loser would be whichever entry the table happens to list
    first, which is not a decision anyone made."""
    aliases = [alias for entry in COMMAND_ENTRIES for alias in entry.aliases]
    duplicates = sorted({a for a in aliases if aliases.count(a) > 1})
    assert not duplicates, duplicates
    assert len(COMMAND_ALIAS_TO_KEY) == len(aliases)


def test_every_key_is_callable_as_itself() -> None:
    """``/cmdcfg show <key>`` must always resolve — the key is what the
    list view prints, so it has to be accepted back as input."""
    for entry in COMMAND_ENTRIES:
        assert entry.key in entry.aliases, entry.key
        assert command_key_for(entry.key) == entry.key


def test_subcommands_are_declared_out_of_the_row_they_live_on() -> None:
    """#164: ``subcommands`` is a *view* over ``aliases``, not a second
    list.

    The whole design rests on that: the ``/cmdcfg`` gate keeps reading
    ``aliases`` verbatim, so a subcommand token that drifted out of it
    would be a command the owner's off-switch silently stopped covering
    — the exact hole :data:`COMMAND_ALIAS_TO_KEY` exists to close. The
    reverse drift is milder but still a lie on the page: a declared
    token that no longer belongs to the row would be printed under a
    command it has nothing to do with.
    """
    for entry in COMMAND_ENTRIES:
        for sub in entry.subcommands:
            assert sub.key in sub.aliases, (entry.key, sub.key)
            for token in sub.aliases:
                assert token in entry.aliases, (entry.key, sub.key, token)
                assert command_key_for(token) == entry.key, (entry.key, token)


def test_subcommand_keys_do_not_collide() -> None:
    """Each subcommand key names its own ``h_subcmd_<key>`` copy, and a
    key shared with a catalog row (or with another subcommand) would put
    two different descriptions behind one name."""
    keys = [sub.key for entry in COMMAND_ENTRIES for sub in entry.subcommands]
    assert len(keys) == len(set(keys)), sorted(keys)
    catalog_keys = {entry.key for entry in COMMAND_ENTRIES}
    assert not (set(keys) & catalog_keys), sorted(set(keys) & catalog_keys)


def test_synonym_aliases_drops_the_key_and_every_subcommand() -> None:
    """What the display layers print instead of the raw alias tuple."""
    assert synonym_aliases(_entry("weather")) == ("погода", "kom_weather")
    assert synonym_aliases(_entry("cpc")) == ("rps", "кнб", "knb")
    assert synonym_aliases(_entry("alias")) == ()
    for entry in COMMAND_ENTRIES:
        assert entry.key not in synonym_aliases(entry), entry.key


def _entry(key: str):  # noqa: ANN202 - the row type is an implementation detail here
    entry = command_entry(key)
    assert entry is not None, key
    return entry


def test_aliases_are_plausible_command_names() -> None:
    for entry in COMMAND_ENTRIES:
        for alias in entry.aliases:
            assert alias == alias.lower(), alias
            assert " " not in alias, alias
            assert not alias.startswith("/"), alias


def test_categories_partition_the_catalog() -> None:
    assert {entry.category for entry in COMMAND_ENTRIES} == set(COMMAND_CATEGORIES)
    grouped = [e for cat in COMMAND_CATEGORIES for e in entries_in_category(cat)]
    assert sorted(e.key for e in grouped) == sorted(e.key for e in COMMAND_ENTRIES)
    for category in COMMAND_CATEGORIES:
        rows = entries_in_category(category)
        assert rows, category
        assert list(rows) == sorted(rows, key=lambda e: (e.id, e.key))


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_every_category_has_a_label_in_both_languages(lang: str) -> None:
    for category in COMMAND_CATEGORIES:
        key = f"h_cmdcfg_cat_{category}"
        # t() echoes the key back when it is missing — that is the tell.
        assert t(key, lang) != key, key


def test_catalog_ids_are_unique() -> None:
    """The id is the handle ``/cmdcfg set <id> <rank>`` takes instead of
    a name, so an id shared by two rows is an id no operator can use:
    legacy's plain COMMAND_BY_ID dict silently kept the last row, and
    the port replaced that with an "ambiguous, name it instead"
    refusal. Either way the number was dead. #121 renumbered the
    duplicates; this is the guard that keeps a new row from quietly
    taking a number that is already spoken for."""
    collisions = {
        entry.id: tuple(e.key for e in entries_for_id(entry.id))
        for entry in COMMAND_ENTRIES
        if len(entries_for_id(entry.id)) > 1
    }
    assert not collisions, collisions


def test_legacy_duplicate_ids_resolve_to_the_primary_command() -> None:
    """Legacy reused 3, 16 and 60 (bot.py:42316-42367). Each number
    stays with the row that owns the surrounding run — /faq in basic
    1..9, /currency in economy 10..18, /relations closing social
    50..60 — and the second row of each pair moved to the 178+ band.
    Pinned because these three numbers are the ones an operator may
    have written down while they were ambiguous."""
    assert [e.key for e in entries_for_id(3)] == ["faq"]
    assert [e.key for e in entries_for_id(16)] == ["currency"]
    assert [e.key for e in entries_for_id(60)] == ["relations"]
    assert [e.key for e in entries_for_id(178)] == ["faq2"]
    assert [e.key for e in entries_for_id(179)] == ["lang"]
    assert [e.key for e in entries_for_id(180)] == ["rp_commands"]


def test_entry_lookup_by_key_and_id() -> None:
    ban = command_entry("ban")
    assert ban is not None
    assert (ban.id, ban.category, ban.default_rank) == (67, "moderation", 2)
    assert entries_for_id(67) == (ban,)
    # Unknown-but-configurable commands have no row at all.
    assert command_entry("frobnicate") is None
    assert entries_for_id(4242) == ()


def test_rank_card_stays_open_to_everyone() -> None:
    """#576: ``/rank`` prints the caller's own rank card and nothing else.

    It lives in the ``admin`` category only because the catalog groups it
    next to ``/perm`` and ``/cmdcfg``, and every other row in that category
    defaults to rank 5. ``/rank`` must not: legacy has no ``COMMAND_CATALOG``
    row for it at all (bot.py:42339-42417), so legacy never gated it, and the
    handler itself is ungated on purpose (``rank_admin.handle_rank``). A
    rank-5 default here would have locked ordinary members out of reading
    their own rank -- the same trap as ``/check`` (id 102)."""
    rank = command_entry("rank")
    assert rank is not None
    assert (rank.id, rank.category, rank.default_rank) == (173, "admin", 0)
    # The Russian alias resolves to the same ungated row.
    assert command_key_for("ранг") == "rank"
    assert default_min_rank("rank") == 0


def test_manage_ranks_permission_is_a_deliberate_divergence() -> None:
    """#235: the bang-rank commands gate on ``can_manage_mods``, and the
    legacy key ``can_manage_ranks`` is not what decides.

    Legacy called ``check_permission_and_reply(..., "can_manage_ranks")``
    (bot.py:31399), which routes through ``require_group_moderation``
    (bot.py:7568-7577) and ends in ``perms.get(permission, False)``
    (bot.py:7016). No rank row in the default matrix (bot.py:2611-2712)
    carries that key, so for every non-developer the permission half of
    that gate was always ``False`` and only the ``has_group_admin_rights``
    half could pass — i.e. legacy resolved to "developer, or a TG admin of
    *any* group". The port refuses the TG-admin bypass (narrower) and
    grants the gate at ranks 4-6 (wider).

    Pinned as a literal because swapping the constant is an owner call,
    not a cleanup — see ``docs/DESIGN_RANKS.md`` §2.3.
    """
    assert MANAGE_RANKS_PERMISSION == "can_manage_mods"
    # The legacy key is still part of the ``/perm set`` vocabulary...
    assert "can_manage_ranks" in ALL_PERMISSION_KEYS
    # ...but no rank is born with it, which is what collapsed the legacy
    # gate to the TG-admin bypass in the first place.
    assert all("can_manage_ranks" not in perms for perms in DEFAULT_RANK_PERMISSIONS.values())
    granted = sorted(
        int(rank) for rank, perms in DEFAULT_RANK_PERMISSIONS.items() if perms["can_manage_mods"]
    )
    assert granted == [4, 5, 6]


def _legacy_catalog() -> list[dict[str, Any]]:
    """``COMMAND_CATALOG`` parsed out of ``bot.py`` (bot.py:42339-42417).

    Read rather than imported: ``bot.py`` is the legacy monolith and
    importing it would drag in the whole runtime. ``ast.literal_eval``
    keeps it to the literal, so a hand-edit that made the table
    non-literal fails loudly here instead of quietly skipping.
    """
    lines = (_REPO_ROOT / "bot.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("COMMAND_CATALOG"))
    end = next(i for i in range(start, len(lines)) if lines[i].rstrip() == "]")
    blob = "\n".join(lines[start : end + 1]).split("=", 1)[1].strip()
    rows = ast.literal_eval(blob)
    assert isinstance(rows, list)
    return rows


def test_legacy_catalog_divergence_counts() -> None:
    """#1073: the module docstring's numbers, re-measured rather than trusted.

    Three of them were wrong — 44 changed alias tuples (really 42) and a
    duplicate-id set of ``{2, 3, 16, 60}`` (really ``{3, 16, 60}``; id 2
    is ``help`` at bot.py:42341 and occurs once). A prose count that
    nothing checks is a count that drifts, and this one had already
    drifted far enough to contradict two other docstrings in the same
    file. Asserting it here is the only thing that stops a fourth.
    """
    legacy = _legacy_catalog()
    port = {entry.key: entry for entry in COMMAND_ENTRIES}

    assert len(legacy) == 71
    assert len(COMMAND_ENTRIES) == 149
    assert {row["key"] for row in legacy} <= set(port)

    changed = [row["key"] for row in legacy if tuple(row["commands"]) != port[row["key"]].aliases]
    assert len(changed) == 42

    counts = Counter(row["id"] for row in legacy)
    assert sorted(i for i, n in counts.items() if n > 1) == [3, 16, 60]
    assert counts[2] == 1

    renumbered = {
        row["key"]: port[row["key"]].id for row in legacy if row["id"] != port[row["key"]].id
    }
    assert renumbered == {"faq2": 178, "lang": 179, "rp_commands": 180}

    # The half of the claim that #1073 caught: on id 16 it is the FIRST
    # row that moved, not the second.
    assert [row["key"] for row in legacy if row["id"] == 16] == ["lang", "currency"]
    assert port["currency"].id == 16

    # Ranks and categories are unchanged by the port bar one deliberate
    # exception, pinned exactly so the counts above stay readable as
    # "aliases and ids only" — and so a second rank edit has to say so
    # here rather than slipping in under an ``all(...)``.
    rank_changed = {
        row["key"]: (row["default_rank"], port[row["key"]].default_rank)
        for row in legacy
        if row["default_rank"] != port[row["key"]].default_rank
    }
    assert rank_changed == {"admin": (5, 0)}
    assert all(row["category"] == port[row["key"]].category for row in legacy)
