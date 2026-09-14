"""``/cmdcfg list`` must fit inside Telegram's 4096-character ceiling.

The catalog went out as one message: 73 commands, ~1500 characters as
Telegram measures them, so ~2600 of headroom under the ceiling. But
``/cmdcfg set`` accepts any well-formed key by design
(``_COMMAND_KEY_RE``), so an override pinned onto a mistyped command
name is stored, and ``list`` grows an "outside the catalog" line for it
that never goes away. A few dozen of those at the 64-character key cap
eat the headroom, the reply comes back a 400 the developer never sees,
and the view they lose is the only one that would have told them which
keys to reset.

What these tests pin:

* the ordinary catalog still goes out as a single message — pagination
  must not turn the everyday view into a burst;
* a bloated catalog splits, every page fits when measured the way
  Telegram measures (parsed text, entities free), and no row is lost;
* the page count stays bounded on a pathological override table;
* the footer — which carries ``/cmdcfg reset all``, the only escape from
  that table — survives even when the tail was truncated.
"""

from __future__ import annotations

import re

import pytest

from telegram_invite_bot.core.ranks import COMMAND_CATEGORIES, COMMAND_ENTRIES
from telegram_invite_bot.handlers.rank_admin import _render_cmdcfg_list
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.render import PAGE_MAX, TELEGRAM_TEXT_LIMIT, parsed_length

#: ``<code>61) /warn → 4</code> ✏️`` and its uncataloged ``—)`` twin.
_ROW = re.compile(r"^<code>\S+\) /(?P<cmd>\S+) → (?P<rank>\d)</code>")


def _rows(pages: list[str]) -> list[tuple[str, str]]:
    """Every rendered command row, in order, across all pages."""
    return [
        (m["cmd"], m["rank"])
        for page in pages
        for line in page.splitlines()
        if (m := _ROW.match(line))
    ]


def _phantoms(count: int, *, length: int = 12) -> dict[str, int]:
    """``count`` overrides on keys the catalog never listed.

    Exactly what a developer's typos leave behind: ``set`` stored them,
    nothing ever removes them, and each one owns a line in ``list``.
    """
    stem = "p" * (length - 4)
    return {f"{stem}{i:04d}": 3 for i in range(count)}


def test_the_ordinary_catalog_still_goes_out_as_one_message() -> None:
    """Today's 73 commands fit; splitting them would be a regression."""
    pages = _render_cmdcfg_list({}, "ru", categories=COMMAND_CATEGORIES)

    assert len(pages) == 1
    assert parsed_length(pages[0]) <= TELEGRAM_TEXT_LIMIT
    assert len(_rows(pages)) == len(COMMAND_ENTRIES)
    for category in COMMAND_CATEGORIES:
        assert t(f"h_cmdcfg_cat_{category}", "ru") in pages[0]
    assert t("h_cmdcfg_all_default", "ru") in pages[0]


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_a_catalog_past_the_limit_is_split_and_loses_nothing(lang: str) -> None:
    """60 long phantom keys on top of the catalog is a 6100-character 400."""
    overrides = _phantoms(60, length=64)

    pages = _render_cmdcfg_list(overrides, lang, categories=COMMAND_CATEGORIES)

    assert sum(parsed_length(page) for page in pages) > TELEGRAM_TEXT_LIMIT, (
        "fixture must exceed the ceiling — otherwise this proves nothing"
    )
    assert len(pages) > 1
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages)

    rendered = [cmd for cmd, _ in _rows(pages)]
    # Catalog rows come out grouped by category, not in id order.
    assert sorted(cmd for cmd in rendered if cmd not in overrides) == sorted(
        entry.key for entry in COMMAND_ENTRIES
    )
    assert [cmd for cmd in rendered if cmd in overrides] == sorted(overrides)


def test_a_pathological_override_table_stays_bounded_and_says_so() -> None:
    """400 maximum-length phantom keys is ~30 000 characters."""
    overrides = _phantoms(400, length=64)

    pages = _render_cmdcfg_list(overrides, "ru", categories=COMMAND_CATEGORIES)

    # PAGE_MAX list pages, plus at most one for the footer below.
    assert PAGE_MAX <= len(pages) <= PAGE_MAX + 1
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in pages)

    dropped = len(COMMAND_ENTRIES) + len(overrides) - len(_rows(pages))
    assert dropped > 0
    assert t("h_cmdcfg_list_more", "ru", count=dropped) in "\n".join(pages)


def test_the_reset_all_footer_survives_truncation() -> None:
    """``reset all`` is the only way out of a table like this, so the
    footer cannot be the thing the truncation drops."""
    overrides = _phantoms(400, length=64)

    pages = _render_cmdcfg_list(overrides, "ru", categories=COMMAND_CATEGORIES)

    assert t("h_cmdcfg_changed", "ru", count=len(overrides)) in pages[-1]
    assert t("h_cmdcfg_list_footer", "ru") in pages[-1]


def test_ampersand_keys_are_budgeted_on_the_parsed_length() -> None:
    """``&`` escapes to ``&amp;`` — five characters Telegram counts as
    one. Budgeting on the escaped HTML would split the catalog into
    pages it does not need.

    Stated as a comparison rather than "45 keys still fit one page":
    the absolute form only held while the catalog had the headroom for
    exactly that fixture, and #114 spent it. Two override sets of
    *identical parsed length* must paginate identically no matter how
    far apart their raw forms are — which stays true at any catalog
    size. The keys are not reachable through ``set``; the point is that
    the renderer measures what Telegram measures.
    """
    keys = 8
    ampersands = 60
    plain = {"x" * ampersands + f"{i:03d}": 3 for i in range(keys)}
    escaped = {"&" * ampersands + f"{i:03d}": 3 for i in range(keys)}

    plain_pages = _render_cmdcfg_list(plain, "ru", categories=COMMAND_CATEGORIES)
    escaped_pages = _render_cmdcfg_list(escaped, "ru", categories=COMMAND_CATEGORIES)

    # Each ``&`` costs 4 extra raw characters and 0 parsed ones.
    assert (
        sum(len(page) for page in escaped_pages) - sum(len(page) for page in plain_pages)
        >= keys * ampersands * 4
    ), "fixture must be far heavier as raw HTML — that's the point"
    # The distinguishing assertion: budgeting on the raw form would give
    # the escaped set an extra page the plain set does not get.
    assert len(escaped_pages) == len(plain_pages)
    assert all(parsed_length(page) <= TELEGRAM_TEXT_LIMIT for page in escaped_pages)
