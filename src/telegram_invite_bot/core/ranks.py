"""Rank system constants — levels, default permission matrix, command catalog.

Single in-code source of truth for the ranks epic (DESIGN_RANKS.md §2.1).
Most of what follows is a verbatim port of legacy data — but
:data:`COMMAND_ENTRIES` is *not*, and used to be described here as if it
were (#711). Read each bullet's own claim rather than the heading:

* :class:`RankLevel` — legacy ``RankLevel`` enum (bot.py:6537-6549).
* :data:`PERMISSION_CATEGORIES` — the permission vocabulary as ONE
  grouped table (RR-4 #45); :data:`KNOWN_PERMISSIONS` (legacy's
  ``RankPermissions.PERMISSIONS``, bot.py:6566-6603, 24 keys) and
  :data:`ALL_PERMISSION_KEYS` are derived from it and verified
  byte-identical to the literals they replaced.
* :data:`DEFAULT_RANK_PERMISSIONS` — the default matrix shipped in
  legacy ``settings.json`` defaults (bot.py:2611-2712). Ranks ``0`` and
  ``-1`` are intentionally ABSENT there; legacy resolves them to ``{}``
  (``rank_permissions.get(str(level.value), {})``, bot.py:6618), i.e.
  every permission False. Rank ``1`` genuinely lacks the ``can_pin``
  key in legacy — preserved as-is, the lookup default (False) covers it.
* :data:`COMMAND_ENTRIES` — the command catalog itself: numeric id,
  key, category, aliases and default minimum rank per row. Legacy's
  ``COMMAND_CATALOG`` (bot.py:42339-42417) is its *seed*, not its
  content: legacy has 71 rows, this table has 149 (the 71 plus 78 rows
  for commands legacy never had). Of the 71 shared keys, 42 carry a
  changed alias tuple and 3 are renumbered, because legacy reused ids
  ``{3, 16, 60}`` across rows and a duplicate id cannot survive a
  keyed table — ``faq2`` 3→178, ``lang`` 16→179, ``rp_commands``
  60→180. (#1073: this line used to say 44 and ``{2, 3, 16, 60}``. Both
  are now re-measured from the parsed legacy table by
  :func:`test_legacy_catalog_divergence_counts`; id ``2`` — ``help``,
  bot.py:42341 — occurs exactly once, which is what the two docstrings
  below had always said.) Deliberate deviations are marked inline with the ticket that
  decided them (``#501``, ``#115``, ``#116``, ``#121``, the
  ``create_check``/``check`` split, ``rank``); anything unmarked does
  match legacy. Every
  moderation-category command defaults to rank 2 and every
  admin-category command to rank 5 there — including ``ban`` (2) and
  ``unwarn`` (2); verified against the actual catalog rows, not
  folklore. :data:`COMMAND_CATALOG` (key→rank) and
  :data:`COMMAND_ALIAS_TO_KEY` are *derived* from it, so the three
  views cannot drift out of sync the way three hand-kept dicts would.
* :data:`COMMAND_CATEGORIES` — the eight category keys in legacy's
  render order (bot.py:42321-42330). Labels are yaml-side
  (``h_cmdcfg_cat_<key>``) so ``/cmdcfg list`` translates.

DB-backed overrides (``rank_permissions`` / ``command_rank_overrides``
tables in moderation.db) overlay these defaults — see
:mod:`telegram_invite_bot.repositories.rank_repo`.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final, NamedTuple

from telegram_invite_bot.i18n import t


class RankLevel(IntEnum):
    """Global per-user rank levels (legacy bot.py:6537-6549)."""

    BANNED = -1
    USER = 0
    JUNIOR_MOD = 1
    MODERATOR = 2
    SENIOR_MOD = 3
    ADMIN = 4
    OWNER = 5
    DEVELOPER = 6

    @classmethod
    def from_int(cls, value: int) -> RankLevel:
        """Legacy ``RankLevel.from_int`` — unknown values map to USER."""
        try:
            return cls(value)
        except ValueError:
            return cls.USER


#: Permission key gating rank WRITES. **DELIBERATE DIVERGENCE — this is
#: not a parity port; read both halves before changing it.**
#:
#: Legacy gated the bang-commands on ``require_group_moderation(message,
#: "can_manage_ranks")`` (bot.py:31399), which is TG-admin-of-this-group
#: **OR** a rank grant (bot.py:7568-7577). The rank half of that OR is
#: dead code: the default matrix (bot.py:2611-2712) never defines
#: ``can_manage_ranks`` at any level, and the lookup ends in
#: ``perms.get(permission, False)`` (bot.py:7016), so it resolved False
#: for every non-developer. ``can_manage_ranks`` exists in legacy only
#: as display vocabulary (bot.py:6580, bot.py:28582) — the three hits
#: above are its only occurrences in 45k lines.
#:
#: So legacy's effective answer was "developer, or any TG admin of any
#: group". The port answers "developer, or ``can_manage_mods`` (ranks
#: 4/5/6, bot.py:2619/2636/2653)" — see
#: :meth:`services.rank_service.RankService.may_manage_ranks`, which
#: refuses the TG-admin bypass outright. That is two changes at once:
#: (1) it narrows sharply — a group's own TG admins can no longer mint
#: global ranks, which is the point, since ranks are global and group
#: adminship is not; (2) it widens slightly — bot ranks 4/5/6 gain a
#: write legacy never actually granted them.
#:
#: The widening is the debatable half. It is currently accepted because
#: the port added a target guard legacy lacked (``handlers.rank_self``
#: refuses any level >= the actor's own; legacy bot.py:31415-31417
#: guarded only developers). Switching this to ``"can_manage_ranks"``
#: would restore strict legacy parity (developer-only in practice) at
#: the cost of that widening — an owner call, not a cleanup.
#:
#: NOTE: this key also gates the ``/groupadmin`` staff panel
#: (``handlers.groupadmin``), so changing it changes who sees a
#: write-enabled panel, not just who may write ranks.
MANAGE_RANKS_PERMISSION: Final[str] = "can_manage_mods"

#: Lowest/highest rank a setter may assign. Legacy ``set_user_rank``
#: rejects ``level < 0 or level > 6`` (bot.py:6666-6668) — BANNED (-1)
#: exists as a level but is not assignable through the rank setter.
MIN_SETTABLE_RANK: Final[int] = 0
MAX_SETTABLE_RANK: Final[int] = 6


def rank_name(level: int, lang: str, *, in_group: bool = False) -> str:
    """Localized rank title, reusing the legacy-parity yaml keys.

    ``rank_level_0..6`` already exist in ``i18n/data/*.yaml`` (legacy
    ``rank_names``, bot.py:2602-2610). Legacy ``get_rank_name``
    (bot.py:6732-6745) falls back to the rank-0 title for unknown
    levels (incl. BANNED, which has no name in legacy) and, when
    rendering inside a group, masks DEVELOPER as OWNER so the bot
    owner isn't advertised to group members.
    """
    if in_group and level == RankLevel.DEVELOPER:
        level = RankLevel.OWNER
    if level < 0 or level > RankLevel.DEVELOPER:
        level = RankLevel.USER
    return t(f"rank_level_{level}", lang)


#: The matrix spelling of "take a warning back". Legacy carries BOTH
#: this and ``can_unwarn`` (bot.py); the matrix keys win for
#: storage/lookup, so it is settable even though legacy's PERMISSIONS
#: vocabulary does not list it.
_MATRIX_TWIN: Final[str] = "can_remove_warn"

#: Full permission vocabulary, grouped (RR-4 #45). Legacy kept the
#: grouping only as comments over a flat dict (bot.py:6566-6603), so
#: ``/perm`` could never show it — 24 keys arrived as one alphabetical
#: wall, and "which of these does an economy admin need?" had no answer
#: short of reading the source. Promoting it to data makes the ONE table
#: the render order for ``/perm list`` AND the vocabulary the
#: unknown-permission error prints back.
#:
#: Insertion order is the render order. Within a category the pairs are
#: kept adjacent (grant next to its inverse) rather than alphabetised:
#: ``can_ban``/``can_unban`` read as one decision.
PERMISSION_CATEGORIES: Final[dict[str, tuple[str, ...]]] = {
    "moderation": (
        "can_warn",
        "can_unwarn",
        _MATRIX_TWIN,
        "can_mute",
        "can_unmute",
        "can_ban",
        "can_unban",
        "can_kick",
        "can_pin",
        "can_clear",
    ),
    "management": (
        "can_manage_mods",
        "can_manage_ranks",
        "can_view_logs",
        "can_change_settings",
    ),
    "economy": (
        "can_manage_economy",
        "can_give_coins",
        "can_take_coins",
        "can_manage_shop",
    ),
    "activity": (
        "can_manage_activity",
        "can_run_cleanup",
    ),
    "broadcasts": ("can_broadcast",),
    "achievements": ("can_manage_achievements",),
    "special": (
        "can_bypass_limits",
        "can_bypass_mute",
        "can_see_hidden",
    ),
}

#: Every settable permission, in category order. Derived so the table
#: above cannot drift from what ``/perm set`` accepts.
ORDERED_PERMISSION_KEYS: Final[tuple[str, ...]] = tuple(
    key for keys in PERMISSION_CATEGORIES.values() for key in keys
)

#: Legacy's ``RankPermissions.PERMISSIONS`` keys (bot.py:6566-6603) —
#: the 24-key vocabulary WITHOUT the matrix twin. The default matrix
#: below only populates the 15 keys legacy's settings defaults actually
#: carry; the rest exist so ``/perm set`` (R2) can validate input and
#: the override table can grant them without code changes.
KNOWN_PERMISSIONS: Final[frozenset[str]] = frozenset(ORDERED_PERMISSION_KEYS) - {_MATRIX_TWIN}

#: Vocabulary spelling -> the key it is actually STORED and ENFORCED
#: under. Legacy was internally inconsistent about taking a warning
#: back: its permission vocabulary lists ``can_unwarn``
#: (bot.py:6566-6603) and ``/unwarn`` is gated on that spelling
#: (bot.py:31589), while its default matrix only ever carries
#: :data:`_MATRIX_TWIN` (bot.py:2618-2702). The port renders both — the
#: legacy spelling has to stay discoverable — and reads the matrix
#: spelling at the one enforcement seam — the ``can_remove_warn``
#: check inside ``moderation.handle_unwarn`` (named by symbol, not by
#: line: the old range had drifted, #1510). That left ``/perm set <rank>
#: can_unwarn on`` reporting success and changing nothing (#808).
#: Folding the WRITE onto the twin closes it without narrowing the
#: 25-key render surface that ``/perm list`` and the unknown-key error
#: are both built on.
PERMISSION_ALIASES: Final[dict[str, str]] = {"can_unwarn": _MATRIX_TWIN}


def storage_permission(perm_key: str) -> str:
    """The key ``perm_key`` is stored and enforced under.

    Identity for every key but the unwarn twin — see
    :data:`PERMISSION_ALIASES` for why that one is not its own key.
    """
    return PERMISSION_ALIASES.get(perm_key, perm_key)


#: Default rank → permission matrix, VERBATIM from the legacy settings
#: defaults (bot.py:2611-2712). Do NOT "fix" rank 1's missing
#: ``can_pin`` or add rank 0/-1 rows — absence IS the legacy semantic
#: (missing key/row → False).
DEFAULT_RANK_PERMISSIONS: Final[dict[int, dict[str, bool]]] = {
    6: {
        "can_warn": True,
        "can_mute": True,
        "can_ban": True,
        "can_kick": True,
        "can_pin": True,
        "can_remove_warn": True,
        "can_manage_mods": True,
        "can_view_logs": True,
        "can_change_settings": True,
        "can_manage_economy": True,
        "can_manage_activity": True,
        "can_broadcast": True,
        "can_manage_shop": True,
        "can_manage_achievements": True,
        "can_bypass_limits": True,
    },
    5: {
        "can_warn": True,
        "can_mute": True,
        "can_ban": True,
        "can_kick": True,
        "can_pin": True,
        "can_remove_warn": True,
        "can_manage_mods": True,
        "can_view_logs": True,
        "can_change_settings": True,
        "can_manage_economy": True,
        "can_manage_activity": True,
        "can_broadcast": True,
        "can_manage_shop": True,
        "can_manage_achievements": True,
        "can_bypass_limits": True,
    },
    4: {
        "can_warn": True,
        "can_mute": True,
        "can_ban": True,
        "can_kick": True,
        "can_pin": True,
        "can_remove_warn": True,
        "can_manage_mods": True,
        "can_view_logs": True,
        "can_change_settings": False,
        "can_manage_economy": True,
        "can_manage_activity": True,
        "can_broadcast": False,
        "can_manage_shop": True,
        "can_manage_achievements": False,
        "can_bypass_limits": False,
    },
    3: {
        "can_warn": True,
        "can_mute": True,
        "can_ban": True,
        "can_kick": True,
        "can_pin": True,
        "can_remove_warn": False,
        "can_manage_mods": False,
        "can_view_logs": True,
        "can_change_settings": False,
        "can_manage_economy": False,
        "can_manage_activity": False,
        "can_broadcast": False,
        "can_manage_shop": False,
        "can_manage_achievements": False,
        "can_bypass_limits": False,
    },
    2: {
        "can_warn": True,
        "can_mute": True,
        "can_ban": False,
        "can_kick": True,
        "can_pin": True,
        "can_remove_warn": False,
        "can_manage_mods": False,
        "can_view_logs": False,
        "can_change_settings": False,
        "can_manage_economy": False,
        "can_manage_activity": False,
        "can_broadcast": False,
        "can_manage_shop": False,
        "can_manage_achievements": False,
        "can_bypass_limits": False,
    },
    1: {
        "can_warn": True,
        "can_mute": False,
        "can_ban": False,
        "can_kick": False,
        # NOTE: legacy rank-1 row has NO ``can_pin`` key (bot.py:2698+).
        "can_remove_warn": False,
        "can_manage_mods": False,
        "can_view_logs": False,
        "can_change_settings": False,
        "can_manage_economy": False,
        "can_manage_activity": False,
        "can_broadcast": False,
        "can_manage_shop": False,
        "can_manage_achievements": False,
        "can_bypass_limits": False,
    },
}

#: The legacy matrix uses ``can_remove_warn`` while the PERMISSIONS
#: vocabulary uses ``can_unwarn`` (both exist in bot.py). The matrix
#: keys win for storage/lookup; expose the union for validation.
ALL_PERMISSION_KEYS: Final[frozenset[str]] = frozenset(ORDERED_PERMISSION_KEYS)


#: Ordered category keys, verbatim from legacy
#: ``COMMAND_ACCESS_CATEGORIES`` (bot.py:42321-42330) — the dict order
#: there is the render order of ``/cmdcfg list``, which is why ``admin``
#: precedes ``moderation`` even though the catalog rows are the other
#: way round. Display labels live in yaml under ``h_cmdcfg_cat_<key>``
#: so the eight headings translate; legacy hardcoded Russian.
COMMAND_CATEGORIES: Final[tuple[str, ...]] = (
    "basic",
    "economy",
    "games",
    "profile",
    "stats",
    "social",
    "admin",
    "moderation",
)


class SubCommand(NamedTuple):
    """A command that shares a catalog row with a *different* command.

    Three legacy rows bundle two commands under one entry: ``/weather``
    carries ``forecast``/``прогноз``, ``/cpc`` carries
    ``cpc_cancel``/``кнб_отмена``, ``/alias`` carries ``aliases``. Every
    one of those tokens reaches its own handler and does its own thing,
    yet the row lists them flat in :attr:`CommandEntry.aliases`, so the
    site and ``/cmdcfg show`` printed them as if ``/прогноз`` were just
    another way to spell ``/weather`` (#164) — a reader who wanted a
    multi-day forecast had no way to learn the command existed.

    They cannot simply be moved out of ``aliases``: that tuple is what
    :data:`ALIAS_TO_KEY` — and through it the ``/cmdcfg`` off-switch —
    consumes, and a token missing from it is a token that bypasses the
    owner's gate. So this is a *view* over the same tokens rather than
    a second list. The gate keeps reading ``aliases`` verbatim; only the
    display layers subtract these and print them under their own label
    with their own ``h_subcmd_<key>`` copy.

    Sharing the row also means sharing its rank: turning ``/cpc`` off
    turns ``/cpc_cancel`` off with it. That is the pre-existing
    behaviour and the reason these are not simply given rows of their
    own — a separate row would give ``cpc_cancel`` a separate switch,
    and a player who can start a match but not cancel one is a worse
    outcome than the honest label this ships instead.
    """

    #: Canonical name, and the ``h_subcmd_<key>`` copy that describes it.
    key: str
    #: Every spelling that reaches it, ``key`` included. All of them
    #: must also appear in the owning row's ``aliases`` — see
    #: ``tests/unit/core/test_ranks.py``.
    aliases: tuple[str, ...]


class CommandEntry(NamedTuple):
    """One row of the legacy ``COMMAND_CATALOG`` (bot.py:42339-42417).

    ``id`` is the operator-facing handle ``/cmdcfg`` accepts in place of
    a name, and it is unique — see
    :func:`test_catalog_ids_are_unique`. Legacy reused three ids
    (3, 16, 60); one row of each pair was renumbered into the 178+
    band, because a number that resolves to two commands is a number
    nobody can type. Which row moved is not uniform (#1073): ``faq2``
    3→178 and ``rp_commands`` 60→180 are the second row of their pair,
    but on id 16 it is the FIRST row that moved — ``lang``
    (bot.py:42350) → 179, while ``currency`` (bot.py:42357) kept 16.
    Lookups still go through
    :func:`entries_for_id`, which returns a tuple: uniqueness is a
    tested invariant, not something the resolver assumes.
    """

    id: int
    key: str
    category: str
    aliases: tuple[str, ...]
    default_rank: int
    #: The tokens in :attr:`aliases` that are *not* spellings of this
    #: command. See :class:`SubCommand`; empty on all but three rows.
    subcommands: tuple[SubCommand, ...] = ()


#: The single source of truth for the command catalog — ids, categories,
#: aliases and default minimum ranks in one table instead of the three
#: parallel dicts this module used to carry. VERBATIM from legacy
#: ``COMMAND_CATALOG`` (bot.py:42339-42417); 0 = everyone,
#: 2 = moderation, 5 = admin tools.
#:
#: One deliberate omission: legacy lists ``"relationship commands"``
#: (with a space) among ``rp_commands``' aliases. A Telegram command
#: name cannot contain a space, so that entry was unreachable and is
#: dropped rather than carried as decoration.
COMMAND_ENTRIES: Final[tuple[CommandEntry, ...]] = (
    # -- basic -------------------------------------------------------
    CommandEntry(1, "start", "basic", ("start", "kom_start"), 0),
    CommandEntry(2, "help", "basic", ("help", "h", "commands", "kom_help"), 0),
    # #446: rows below carry a Latin twin for a Russian alias that says
    # something the row name does not already say in English. The site
    # drops Cyrillic tokens from ``/commands/en`` (``readable_in``), so a
    # row whose only synonym is Russian offers an English reader no second
    # spelling at all — and «пары» / «отзыв» / «анекдот» are not spellings
    # of the row name, they are different words for the same thing. Rows
    # where the Russian alias is merely the Russian for an already-English
    # name (бан/ban, кик/kick, мут/mute) are deliberately NOT given an
    # invented second English spelling: the English form is the row name,
    # and a chip-less row renders no empty cell — the site omits the whole
    # ``cmd-aliases`` block. Every twin here is registered on its handler
    # too; ``tests/regression/test_help_surface.py`` fails otherwise.
    CommandEntry(3, "faq", "basic", ("faq", "вопросы", "questions", "kom_faq"), 0),
    # #121: legacy gave /faq2 the same id 3 as /faq. The primary keeps
    # the legacy number, the second page moves to the 178+ band —
    # nothing persists an id (overrides are keyed by ``command_key``),
    # so renumbering costs a line in ``/cmdcfg list`` and buys back a
    # number an operator can actually type.
    CommandEntry(178, "faq2", "basic", ("faq2", "faq_2"), 0),
    # NOT in legacy's catalog — new surface. The public offer, the
    # privacy policy and the support page, one tap from any chat; an
    # acquiring bank requires them to be reachable by an ordinary user,
    # so they get a catalog row like any other user-facing command
    # rather than living only in a link somebody has to be handed.
    CommandEntry(101, "legal", "basic", ("legal", "terms", "privacy", "offer", "docs"), 0),
    CommandEntry(4, "ping", "basic", ("ping", "kom_ping"), 0),
    CommandEntry(5, "botcheck", "basic", ("botcheck", "alive", "kom_botcheck"), 0),
    # ``clock``/``date`` dropped (#115): the legacy bot never registered
    # them either — the whole family below was invented in the catalog
    # table and copied forward verbatim. They cost nothing while the
    # catalog was internal; once the generated site started printing
    # every alias as a chip, each one became a name the page tells the
    # user to type and the bot answers with silence.
    CommandEntry(6, "time", "basic", ("time", "время", "time_msk", "kom_time"), 0),
    CommandEntry(7, "city", "basic", ("city", "город", "location", "kom_city"), 0),
    CommandEntry(
        8,
        "weather",
        "basic",
        (
            "weather",
            "погода",
            "forecast",
            "прогноз",
            "fc",
            "kom_weather",
            "kom_forecast",
        ),
        0,
        subcommands=(SubCommand("forecast", ("forecast", "прогноз", "fc", "kom_forecast")),),
    ),
    # RR-6 #72: ``kom_joke`` added to legacy's alias list. Legacy could
    # omit it because /joke was private-only there and /cmdcfg is a
    # per-GROUP gate; now that the command answers in groups, an alias
    # missing from the catalog is an alias that silently bypasses the
    # owner's off-switch.
    # #501: ``funny`` restored (bot.py:42349). It was dropped from the
    # catalog with no comment AND never registered on the handler, so
    # ``/funny`` answered nothing at all — not a deliberate divergence,
    # just a lost token. Same story for the four rows below.
    CommandEntry(9, "joke", "basic", ("joke", "шутка", "анекдот", "kom_joke", "funny"), 0),
    # RR-6 #72: NOT in legacy's catalog at all — /joke18 was private-only
    # so a group gate for it was meaningless. It answers in groups now,
    # and an 18+ command is precisely the one a group owner reaches for
    # /cmdcfg over. Default rank stays 0 (available to everyone, as in
    # legacy); the point is that raising it is now POSSIBLE.
    CommandEntry(
        29, "joke18", "basic", ("joke18", "шутка18", "анекдот18", "funny18", "kom_joke18"), 0
    ),
    # #121: legacy gave /lang id 16, already held by /currency. The
    # economy block below runs 10..18 without a gap, so 16 belongs to
    # /currency and the odd row out is this one.
    CommandEntry(
        179, "lang", "basic", ("lang", "language", "язык", "settings", "настройки", "kom_lang"), 0
    ),
    # -- economy -----------------------------------------------------
    CommandEntry(
        10, "balance", "economy", ("balance", "баланс", "bal", "kom_balance", "kom_bal"), 0
    ),
    CommandEntry(11, "daily", "economy", ("daily", "kom_daily"), 0),
    CommandEntry(12, "send", "economy", ("send",), 0),
    CommandEntry(13, "top", "economy", ("top", "kom_top"), 0),
    CommandEntry(14, "shop", "economy", ("shop", "магазин", "store", "kom_shop"), 0),
    CommandEntry(15, "inventory", "economy", ("inventory", "инвентарь", "inv"), 0),
    CommandEntry(16, "currency", "economy", ("currency", "валюта", "kom_currency"), 0),
    # #501: ``rates``/``exchange`` (bot.py:42358) and ``conversion``
    # (bot.py:42359) restored — same lost-token case as ``funny`` above.
    CommandEntry(17, "rate", "economy", ("rate", "курс", "курсы", "rates", "exchange"), 0),
    CommandEntry(18, "convert", "economy", ("convert", "конверт", "конвертация", "conversion"), 0),
    # ``/check`` is NOT legacy's ``/check``. Legacy registered it as a
    # second name for the developer-only *create* command
    # (``@bot.message_handler(commands=['create_check', 'check'])``,
    # bot.py:25301) and the catalog row below says so — id 98, rank 5.
    # The new pipeline repurposed the bare name for the thing an
    # ordinary user actually does with a check: CLAIM one
    # (``handlers/checks.py`` ``_handle_check``). Carrying the legacy
    # alias forward meant ``CommandAccessMiddleware`` resolved /check to
    # the dev row and refused every claim below rank 5 — in a
    # private-only router, where the live-TG-admin bypass cannot apply,
    # so nobody but a developer could redeem a check at all. Its own row,
    # rank 0.
    CommandEntry(102, "check", "economy", ("check", "чек"), 0),
    # -- games -------------------------------------------------------
    CommandEntry(20, "roulette", "games", ("roulette",), 0),
    CommandEntry(21, "dice", "games", ("dice",), 0),
    CommandEntry(22, "duel", "games", ("duel", "дуэль"), 0),
    CommandEntry(23, "duel_stats", "games", ("duel_stats",), 0),
    # RR-6 #71: ``kom_quote`` added for the same reason as ``kom_joke``
    # above — /quote is group-capable now, so every alias must be gateable.
    CommandEntry(24, "quote", "games", ("quote", "цитата", "kom_quote"), 0),
    # #501: ``dice_roll`` (bot.py:42366) and ``coin``/``coinflip``
    # (bot.py:42367) restored — same lost-token case as ``funny`` above.
    # ``dice`` stays OUT of this row on purpose even though the handler
    # registers it on ``/roll``: it owns row 21 above, so
    # ``command_key_for`` resolves it to ``dice`` and ``/cmdcfg`` gates
    # it under that key. Listing it twice is the one thing
    # :func:`test_catalog_aliases_are_unique_across_entries` forbids.
    CommandEntry(25, "roll", "games", ("roll", "кубик", "dice_roll"), 0),
    CommandEntry(26, "flip", "games", ("flip", "монетка", "kom_flip", "coin", "coinflip"), 0),
    CommandEntry(27, "calc", "games", ("calc", "калькулятор", "kom_calc"), 0),
    # ``cpc_accept``/``cpc_decline`` dropped (#115). Legacy DID register
    # them (rock_paper_scissors.py:717-718) as a typed alternative to the
    # challenge card's buttons, but the new pipeline answers accept and
    # decline through the inline keyboard only, and the accept path needs
    # the session id the callback payload carries — a bare
    # ``/cpc_accept`` has nothing to resolve. Listing them as *aliases of
    # ``/cpc``* would be worse than dropping them: typing either would
    # then open a brand-new challenge instead of answering the pending
    # one. The buttons sit on the card the opponent already received, so
    # nothing is unreachable.
    CommandEntry(
        28,
        "cpc",
        "games",
        ("cpc", "rps", "кнб", "knb", "cpc_cancel", "кнб_отмена"),
        0,
        subcommands=(SubCommand("cpc_cancel", ("cpc_cancel", "кнб_отмена")),),
    ),
    # -- profile -----------------------------------------------------
    CommandEntry(30, "whoami", "profile", ("whoami", "me", "kom_whoami"), 0),
    CommandEntry(
        31, "profile", "profile", ("profile", "info", "профиль", "инфо", "kom_profile"), 0
    ),
    CommandEntry(32, "achievements", "profile", ("achievements", "ach"), 0),
    # -- stats -------------------------------------------------------
    CommandEntry(40, "stats", "stats", ("stats", "статистика"), 0),
    CommandEntry(41, "chatinfo", "stats", ("chatinfo",), 0),
    CommandEntry(42, "chatstats", "stats", ("chatstats", "cstats"), 0),
    CommandEntry(43, "top_activity", "stats", ("top_activity", "topactive"), 0),
    # -- social ------------------------------------------------------
    CommandEntry(50, "ai", "social", ("ai", "ask", "chat", "gpt", "ии", "kom_ai"), 0),
    CommandEntry(51, "support", "social", ("support", "ticket", "мои_обращения_help"), 0),
    CommandEntry(52, "feedback", "social", ("feedback", "отзыв", "review", "kom_feedback"), 0),
    CommandEntry(53, "ad", "social", ("ad", "ads", "reklama"), 0),
    # The social block is where the invented-alias family clustered
    # hardest (#115): ``wedding``, ``my_marriage_status``, ``unmarry``,
    # ``couples``, ``married``, ``relation``, ``relations_status``,
    # ``break_up``, ``split``, ``relationships_list`` and ``partners``
    # were all advertised and none of them was ever registered — not
    # here and not in legacy.
    CommandEntry(54, "marry", "social", ("marry", "брак", "жениться"), 0),
    CommandEntry(55, "marriage", "social", ("marriage", "my_marriage", "брак_статус"), 0),
    CommandEntry(56, "divorce", "social", ("divorce", "развод"), 0),
    CommandEntry(57, "marriages", "social", ("marriages", "браки", "пары", "couples"), 0),
    CommandEntry(
        58,
        "relationship",
        "social",
        ("relationship", "rel", "отношения", "в_отношениях"),
        0,
    ),
    CommandEntry(59, "breakup", "social", ("breakup", "расстаться"), 0),
    CommandEntry(60, "relations", "social", ("relations", "rels", "отношения_список", "отны"), 0),
    # #121: third and last of legacy's reused ids. 60 closes the social
    # run 50..60, so /relations keeps it and the RP cheat-sheet moves.
    CommandEntry(180, "rp_commands", "social", ("rp_commands", "rp", "рп_команды"), 0),
    # -- moderation — every row defaults to rank 2 in legacy, incl.
    #    ban and unwarn (bot.py:42399, :42401; block 42393-42403).
    # #116: the ``kom_*`` spellings below are not decoration. A command
    # name that is not an alias of any row resolves to *itself* in
    # ``command_key_for``, and an unknown key defaults to rank 0 — so
    # registering ``/kom_ban`` on the handler without cataloguing it
    # here would hand every group member a rank-2 command under a
    # second name. The negative halves (``kom_unban``/``unmute``/
    # ``unpin``) were catalogued from the start; the positive ones were
    # missed.
    CommandEntry(61, "warn", "moderation", ("warn", "варн", "предупреждение", "kom_warn"), 2),
    CommandEntry(62, "kick", "moderation", ("kick", "кик", "kom_kick"), 2),
    CommandEntry(63, "pin", "moderation", ("pin", "закрепить", "kom_pin"), 2),
    CommandEntry(64, "unpin", "moderation", ("unpin", "открепить", "kom_unpin"), 2),
    CommandEntry(65, "mute", "moderation", ("mute", "мут", "kom_mute"), 2),
    CommandEntry(66, "unmute", "moderation", ("unmute", "размут", "kom_unmute"), 2),
    CommandEntry(67, "ban", "moderation", ("ban", "бан", "kom_ban"), 2),
    CommandEntry(68, "unban", "moderation", ("unban", "разбан", "kom_unban"), 2),
    CommandEntry(
        69,
        "unwarn",
        "moderation",
        ("unwarn", "разварн", "снять_варн", "снять_предупреждение", "kom_unwarn"),
        2,
    ),
    CommandEntry(70, "warnings", "moderation", ("warnings", "warns", "варны", "предупреждения"), 2),
    CommandEntry(71, "clear", "moderation", ("clear", "purge", "очистить", "очистка"), 2),
    # -- admin -------------------------------------------------------
    CommandEntry(89, "dev", "admin", ("dev", "developer"), 5),
    # Legacy pinned this at 5, and for legacy that was right: ``/admin``
    # opened the developer panel. It opens the multi-group admin panel
    # now (``handlers/mygroups.py``, the same surface legacy gave a
    # non-developer), which is a rank-0 command — every row it renders
    # is re-scoped to the caller's own ``bot_groups`` attributions, so
    # the gate that matters is ownership of the group, not global rank.
    # Left at 5 the word would answer nobody but the developer.
    CommandEntry(90, "admin", "admin", ("admin",), 0),
    CommandEntry(91, "admin_help", "admin", ("admin_help", "owner_help"), 5),
    CommandEntry(92, "maintenance", "admin", ("maintenance",), 5),
    CommandEntry(93, "cfg_button", "admin", ("cfg_button", "setbutton"), 5),
    CommandEntry(
        94,
        "alias",
        "admin",
        ("alias", "aliases"),
        5,
        subcommands=(SubCommand("aliases", ("aliases",)),),
    ),
    CommandEntry(95, "modcfg", "admin", ("modcfg", "модконфиг"), 5),
    CommandEntry(96, "perm", "admin", ("perm", "rankperm"), 5),
    CommandEntry(97, "cmdcfg", "admin", ("cmdcfg", "cmdaccess"), 5),
    # ``check`` moved out to its own economy row (see there). The RU
    # spelling the handler really registers takes its place, so the two
    # names of the dev command are gated alike instead of one of them
    # silently resolving to itself at rank 0.
    CommandEntry(98, "create_check", "admin", ("create_check", "создать_чек"), 5),
    CommandEntry(99, "clearlogs", "admin", ("clearlogs",), 5),
    CommandEntry(100, "test_logs", "admin", ("test_logs",), 5),
    # ══ new-pipeline commands legacy's catalog never had (ids 103+) ══
    #
    # Everything above is a port of legacy's numbered table with two
    # documented departures: three rows legacy never had — ``joke18``
    # (29), ``legal`` (101) and ``check`` (102) — and three legacy rows
    # renumbered out of an id collision into the 178+ band (``faq2``
    # 178, ``lang`` 179, ``rp_commands`` 180). Every other row above
    # matches legacy key-for-key.
    # The rows below are the other half of the bot: 75 commands the new
    # pipeline registers and the old catalog never listed, which meant
    # ``/help`` and the generated site page advertised well under half
    # the working surface while the rest stayed reachable-but-
    # undiscoverable — the same richness regression RR-6 #62 set out to
    # fix, still open for every command ported after the catalog was
    # copied.
    #
    # **Every row here defaults to rank 0 except the ``admin`` block.**
    # That is deliberate and makes the addition behaviour-neutral for
    # ``CommandAccessMiddleware``: a command absent from the catalog
    # already resolves to :data:`DEFAULT_COMMAND_MIN_RANK` (0), so a
    # rank-0 row grants and denies exactly what today's code does. What
    # it *adds* is a key ``/cmdcfg`` can address — until now an owner
    # literally could not put a threshold on ``/withdraw``, because
    # ``handle_cmdcfg`` only accepts catalog keys. Guessing a non-zero
    # default instead would be the ``/check`` bug (id 102) all over
    # again: several of these are private-chat-only, where the
    # live-TG-admin bypass cannot apply, so a wrong guess locks the
    # command outright rather than merely mis-labelling it.
    #
    # The ``admin`` block is the one *partial* exception: SIX of its
    # seven handlers already hard-refuse non-developers via
    # ``settings.bot.is_developer``, so rank 5 costs them no access —
    # it only changes which refusal the caller reads — and it keeps the
    # owner reference honest, where a lone unannotated row among
    # ``[от 5⭐]`` neighbours would read as "anyone may run this".
    #
    # ``rank`` (id 173) is the seventh and it is NOT one of them:
    # ``handle_rank`` has no developer check on purpose (see the module
    # docstring of ``handlers/rank_admin.py`` — it is a read-only
    # self-service card), so for that row the catalog WAS the only gate
    # and rank 5 locked every non-developer out of their own rank card.
    # Legacy never gated it: ``rank`` has no row in the legacy
    # ``COMMAND_CATALOG`` (bot.py:42339-42417) at all. Hence rank 0
    # below — exactly the ``/check`` lesson (id 102) a second time.
    #
    # Visibility is steered by *category*, never by ``HELP_HIDDEN_KEYS``:
    # that set is reserved for genuinely dead keys and
    # ``test_help_surface`` fails the moment a live one is hidden there.
    # -- basic (new) --------------------------------------------------
    CommandEntry(103, "cancel", "basic", ("cancel", "отмена"), 0),
    CommandEntry(104, "kom", "basic", ("kom", "enter_kom", "войти_в_ком"), 0),
    CommandEntry(105, "exit_kom", "basic", ("exit_kom", "выйти_из_ком"), 0),
    CommandEntry(106, "mode", "basic", ("mode", "режим", "kom_mode"), 0),
    # ``сброс`` matches the vocabulary the bot already answers to for
    # the same idea — ``/city сброс``, ``/time сброс`` — so the word a
    # user has already been taught works here too.
    CommandEntry(107, "reset", "basic", ("reset", "сброс"), 0),
    CommandEntry(108, "ai_limits", "basic", ("ai_limits", "limits", "ии_лимиты"), 0),
    # Reading the group's rules is a member action, not a moderation
    # one — only ``/setrules`` belongs in the staff block.
    CommandEntry(109, "rules", "basic", ("rules", "правила", "kom_rules"), 0),
    CommandEntry(
        110, "my_tickets", "basic", ("my_tickets", "tickets", "мои_обращения", "мои_тикеты"), 0
    ),
    # -- economy (new) ------------------------------------------------
    CommandEntry(111, "topup", "economy", ("topup", "buy_coins", "пополнить"), 0),
    CommandEntry(112, "withdraw", "economy", ("withdraw", "wd", "вывод"), 0),
    CommandEntry(113, "withdraw_status", "economy", ("withdraw_status",), 0),
    CommandEntry(114, "p2p", "economy", ("p2p", "п2п"), 0),
    CommandEntry(115, "buy", "economy", ("buy", "купить", "kom_buy"), 0),
    CommandEntry(116, "vip", "economy", ("vip",), 0),
    CommandEntry(117, "vip_shop", "economy", ("vip_shop",), 0),
    CommandEntry(118, "donate", "economy", ("donate", "донат", "kom_donate"), 0),
    CommandEntry(119, "mydonates", "economy", ("mydonates", "donates", "мои_донаты"), 0),
    CommandEntry(120, "donaters", "economy", ("donaters", "донатеры", "donors", "kom_donaters"), 0),
    CommandEntry(
        121, "commission", "economy", ("commission", "fees", "комиссии", "мои_комиссии"), 0
    ),
    CommandEntry(122, "promo", "economy", ("promo", "промокод"), 0),
    CommandEntry(123, "crypto", "economy", ("crypto", "крипта", "криптовалюта", "kom_crypto"), 0),
    # Not a second spelling of the developer-only ``/create_check``
    # (id 98) despite the near-identical name: this is L-30's
    # interactive flow that lets an ordinary user mint a check from
    # their own balance, a different handler with no developer gate.
    CommandEntry(124, "check_create", "economy", ("check_create", "newcheck", "чек_создать"), 0),
    CommandEntry(125, "group_pay", "economy", ("group_pay", "gpay", "выплата_из_казны"), 0),
    # -- games (new) --------------------------------------------------
    CommandEntry(126, "games", "games", ("games", "игры", "kom_games"), 0),
    CommandEntry(127, "pvp_coin", "games", ("pvp_coin", "пвп_монета"), 0),
    CommandEntry(128, "pvp_dice", "games", ("pvp_dice", "пвп_кости"), 0),
    CommandEntry(129, "accept", "games", ("accept", "принять"), 0),
    CommandEntry(130, "decline", "games", ("decline", "отклонить", "reject"), 0),
    # -- profile (new) ------------------------------------------------
    CommandEntry(131, "nick", "profile", ("nick", "setnick", "ник", "никнейм"), 0),
    CommandEntry(
        132, "referral", "profile", ("referral", "ref", "реферал", "реферальная_ссылка"), 0
    ),
    CommandEntry(133, "referrals", "profile", ("referrals", "refs", "рефералы", "мои_рефералы"), 0),
    CommandEntry(134, "emojis", "profile", ("emojis", "эмодзи"), 0),
    CommandEntry(135, "emoji_buy", "profile", ("emoji_buy",), 0),
    CommandEntry(136, "emoji_preview", "profile", ("emoji_preview",), 0),
    CommandEntry(137, "emoji_set", "profile", ("emoji_set",), 0),
    CommandEntry(138, "timezone", "profile", ("timezone", "часовой_пояс", "tz", "kom_timezone"), 0),
    CommandEntry(139, "voice", "profile", ("voice", "голос"), 0),
    CommandEntry(140, "voice_vip", "profile", ("voice_vip", "голос_вип"), 0),
    CommandEntry(141, "voice_stats", "profile", ("voice_stats",), 0),
    CommandEntry(142, "staff_me", "profile", ("staff_me",), 0),
    # -- stats (new) --------------------------------------------------
    CommandEntry(143, "rating", "stats", ("rating", "рейтинг"), 0),
    CommandEntry(144, "top_groups", "stats", ("top_groups", "rating_groups"), 0),
    CommandEntry(
        145,
        "groupstats",
        "stats",
        (
            "groupstats",
            "статистика_группы",
            "group_treasury",
            "казна_группы",
            "kom_groupstats",
        ),
        0,
    ),
    CommandEntry(146, "group_stats", "stats", ("group_stats",), 0),
    CommandEntry(147, "mygroups", "stats", ("mygroups", "мои_группы"), 0),
    # -- social (new) -------------------------------------------------
    # ``marry_accept``/``marry_decline`` share one handler, and so do
    # the two ``marry_top_*`` switches; they still get a row each,
    # because ``/help`` prints ``/{key}`` and a reader needs to see the
    # spelling that says no as well as the one that says yes.
    CommandEntry(148, "marry_accept", "social", ("marry_accept", "m_accept", "принять_брак"), 0),
    CommandEntry(
        149, "marry_decline", "social", ("marry_decline", "m_decline", "отклонить_брак"), 0
    ),
    CommandEntry(150, "marry_extend", "social", ("marry_extend", "m_extend", "брак_продлить"), 0),
    CommandEntry(151, "marry_other", "social", ("marry_other", "m_other", "твой_брак"), 0),
    CommandEntry(
        152,
        "marry_auto_divorce",
        "social",
        ("marry_auto_divorce", "auto_divorce", "брак_режим_развода"),
        0,
    ),
    CommandEntry(
        153, "marry_top_on", "social", ("marry_top_on", "m_top_on", "брак_рейтинг_вкл"), 0
    ),
    CommandEntry(
        154, "marry_top_off", "social", ("marry_top_off", "m_top_off", "брак_рейтинг_выкл"), 0
    ),
    CommandEntry(155, "activities", "social", ("activities", "acts", "совместные"), 0),
    # -- moderation (new) ---------------------------------------------
    # Rank 0, unlike legacy's moderation block at 2: every handler here
    # runs its own ``_require_admin``/chat-admin check, and two of them
    # (``/voice_settings``, ``/transfer_rights``) are reachable from a
    # private chat, where a rank gate has no live-admin bypass to fall
    # back on. The owner can still raise any of them with ``/cmdcfg``.
    CommandEntry(
        156, "groupadmin", "moderation", ("groupadmin", "group_admin", "управлениегруппой"), 0
    ),
    CommandEntry(
        157, "setrules", "moderation", ("setrules", "установить_правила", "kom_setrules"), 0
    ),
    CommandEntry(158, "filter_add", "moderation", ("filter_add", "f_add", "фильтр_добавить"), 0),
    CommandEntry(159, "filter_list", "moderation", ("filter_list", "f_list", "фильтр_список"), 0),
    CommandEntry(
        160, "filter_remove", "moderation", ("filter_remove", "f_del", "фильтр_удалить"), 0
    ),
    CommandEntry(161, "welcome_on", "moderation", ("welcome_on", "w_on", "приветствие_вкл"), 0),
    CommandEntry(162, "welcome_off", "moderation", ("welcome_off", "w_off", "приветствие_выкл"), 0),
    CommandEntry(
        163, "welcome_test", "moderation", ("welcome_test", "w_test", "приветствие_тест"), 0
    ),
    CommandEntry(
        164, "setwelcome", "moderation", ("setwelcome", "приветствие_текст", "set_welcome"), 0
    ),
    CommandEntry(165, "fine", "moderation", ("fine", "штраф", "penalty"), 0),
    CommandEntry(
        166, "rating_include", "moderation", ("rating_include", "rating_in", "рейтинг_включить"), 0
    ),
    CommandEntry(
        167,
        "rating_exclude",
        "moderation",
        ("rating_exclude", "rating_out", "рейтинг_исключить"),
        0,
    ),
    CommandEntry(168, "voice_settings", "moderation", ("voice_settings", "voice_settings_ru"), 0),
    CommandEntry(
        169, "transfer_rights", "moderation", ("transfer_rights", "giverights", "передать_права"), 0
    ),
    # -- admin (new) --------------------------------------------------
    # Rank 5 — see the block comment above: these refuse non-developers
    # inside the handler, so the row only picks which refusal shows.
    # ``rank`` is the exception and stays at 0: its handler is ungated.
    CommandEntry(170, "broadcast", "admin", ("broadcast",), 5),
    CommandEntry(171, "promo_create", "admin", ("promo_create", "создать_промокод"), 5),
    CommandEntry(172, "rating_recalc", "admin", ("rating_recalc", "рейтинг_пересчет"), 5),
    CommandEntry(173, "rank", "admin", ("rank", "ранг"), 0),
    CommandEntry(174, "admin_tickets", "admin", ("admin_tickets",), 5),
    CommandEntry(175, "ticket_reply", "admin", ("ticket_reply",), 5),
    CommandEntry(176, "ticket_close", "admin", ("ticket_close",), 5),
    # #117: ``/report`` is filed under **social**, not moderation, on
    # purpose. ``moderation`` is a STAFF category — the site publishes it
    # under «🛡 Персонал» and ``/help`` shows it to staff — while
    # reporting is the one moderation-adjacent thing an ordinary member
    # is *supposed* to do. Rank 0 for the same reason: a gate here would
    # silence exactly the people the feature exists for.
    CommandEntry(177, "report", "social", ("report", "репорт", "kom_report"), 0),
)


#: command_key → default minimum rank. Derived from
#: :data:`COMMAND_ENTRIES` so the two can never drift; kept as a public
#: name because the rank middleware and the repo docstrings refer to it.
COMMAND_CATALOG: Final[dict[str, int]] = {
    entry.key: entry.default_rank for entry in COMMAND_ENTRIES
}

#: Min rank for a command absent from the catalog (legacy treats
#: unknown commands as unrestricted).
DEFAULT_COMMAND_MIN_RANK: Final[int] = 0

#: alias (bare command, lowercase, no slash) → catalog key.
COMMAND_ALIAS_TO_KEY: Final[dict[str, str]] = {
    alias.lower(): entry.key for entry in COMMAND_ENTRIES for alias in entry.aliases
}

_ENTRY_BY_KEY: Final[dict[str, CommandEntry]] = {entry.key: entry for entry in COMMAND_ENTRIES}

#: Every subcommand token in the catalog, flattened. Only used to assert
#: the invariants that keep the two views consistent (a subcommand token
#: must also be an alias of its row, and no subcommand may collide with a
#: catalog key) — the gate and the display layers both go through the
#: owning row, never through this set.
SUBCOMMAND_TOKENS: Final[frozenset[str]] = frozenset(
    token for entry in COMMAND_ENTRIES for sub in entry.subcommands for token in sub.aliases
)


def _index_by_id() -> dict[int, tuple[CommandEntry, ...]]:
    """Group entries by id, preserving catalog order within an id.

    Legacy built ``COMMAND_BY_ID`` as a plain dict comprehension
    (bot.py:42419), so on its three duplicated ids — 3 (/faq, /faq2),
    16 (/lang, /currency) and 60 (/relations, /rp_commands) — the last
    row silently won and the first was unreachable by number. #121
    renumbered one row of each pair — the second for ids 3 and 60, the
    FIRST for id 16 (#1073) — so every group here holds exactly one
    entry today.

    The grouping stays a tuple rather than collapsing to a plain dict
    for the same reason the dict comprehension was wrong: uniqueness is
    a property of the table, enforced by
    ``test_catalog_ids_are_unique``. If a future row reintroduces a
    collision, ``/cmdcfg`` asks which command was meant instead of
    silently configuring whichever one the table happens to list last.
    """
    grouped: dict[int, list[CommandEntry]] = {}
    for entry in COMMAND_ENTRIES:
        grouped.setdefault(entry.id, []).append(entry)
    return {cmd_id: tuple(rows) for cmd_id, rows in grouped.items()}


_ENTRIES_BY_ID: Final[dict[int, tuple[CommandEntry, ...]]] = _index_by_id()

_ENTRIES_BY_CATEGORY: Final[dict[str, tuple[CommandEntry, ...]]] = {
    category: tuple(
        sorted(
            (entry for entry in COMMAND_ENTRIES if entry.category == category),
            key=lambda entry: (entry.id, entry.key),
        )
    )
    for category in COMMAND_CATEGORIES
}


def command_key_for(command: str) -> str:
    """Resolve a bare command name (no slash, no @botname) to its
    catalog key. Unknown commands map to themselves so the override
    table can still pin a rank onto them.
    """
    bare = command.lower().lstrip("/").split("@", 1)[0]
    return COMMAND_ALIAS_TO_KEY.get(bare, bare)


def default_min_rank(command_key: str) -> int:
    """Catalog default minimum rank for ``command_key`` (0 if absent)."""
    return COMMAND_CATALOG.get(command_key, DEFAULT_COMMAND_MIN_RANK)


def command_entry(command_key: str) -> CommandEntry | None:
    """Catalog row for ``command_key``; ``None`` for keys the catalog
    does not know (new-pipeline commands are still configurable — they
    just have no id, category or alias list to show)."""
    return _ENTRY_BY_KEY.get(command_key)


def entries_for_id(command_id: int) -> tuple[CommandEntry, ...]:
    """Every catalog row carrying ``command_id`` — empty if none, and
    exactly one for every id the catalog does carry (#121). Longer
    tuples are the fail-safe shape for a reintroduced collision; see
    :func:`_index_by_id`."""
    return _ENTRIES_BY_ID.get(command_id, ())


def synonym_aliases(entry: CommandEntry) -> tuple[str, ...]:
    """The row's aliases that really are other spellings of it.

    Which is ``aliases`` minus the canonical name (a list that repeats
    the name it belongs to says nothing) and minus every token a
    :class:`SubCommand` owns. Display layers call this instead of
    reading ``aliases`` directly; the rank gate deliberately does not,
    because there it is the full tuple that has to be honoured.
    """
    owned = {token for sub in entry.subcommands for token in sub.aliases}
    return tuple(alias for alias in entry.aliases if alias != entry.key and alias not in owned)


def entries_in_category(category: str) -> tuple[CommandEntry, ...]:
    """Catalog rows of one category, in ascending id order (legacy
    rendered them in raw list order, which puts /calc after /cpc for no
    reason a reader of the output could infer)."""
    return _ENTRIES_BY_CATEGORY.get(category, ())
