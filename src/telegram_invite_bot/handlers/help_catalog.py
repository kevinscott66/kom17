"""``/help`` catalog renderer — RR-6 #62/#63.

The monolith→split port left ``/help`` as a **seven-line card**: a
title, an intro, and five hand-picked bullets (``/start``, ``/profile``,
``/balance``, ``/weather``, ``/help``). Legacy rendered the *whole*
catalog grouped by category (``build_participant_help_text``,
bot.py:42967) and, for the owner, the same catalog annotated with each
command's effective minimum rank (``build_owner_help_text``). Over a
hundred working commands were reachable and undiscoverable at the same
time — :func:`visible_keys` yields 105 for an ordinary user, 130 for
staff and 144 for a developer — the single largest richness regression
in the bot, because a command nobody can find may as well not be
ported. (An earlier version of this paragraph said "sixty-five"; that
number was stale long before it was noticed, #717.)

This module owns the *pure* rendering half so it stays unit-testable and
so ``main_menu``'s help tap and ``/help`` render byte-identical cards.

Design decisions worth keeping:

* **Only live commands are advertised.** The catalog in
  :mod:`telegram_invite_bot.core.ranks` still lists five commands the
  new pipeline never registered (:data:`HELP_HIDDEN_KEYS`). Printing
  them would be a promise the bot breaks the moment it's taken up.
  ``tests/regression`` asserts the advertised set equals the
  *router-registered* set, so porting one of the five trips the test
  instead of quietly leaving it hidden. (It was six until ``city`` was
  registered in RR-6 #74 — see the comment on the constant.)
* **Plain ``/command``, not ``<code>/command</code>``.** Legacy wrapped
  each name in ``<code>`` (bot.py:42934) for tap-to-copy; that also
  suppresses Telegram's bot-command entity, so the user had to copy,
  then paste, then send. Left unformatted, Telegram auto-links every
  name and one tap runs it. Deliberate divergence — it's the whole
  interaction, not a cosmetic.
* **Role-aware, one command.** Legacy split participant/owner help
  across ``/help`` and ``/owner_help``; in the new pipeline
  ``/owner_help`` is already taken by the developer *diagnostics* index
  (``handlers/admin/help.py``, ~110 ``/admin_*`` entries), which is a
  different surface and must not be overloaded. So the rank-annotated
  owner reference (#63) lives here, gated on role: a group admin also
  sees Moderation, a developer additionally sees Admin — both with the
  ``[от N⭐]`` / ``[выкл]`` effective-rank notes legacy showed.
* **Pagination, never truncation.** ``/top`` may drop rows (a
  leaderboard tail is expendable); a help card may not — a silently
  clipped catalog is exactly the regression being fixed. Pages split on
  category boundaries, and a category too large for one page splits by
  row with its heading repeated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from telegram_invite_bot.core.ranks import (
    RankLevel,
    command_entry,
    default_min_rank,
    entries_in_category,
)
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.html import TELEGRAM_TEXT_LIMIT, visible_len

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


#: Catalog keys the new pipeline does **not** register, and therefore
#: must not advertise. Verified by router introspection, not by reading
#: source — see ``tests/regression/test_help_surface.py``, which fails
#: the moment one of these becomes live (port it, drop it from here).
#:
#: * ``dev``       — legacy's developer panel; superseded by ``/admin_*``.
#: * ``maintenance``, ``cfg_button``, ``clearlogs``, ``test_logs`` —
#:   legacy ops commands with no new-pipeline equivalent yet.
#:
#: ``city`` left this set in RR-6 #74, the moment ``handlers/city.py``
#: was registered — which is the mechanism working as designed.
HELP_HIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {"dev", "maintenance", "cfg_button", "clearlogs", "test_logs"}
)

#: Categories every user sees, in reading order: what you do first, then
#: what you spend, then what you play, then who you are, then numbers,
#: then other people. Legacy's ``HELP_CATEGORY_ORDER`` (bot.py:42906)
#: minus the staff tail.
USER_CATEGORIES: Final[tuple[str, ...]] = (
    "basic",
    "economy",
    "games",
    "profile",
    "stats",
    "social",
)

#: Appended for a confirmed chat admin (legacy showed the moderation
#: block only to group owners/admins, bot.py:42969).
STAFF_CATEGORIES: Final[tuple[str, ...]] = ("moderation",)

#: Appended for a bot developer — legacy's ``build_owner_help_text``.
OWNER_CATEGORIES: Final[tuple[str, ...]] = ("moderation", "admin")

#: No-slash shortcuts worth advertising, per language. Every token here
#: MUST resolve in ``middlewares.text_alias._ALIAS_MAP`` — a guard test
#: asserts it, because a shortcut that only exists in the help card is
#: worse than no shortcut at all.
NO_SLASH_TRIGGERS: Final[dict[str, tuple[str, ...]]] = {
    "ru": ("меню", "профиль", "баланс", "топ", "бонус", "погода"),
    "en": ("menu", "profile", "balance", "top", "daily", "weather"),
}

#: ``/kom_``-prefixed duplicates for groups running several bots — the
#: perk Iris-style bots don't have (there, two bots both answer ``/top``
#: and the chat gets two cards). Only names actually registered may be
#: listed; the same guard test checks them against the live router.
KOM_PREFIX_HINTS: Final[tuple[str, ...]] = ("kom_help", "kom_top", "kom_shop", "kom_faq")

#: Rank at/above which legacy printed ``[выкл]`` instead of a threshold:
#: nobody but the developer can reach it, so it reads as "disabled"
#: rather than "restricted" (bot.py:42955).
_DISABLED_MIN_RANK: Final[int] = RankLevel.DEVELOPER

# Room reserved for the ``📄 1/2`` page marker appended after paging is
# known. Counted up-front so adding the marker can never push a page
# that *just* fit back over the cap.
_PAGE_MARKER_BUDGET: Final[int] = 24


def categories_for(*, is_staff: bool, is_developer: bool) -> tuple[str, ...]:
    """Category order for one viewer.

    ``is_developer`` wins over ``is_staff`` — a developer in a group
    they don't administer still gets the full reference, matching
    legacy, where ``build_owner_help_text`` was keyed on bot ownership
    and not on chat membership.
    """
    if is_developer:
        return USER_CATEGORIES + OWNER_CATEGORIES
    if is_staff:
        return USER_CATEGORIES + STAFF_CATEGORIES
    return USER_CATEGORIES


def visible_keys(categories: Sequence[str]) -> tuple[str, ...]:
    """Catalog keys ``categories`` advertise, in render order.

    Public because the regression guard compares exactly this against
    the router's registered vocabulary.
    """
    return tuple(
        entry.key
        for category in categories
        for entry in entries_in_category(category)
        if entry.key not in HELP_HIDDEN_KEYS
    )


def rank_note(min_rank: int, lang: str) -> str:
    """Legacy's ``[от N⭐]`` / ``[выкл]`` suffix; empty for rank 0.

    Rendered only in the staff/developer views: a plain user seeing
    ``[от 2⭐]`` next to a command they can't run learns nothing except
    that the bot is withholding something.
    """
    if min_rank >= _DISABLED_MIN_RANK:
        return t("h_help_rank_off", lang)
    if min_rank > 0:
        return t("h_help_rank_from", lang, rank=min_rank)
    return ""


def _command_line(key: str, lang: str, ranks: Mapping[str, int] | None) -> str:
    """One ``• /cmd — description [rank note]`` row, plus any
    subcommands of that row indented under it.

    The description key is ``h_cmd_<key>``; :func:`t` returns the raw key
    when a translation is missing, which the i18n convergence tests turn
    into a hard failure rather than a card with ``h_cmd_flip`` in it.

    Three rows carry a command that is not a spelling of them —
    ``/forecast`` under ``/weather``, ``/cpc_cancel`` under ``/cpc``,
    ``/aliases`` under ``/alias`` (see
    :class:`~telegram_invite_bot.core.ranks.SubCommand`). ``/help`` never
    printed aliases and so never printed those either, which left the
    multi-day forecast reachable only by someone who already knew it
    existed. They get a line, indented, because they belong to the row
    above and share its rank note rather than carrying one of their own.
    """
    line = f"• /{key} — {t(f'h_cmd_{key}', lang)}"
    if ranks is not None:
        note = rank_note(ranks.get(key, default_min_rank(key)), lang)
        if note:
            line = f"{line} {note}"
    entry = command_entry(key)
    if entry is None or not entry.subcommands:
        return line
    subs = (f"   ↳ /{sub.key} — {t(f'h_subcmd_{sub.key}', lang)}" for sub in entry.subcommands)
    return "\n".join([line, *subs])


def _category_block(category: str, lang: str, ranks: Mapping[str, int] | None) -> list[str]:
    """Heading + rows for one category; empty when everything is hidden."""
    rows = [
        _command_line(entry.key, lang, ranks)
        for entry in entries_in_category(category)
        if entry.key not in HELP_HIDDEN_KEYS
    ]
    if not rows:
        return []
    # Category labels are shared with ``/cmdcfg list`` — one wording for
    # "what is Moderation" across every surface that groups commands.
    return [t(f"h_cmdcfg_cat_{category}", lang), *rows]


def _tail_block(lang: str, *, has_button: bool) -> list[str]:
    lines = [
        t("h_help_noslash", lang, triggers=" · ".join(NO_SLASH_TRIGGERS.get(lang, ()))),
        t("h_help_kom", lang, commands=" · ".join(f"/{name}" for name in KOM_PREFIX_HINTS)),
    ]
    if has_button:
        lines.append(t("h_help_footer_btn", lang))
    return lines


def _paginate(blocks: list[list[str]], limit: int) -> list[str]:
    """Pack ``blocks`` into as few pages as fit under ``limit``.

    A block is kept whole when it can be; one that cannot fit alone is
    split by row with its first line (the heading) repeated, so a page
    never opens with orphaned bullets under no heading. Blank lines
    between blocks are added here rather than baked into the blocks so
    a page can't start or end with one.
    """
    pages: list[str] = []
    current: list[list[str]] = []
    used = 0

    def flush() -> None:
        nonlocal current, used
        if current:
            pages.append("\n\n".join("\n".join(block) for block in current))
        current = []
        used = 0

    def cost(block: list[str]) -> int:
        # +1 per newline inside the block, +2 for the blank-line join.
        return sum(visible_len(line) + 1 for line in block) + 2

    for block in blocks:
        block_cost = cost(block)
        if used + block_cost > limit and current:
            flush()
        if block_cost <= limit:
            current.append(block)
            used += block_cost
            continue
        # Oversized single block — split by row, repeating the heading.
        heading, *rows = block
        chunk = [heading]
        chunk_cost = visible_len(heading) + 3
        for row in rows:
            row_cost = visible_len(row) + 1
            if chunk_cost + row_cost > limit and len(chunk) > 1:
                current.append(chunk)
                flush()
                chunk = [heading]
                chunk_cost = visible_len(heading) + 3
            chunk.append(row)
            chunk_cost += row_cost
        if len(chunk) > 1:
            current.append(chunk)
            used = chunk_cost

    flush()
    return pages


def render_help_pages(
    lang: str,
    *,
    is_staff: bool = False,
    is_developer: bool = False,
    ranks: Mapping[str, int] | None = None,
    has_button: bool = False,
) -> list[str]:
    """Full help card as one or more send-ready HTML pages.

    ``ranks`` is the effective ``{command_key: min_rank}`` map (DB
    overrides over catalog defaults). Pass ``None`` — the default — for
    the plain-user view; passing it is what turns on the ``[от N⭐]``
    annotations, so a caller cannot accidentally leak the moderation
    thresholds into a member's card without also asking for them.
    """
    categories = categories_for(is_staff=is_staff, is_developer=is_developer)
    header = [t("h_help_catalog_title", lang), "", t("h_help_catalog_intro", lang)]
    blocks: list[list[str]] = [header]
    blocks += [block for block in (_category_block(c, lang, ranks) for c in categories) if block]
    blocks.append(_tail_block(lang, has_button=has_button))

    pages = _paginate(blocks, TELEGRAM_TEXT_LIMIT - _PAGE_MARKER_BUDGET)
    if len(pages) < 2:
        return pages
    total = len(pages)
    return [
        f"{page}\n\n{t('h_help_page', lang, page=index, total=total)}"
        for index, page in enumerate(pages, start=1)
    ]
