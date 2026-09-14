"""``t(f"h_top_{mode}_row", ...)`` — the half a literal scan cannot see.

:mod:`tests.regression.test_i18n_placeholders` covers ``t("literal")``.
Every other call site builds its key at runtime, and
``tests/unit/i18n/test_handler_keys.py`` says so out loud: its scan
"skips any call whose first arg is not a plain string ``Constant``",
with the families "covered by their own handler/e2e tests". Several are
not — ``rel_rp_done_*``, ``h_pvp_side_*``, ``h_vset_lang_*`` and
``h_top_*`` had no completeness check anywhere.

That matters because a family is open-ended by construction: the domain
is a catalog, and catalogs grow. Add a fourth ``/top`` mode, a fifth VIP
duration, a new RP verb, a new permission category — the handler asks
for a key nobody wrote, and i18n answers with the key itself. The user
reads ``h_top_kills_header``; nothing raises and nothing is logged.

So each family below pairs its key template with the domain taken from
the code's own source of truth — never a hand-typed list, which would
go stale in exactly the case the test exists to catch — and asserts:

* every member exists in BOTH catalogues;
* every member's ``{placeholder}`` set is covered by the kwargs the
  f-string call site actually passes, read off the same AST.

The second check is what makes a NEW member safe rather than merely
present: a template is free to use fewer placeholders than the call site
supplies (the 180-day VIP blurb skips ``{msg_bonus}``), never more.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from telegram_invite_bot.core.couple_activities import (
    HISTORY_ONLY_ICONS,
    MARRIAGE_BY_KEY,
    RELATIONSHIP_BY_KEY,
)
from telegram_invite_bot.core.ranks import (
    COMMAND_ENTRIES,
    PERMISSION_CATEGORIES,
    RankLevel,
)
from telegram_invite_bot.core.rp_actions import RP_ACTION_SPEC, RP_UNIVERSAL
from telegram_invite_bot.handlers.help_catalog import HELP_HIDDEN_KEYS
from telegram_invite_bot.keyboards.builders.voice_settings import _LANGUAGES, _TARGETS
from telegram_invite_bot.services.inventory_use_planner import VIP_NAME_DURATIONS

pytestmark = pytest.mark.integration

_SRC = Path(__file__).resolve().parents[2] / "src/telegram_invite_bot"
_DATA = _SRC / "i18n/data"

_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}")

_COMMAND_KEYS = sorted({e.key for e in COMMAND_ENTRIES})
_SUBCOMMAND_KEYS = sorted({s.key for e in COMMAND_ENTRIES for s in e.subcommands})
_CATEGORIES = sorted({e.category for e in COMMAND_ENTRIES})
_RP_NAMES = sorted({name for _, _, name in RP_ACTION_SPEC.values()})
_ACTIVITY_KEYS = sorted(set(MARRIAGE_BY_KEY) | set(RELATIONSHIP_BY_KEY))
#: Names — but NOT stories — also exist for the history-only keys (#232):
#: the RP actions and the retired catalog keys the log still references.
#: They have no button and no confirmation card, so ``h_couple_act_done_``
#: deliberately stays on the catalog set.
_ACTIVITY_NAME_KEYS = sorted(set(_ACTIVITY_KEYS) | set(HISTORY_ONLY_ICONS))
_PLAN_DAYS = sorted({str(days) for days in VIP_NAME_DURATIONS.values()})

#: ``template -> domain``. The template is spelled exactly as the
#: f-string reconstructs (``{}`` where the value goes), which is how a
#: family is matched to its call site — see :func:`_call_sites`.
_FAMILIES: dict[str, list[str]] = {
    # The five commands with no description are precisely
    # ``HELP_HIDDEN_KEYS``: developer-only surfaces neither /help nor the
    # site prints. tests/regression/test_help_surface.py already fails if
    # one is un-hidden without writing its copy, so subtract them here
    # rather than duplicate that argument.
    "h_cmd_{}": [key for key in _COMMAND_KEYS if key not in HELP_HIDDEN_KEYS],
    # Subcommands sit on someone else's row, so they are never in
    # HELP_HIDDEN_KEYS and every one of them is printed.
    "h_subcmd_{}": _SUBCOMMAND_KEYS,
    "h_cmdcfg_cat_{}": _CATEGORIES,
    "h_rankadm_cat_{}": sorted(PERMISSION_CATEGORIES),
    "rank_level_{}": [str(level) for level in range(int(RankLevel.DEVELOPER) + 1)],
    "h_top_{}_empty": ["games", "wins", "streak"],
    "h_top_{}_header": ["games", "wins", "streak"],
    "h_top_{}_row": ["games", "wins", "streak"],
    "h_start_greet_{}": ["morning", "day", "evening", "night"],
    "h_pvp_side_{}": ["heads", "tails"],
    "h_couple_act_name_{}": _ACTIVITY_NAME_KEYS,
    "h_couple_act_done_{}": _ACTIVITY_KEYS,
    "rel_rp_done_{}": _RP_NAMES,
    "rel_rp_done_{}_general": sorted(RP_UNIVERSAL),
    "h_vip_plan_name_{}": _PLAN_DAYS,
    "h_vip_plan_desc_{}": _PLAN_DAYS,
    "h_vset_target_{}": sorted(_TARGETS),
    "h_vset_lang_{}": sorted(_LANGUAGES),
    "rel_activity_done_effect_{}": ["d", "dh", "h"],
}

#: Families whose key is assembled by a helper, not by an f-string at
#: the ``t()`` call (``effect_template_key`` returns one of three
#: literals). Nothing to match a call site against, so only the
#: existence half applies.
_NO_FSTRING_SITE = frozenset({"rel_activity_done_effect_{}"})

#: Families whose braces are filled by the CALLER with ``str.replace``
#: instead of by ``t()``. ``handlers/couple_activities`` does this
#: deliberately: ``{actor}``/``{partner}`` are rendered mentions built
#: from attacker-controlled display names, and a name containing a brace
#: would raise inside ``format_map`` — or smuggle a placeholder of its
#: own into the template.
_MANUAL_SUBSTITUTION = frozenset({"h_couple_act_done_{}"})


def _catalogue(lang: str) -> dict[str, str]:
    raw = yaml.safe_load((_DATA / f"{lang}.yaml").read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in raw.items()}


def _pattern(node: ast.JoinedStr) -> str | None:
    """Reconstruct ``f"h_top_{mode}_row"`` as ``"h_top_{}_row"``.

    ``None`` when a segment is not a plain string constant — nothing we
    can line up with a declared family.
    """
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.FormattedValue):
            parts.append("{}")
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            return None
    return "".join(parts)


def _call_sites() -> dict[str, list[tuple[str, set[str]]]]:
    """``pattern -> [(where, supplied kwargs), …]`` for every f-string key."""
    sites: dict[str, list[tuple[str, set[str]]]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "t" or not node.args:
                continue
            key_node = node.args[0]
            if not isinstance(key_node, ast.JoinedStr):
                continue
            pattern = _pattern(key_node)
            if pattern is None:
                continue
            where = f"{path.relative_to(_SRC)}:{node.lineno}"
            supplied = {kw.arg for kw in node.keywords if kw.arg}
            sites.setdefault(pattern, []).append((where, supplied))
    return sites


@pytest.fixture(scope="module")
def sites() -> dict[str, list[tuple[str, set[str]]]]:
    return _call_sites()


def _members(template: str) -> Iterator[tuple[str, str]]:
    for value in _FAMILIES[template]:
        yield value, template.format(value)


def test_every_family_member_exists_in_both_languages() -> None:
    ru, en = _catalogue("ru"), _catalogue("en")
    gaps = [
        f"{key}  (ru={key in ru} en={key in en})"
        for template in _FAMILIES
        for _value, key in _members(template)
        if not (key in ru and key in en)
    ]
    assert not gaps, (
        "a handler builds these keys but no catalogue has them; the user "
        "reads the raw key:\n" + "\n".join(gaps)
    )


def test_every_family_member_fits_the_kwargs_its_call_site_passes(
    sites: dict[str, list[tuple[str, set[str]]]],
) -> None:
    ru, en = _catalogue("ru"), _catalogue("en")
    gaps: list[str] = []
    for template in _FAMILIES:
        if template in _NO_FSTRING_SITE or template in _MANUAL_SUBSTITUTION:
            continue
        supplied: set[str] = set()
        for _where, kwargs in sites.get(template, []):
            supplied |= kwargs
        for _value, key in _members(template):
            needed = set(_PLACEHOLDER.findall(ru.get(key, ""))) | set(
                _PLACEHOLDER.findall(en.get(key, ""))
            )
            missing = needed - supplied
            if missing:
                gaps.append(f"{key} -> {sorted(missing)} (site passes {sorted(supplied)})")
    assert not gaps, (
        "these family members want a placeholder no call site supplies; "
        "_SafeFormat renders it literally:\n" + "\n".join(gaps)
    )


def test_every_declared_family_matches_a_real_call_site(
    sites: dict[str, list[tuple[str, set[str]]]],
) -> None:
    """Guard the guard, half one: the family table is only worth
    anything while it describes keys the code still builds.

    A renamed template would leave both checks above passing over a
    family nothing asks for any more, and the REAL family — the renamed
    one — unguarded. Matching each declaration to an f-string in the
    tree is what keeps the table honest.
    """
    orphans = [
        template
        for template in _FAMILIES
        if template not in _NO_FSTRING_SITE and template not in sites
    ]
    assert not orphans, (
        "declared families with no f-string t() call site left in src/ — "
        f"renamed or deleted?: {orphans}"
    )


def test_every_domain_is_non_empty() -> None:
    """Guard the guard, half two: a domain that came back empty checks
    nothing, and the imports it is derived from are exactly the kind of
    module constant a refactor renames."""
    empty = [template for template, domain in _FAMILIES.items() if not domain]
    assert not empty, empty
    total = sum(len(domain) for domain in _FAMILIES.values())
    assert total >= 200, total


def test_the_exemptions_still_describe_the_code(
    sites: dict[str, list[tuple[str, set[str]]]],
) -> None:
    """Both exemptions are claims about how a call site behaves, and a
    stale one is a hole held open for nothing.

    ``_NO_FSTRING_SITE`` claims no f-string builds the key; if one
    appears, the family can be checked like every other. ``_MANUAL_
    SUBSTITUTION`` claims the call site passes NO kwargs and fills the
    braces itself; the moment it passes some, the ordinary check applies
    and the exemption is hiding whatever it does not cover.
    """
    contradictions = [
        f"{template}: exempt as no-f-string, but built at {sites[template][0][0]}"
        for template in _NO_FSTRING_SITE
        if template in sites
    ]
    contradictions += [
        f"{template}: exempt as manually substituted, but {where} passes {sorted(kwargs)}"
        for template in _MANUAL_SUBSTITUTION
        for where, kwargs in sites.get(template, [])
        if kwargs
    ]
    assert not contradictions, "\n".join(contradictions)
