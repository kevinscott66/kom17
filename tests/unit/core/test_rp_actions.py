"""Unit tests for the RP-action parser + spec (FEAT-RP).

The parser is the load-bearing no-chatter-theft gate: only a prefixed
verb (or a bare sex-verb) is an RP action; everything else returns
``None`` so ordinary group chatter flows through untouched. These tests
pin that contract plus the prefix-stripping, multi-word matching, and
EN/RU twin coverage.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.core.couple_activities import HISTORY_ONLY_ICONS
from telegram_invite_bot.core.rp_actions import (
    RP_18_MIN_LEVEL,
    RP_ACTION_SPEC,
    RP_UNIVERSAL,
    TRIGGERS_BY_NAME,
    parse_rp_trigger,
    rp_activity_key,
)


def test_simple_ru_prefix_match() -> None:
    parsed = parse_rp_trigger(".обнять")
    assert parsed == ("hug", 2, 10, "hug")


def test_en_twin_matches_same_tuple() -> None:
    assert parse_rp_trigger(".hug") == ("hug", 2, 10, "hug")


@pytest.mark.parametrize("prefix", [".", "?", "!", "! ", "бот ", "ком ", "ии "])
def test_all_prefixes_strip(prefix: str) -> None:
    assert parse_rp_trigger(f"{prefix}обнять") == ("hug", 2, 10, "hug")


def test_prefix_case_insensitive() -> None:
    assert parse_rp_trigger("БОТ обнять") == ("hug", 2, 10, "hug")


def test_multiword_trigger_full_phrase() -> None:
    assert parse_rp_trigger(".пожать руку") == ("handshake", 1, 3, "handshake")
    assert parse_rp_trigger(".романтический ужин") == ("dinner", 6, 30, "dinner")
    assert parse_rp_trigger(".high five") == ("highfive", 1, 5, "highfive")


def test_first_word_fallback_with_trailing_text() -> None:
    # First two words don't match a spec; falls back to the single word.
    assert parse_rp_trigger(".обнять крепко") == ("hug", 2, 10, "hug")


def test_bare_sex_verb_without_prefix() -> None:
    assert parse_rp_trigger("выебать") == ("sex", 6, 0, "sex")
    assert parse_rp_trigger("трахнуть") == ("sex", 6, 0, "sex")


def test_plain_chatter_returns_none() -> None:
    assert parse_rp_trigger("обнять") is None  # no prefix, not a bare sex-verb
    assert parse_rp_trigger("привет всем") is None
    assert parse_rp_trigger("") is None
    assert parse_rp_trigger(None) is None


def test_prefixed_nonverb_returns_none() -> None:
    # A prefix but no matching verb → not an RP action (no theft).
    assert parse_rp_trigger(".просто текст") is None
    assert parse_rp_trigger("бот как дела") is None


def test_sex_xp_is_zero_and_universal() -> None:
    assert RP_ACTION_SPEC["выебать"][1] == 0
    assert "sex" in RP_UNIVERSAL


def test_relationship_only_actions_not_universal() -> None:
    rel_only = ("dinner", "flowers", "confess", "ring", "propose", "engagement", "wedding", "photo")
    for name in rel_only:
        assert name not in RP_UNIVERSAL


def test_rp18_min_level_constant() -> None:
    assert RP_18_MIN_LEVEL == 5


def test_rp_activity_key_prefixes_the_action_name() -> None:
    # Legacy's fourth RP_ACTION_SPEC element (bot.py:22120-22185) was
    # ``rp_<name>`` for all 26 actions without exception, which is why
    # the port derives it instead of carrying a hand-copied duplicate.
    assert rp_activity_key("hug") == "rp_hug"
    assert rp_activity_key("tickle") == "rp_tickle"


def test_every_rp_key_can_be_named_by_the_history_view() -> None:
    """#231 writes these keys; #232 is what renders them.

    The pair's joint-activity history names a row by looking the key
    up in three tables, the last of which is
    :data:`HISTORY_ONLY_ICONS`. An RP key missing from it renders as
    "—" — the row still happened, but the action is unreadable. This
    is the join between the two tickets, and it is a real guarantee
    rather than a tautology: the two catalogs live in different
    modules and neither is derived from the other.
    """
    missing = {rp_activity_key(name) for name in TRIGGERS_BY_NAME} - set(HISTORY_ONLY_ICONS)
    assert missing == set()


def test_legacy_two_word_dinner_trigger_still_resolves() -> None:
    """``romantic dinner`` is legacy's EN twin (bot.py:22154).

    The port shipped a bare ``dinner`` in its place, so anyone typing
    the wording the bot has answered to for years fell through to
    ``None`` — the resolver matches whole triggers, and neither the
    two-word pass nor the one-word pass can reach a ``dinner`` entry
    from ``romantic dinner`` (#504). Both spellings must work: the
    legacy one because it is the contract, the short one because it
    was advertised once it shipped.
    """
    assert parse_rp_trigger(".romantic dinner @someone") == ("dinner", 6, 30, "dinner")
    assert parse_rp_trigger(".dinner @someone") == ("dinner", 6, 30, "dinner")
