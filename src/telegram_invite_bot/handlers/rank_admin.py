"""Rank-management commands — /perm, /cmdcfg, /rank (cluster R2).

DESIGN_RANKS.md §2.3, legacy anchors verified:

* **/perm (+/rankperm)** — port of ``cmd_perm`` (bot.py:42248-42317):
  ``/perm list <rank>`` renders the merged matrix row (legacy iterated
  ``sorted(RankPermissions.PERMISSIONS.keys())`` with ✅/❌ marks,
  bot.py:42283-42287); ``/perm set <rank> <permission> on|off``
  validates the permission against the full vocabulary
  (bot.py:42295-42302) and writes one cell. Both surfaces render the
  vocabulary grouped by :data:`PERMISSION_CATEGORIES` (RR-4 #45) —
  legacy had the grouping in comments only — and a rejected key is
  echoed back with its near-misses (legacy echoed the key,
  bot.py:42299; the port had dropped even that). Legacy stored deltas in
  ``settings.json``; the new pipeline writes through
  :meth:`RankRepo.set_permission` (moderation.db, same delta-only
  shape). Legacy restricted ``<rank>`` to ``{"1".."5"}``
  (bot.py:42273-42275); the design-approved range here is the full
  settable 0..6 so a developer can also inspect rank 0/6 rows and
  grant rank-0 cells through the override table.

* **/cmdcfg (+/cmdaccess)** — port of ``cmd_cmdcfg``
  (bot.py:43125-43237) over ``command_rank_overrides``
  (bot.py:42808-42872): ``list [category]`` prints the whole catalog
  grouped into the eight categories with the 0..6 legend
  (bot.py:43153-43171), marking rows whose rank came from an override;
  ``show <id|cmd>`` renders the command card — numeric id, alias list,
  category, current vs default (bot.py:43174-43196); ``set <id|cmd>
  <0-6>`` (6=disabled) removes the override row when the value equals
  the catalog default — legacy ``set_command_required_rank`` did
  exactly that (bot.py:42840-42841); ``reset <id|cmd|all>`` drops
  override rows.

  ``<id|cmd>`` accepts a catalog number, ``/cmd``, ``cmd@Bot`` and
  every legacy alias. Two divergences from legacy, both deliberate:
  unknown-but-well-formed *names* resolve to themselves (catalog
  default 0) so new-pipeline commands absent from the legacy catalog
  can still be pinned — the R4 middleware gates by the same key —
  while unknown *numbers* are refused, because a number the catalog
  does not carry cannot be anything but a typo. And on legacy's three
  duplicated ids the reply asks which command was meant instead of
  silently taking the last row (see :func:`_resolve_ref_or_reply`).

* **/rank** — read-only rank card (no legacy slash command; the copy
  is the legacy ``your_rank`` key used in the rank-denial composite,
  bot.py:7088). No reply target → caller's own rank via the existing
  ``your_rank`` yaml key; with a reply → the replied user's rank via
  the new ``h_rankadm_rank_of`` key. Group rendering masks DEVELOPER
  as OWNER (``rank_name(..., in_group=True)``, legacy
  ``get_rank_name`` semantics).

Authorisation: /perm and /cmdcfg are developer-only
(``settings.bot.is_developer``) — legacy gated on ``is_owner``
(bot.py:42258, 43136) and the design maps legacy owner ≈ new-pipeline
developer (DESIGN_RANKS.md §2.3). The denial reuses the legacy-parity
``owner_only_short`` key (the verbatim legacy reply text). /rank is
ungated — it is a read-only self-service card (DESIGN_RANKS.md §2.3
lists it without a gate; the cluster's "developer-only" applies to the
management commands).

No session middleware: :class:`RankRepo` writes run in short
``session_for`` transactions (commit-on-exit) and :class:`RankService`
opens its own sessions — same pattern as ``handlers/rank_self.py``.
``lang`` is injected by the root ``LanguageMiddleware``.
"""

from __future__ import annotations

import difflib
import html
import re
from typing import TYPE_CHECKING, Final

from aiogram import Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.core.ranks import (
    ALL_PERMISSION_KEYS,
    COMMAND_CATEGORIES,
    MAX_SETTABLE_RANK,
    MIN_SETTABLE_RANK,
    ORDERED_PERMISSION_KEYS,
    PERMISSION_CATEGORIES,
    command_entry,
    command_key_for,
    default_min_rank,
    entries_for_id,
    entries_in_category,
    rank_name,
    storage_permission,
    synonym_aliases,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.rank_repo import (
    RankRepo,
    clear_command_override_cache,
    clear_rank_matrix_cache,
)
from telegram_invite_bot.services.rank_service import RankService
from telegram_invite_bot.utils.aiogram import command_body
from telegram_invite_bot.utils.numbers import is_int_token, parse_int_token
from telegram_invite_bot.utils.render import (
    PAGE_BUDGET,
    on_off_text,
    paginate_lines,
    parsed_length,
)

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db.engines import EngineRegistry

log = logger.bind(component="handlers.rank_admin")


#: Min rank meaning "command disabled" (legacy /cmdcfg scale: 0=all …
#: 6=off, bot.py:43162 "0=всем | 1..5=по рангу | 6=off").
DISABLED_MIN_RANK: Final[int] = 6

#: Well-formed command key after :func:`command_key_for` normalisation
#: (lowercased, slash/@botname stripped). Rejects junk like emoji or
#: whitespace so the override table only ever stores plausible keys.
_COMMAND_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_Ѐ-ӿ]{1,64}$")

#: Toggle vocabulary — legacy ``parse_toggle`` accepted on/off; the RU
#: tokens mirror handlers/modcfg.py so the two config surfaces feel
#: identical to a Russian-speaking operator.
_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"on", "1", "true", "yes", "вкл", "да"})
_FALSE_TOKENS: Final[frozenset[str]] = frozenset({"off", "0", "false", "no", "выкл", "нет"})

#: Marks a ``/cmdcfg list`` row whose rank came from the override table
#: rather than the catalog default. Language-neutral on purpose — it
#: sits inside a dense monospace column where a word would not fit.
_OVERRIDE_MARK: Final[str] = " ✏️"

#: Catalog ids top out at 180 — the port's ``COMMAND_ENTRIES``
#: (``core/ranks.py``) is the authority, not legacy's table, where the
#: id column was not even unique (bot.py:42390-42391 both carry id 60).
#: Anything longer than this many digits is answered with "unknown
#: command" without ever touching the catalog. :func:`is_int_token`
#: already refuses the runs long enough to make ``int()`` itself
#: raise; this is the far tighter domain check.
_MAX_ID_DIGITS: Final[int] = 9


def _parse_toggle(raw: str) -> bool | None:
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return None


def _parse_rank(raw: str) -> int | None:
    """``"0".."6"`` → int; anything else (incl. ``-1``) → ``None``.

    :func:`parse_int_token` rather than a bare ``int()``. The bare call
    also takes Arabic-Indic digits, ``_`` separators, surrounding
    whitespace and a leading sign, and this parser sits in front of
    ``/cmdcfg set <cmd> <0-6>``: ``/cmdcfg set ban ０`` (fullwidth zero)
    would read as ``min_rank = 0`` and open ``/ban`` to everyone. Same
    policy as :func:`is_int_token` on the id branch below.
    """
    value = parse_int_token(raw)
    if value is None:
        return None
    if value < MIN_SETTABLE_RANK or value > MAX_SETTABLE_RANK:
        return None
    return value


def _parse_command_key(raw: str) -> str | None:
    """Resolve user input (``/warn``, ``warn@Bot``, RU alias) to a
    catalog key; ``None`` for junk that can't be a command name."""
    key = command_key_for(raw.strip())
    if not _COMMAND_KEY_RE.match(key):
        return None
    return key


# -- /perm ----------------------------------------------------------------------


#: How many "did you mean" candidates a rejected permission key gets.
#: Three is enough to cover a plausible typo without turning the error
#: into a second vocabulary listing — the vocabulary is right below it.
_PERM_SUGGESTIONS: Final[int] = 3

#: How close a candidate must be to be offered — difflib's default.
#: Tightening it to 0.72 was tried and reverted: it still lets
#: ``can_pin`` through for ``can_win`` (ratio 0.86) while silencing the
#: single most likely operator input, the key typed without its prefix
#: — ``mute``/``ban``/``warn`` score 0.61-0.70 against their own keys
#: and would answer nothing. The suggestions are a labelled hint the
#: operator has to retype, so an extra near-miss costs nothing; a
#: missing one costs a round trip.
_PERM_SUGGEST_CUTOFF: Final[float] = 0.6

#: How much of a rejected key is echoed back. Long enough that any real
#: typo is shown whole (the longest key is 22 chars), short enough that
#: a pasted paragraph cannot pad the reply.
_PERM_ECHO_CHARS: Final[int] = 32


def _perm_usage(lang: str) -> str:
    """The ``/perm`` usage card.

    The old copy spelled its placeholders as bare ``<ранг>``/``<rank>``
    — with the bot's global HTML parse mode Telegram read that as an
    unsupported start tag and refused the whole message, so ``/perm``
    with no arguments answered nothing at all. Worked examples carry
    the same information and cannot be mistaken for markup.
    """
    return t("h_rankadm_usage", lang, min=MIN_SETTABLE_RANK, max=MAX_SETTABLE_RANK)


def _perm_suggestions(perm_key: str) -> list[str]:
    """Vocabulary keys close enough to ``perm_key`` to be a typo of it.

    Fed the ORDERED tuple, not the frozenset: equally-close candidates
    are returned in input order, and set iteration order varies between
    processes (hash randomisation) — the same typo would then get
    different suggestions on different workers.
    """
    return difflib.get_close_matches(
        perm_key,
        ORDERED_PERMISSION_KEYS,
        n=_PERM_SUGGESTIONS,
        cutoff=_PERM_SUGGEST_CUTOFF,
    )


def _render_perm_vocabulary(lang: str) -> str:
    """The full settable vocabulary, grouped (RR-4 #45).

    Legacy printed it as one comma-joined line of its own 24-key
    vocabulary (``sorted(RankPermissions.PERMISSIONS)``, bot.py:42296)
    and the port inherited the shape. Grouped, the same data answers
    "which of these does an economy admin need" at a glance. The 25th
    key here is the matrix twin ``can_remove_warn``, which legacy's
    vocabulary omits but its matrix stores — see
    :data:`core.ranks.PERMISSION_ALIASES` (#810 F-5).
    """
    lines: list[str] = []
    for category, keys in PERMISSION_CATEGORIES.items():
        lines.append(t(f"h_rankadm_cat_{category}", lang))
        lines.append(" · ".join(f"<code>{key}</code>" for key in keys))
    return "\n".join(lines)


def _render_perm_matrix_row(matrix: dict[int, dict[str, bool]], rank: int, lang: str) -> str:
    """One rank's merged row with ✅/❌ marks, grouped by category.

    Legacy listed the configured cells only (bot.py:42282-42287) — a
    rank with no overrides answered "нет настроенных прав" and told you
    nothing about what it COULD have. We render the whole vocabulary, in
    the category order of :data:`PERMISSION_CATEGORIES`, so ``/perm
    list`` doubles as the vocabulary reference the ``set`` error points
    at. A granted/total counter makes "did that write land" one glance.

    Marks are read through :func:`storage_permission`, so the unwarn
    twin's two rows always agree: ``can_unwarn`` has no storage of its
    own, and rendering it off its own (never-written) key showed ❌ next
    to a granted ``can_remove_warn`` (#808). The counter runs over the
    RENDERED keys for the same reason — it summarises the marks below
    it, so the twin counts as the two rows it occupies.
    """
    perms = matrix.get(rank, {})
    granted = sum(1 for key in ORDERED_PERMISSION_KEYS if perms.get(storage_permission(key), False))
    lines = [
        t("h_rankadm_header", lang, rank=rank, name=rank_name(rank, lang)),
        t(
            "h_rankadm_granted",
            lang,
            granted=granted,
            total=len(ALL_PERMISSION_KEYS),
        ),
    ]
    for category, keys in PERMISSION_CATEGORIES.items():
        lines.append("")
        lines.append(t(f"h_rankadm_cat_{category}", lang))
        lines.extend(
            f"{'✅' if perms.get(storage_permission(perm_key), False) else '❌'}"
            f" <code>{perm_key}</code>"
            for perm_key in keys
        )
    return "\n".join(lines)


async def handle_perm(
    message: Message,
    settings: Settings,
    registry: EngineRegistry,
    lang: str,
) -> None:
    """/perm list <rank> | /perm set <rank> <permission> on|off.

    Legacy ``cmd_perm`` (bot.py:42248-42317), developer-gated.
    """
    if message.from_user is None:
        return
    if not settings.bot.is_developer(message.from_user.id):
        await message.reply(t("owner_only_short", lang))
        return

    parts = command_body(message).split()
    if len(parts) < 3:
        await message.reply(_perm_usage(lang))
        return

    action = parts[1].lower()
    if action not in ("list", "set"):
        # Legacy named the two actions back (bot.py:42317) instead of
        # re-printing the usage block; "list/set" is the shorter answer
        # to "what did I get wrong".
        await message.reply(t("h_rankadm_bad_action", lang))
        return

    rank = _parse_rank(parts[2])
    if rank is None:
        await message.reply(
            t("h_rankadm_bad_rank", lang, min=MIN_SETTABLE_RANK, max=MAX_SETTABLE_RANK)
        )
        return

    if action == "list":
        async with session_for(registry, DBName.MODERATION) as session:
            matrix = await RankRepo(session).merged_matrix()
        await message.reply(_render_perm_matrix_row(matrix, rank, lang))
        return

    # action == "set"
    if len(parts) < 5:
        await message.reply(_perm_usage(lang))
        return
    perm_key = parts[3].strip().lower()
    if perm_key not in ALL_PERMISSION_KEYS:
        # Legacy echoed the rejected key back (bot.py:42299) — the port
        # had dropped it, so an operator who mistyped one character got
        # a wall of 25 keys and no hint which of them they had meant.
        # Echo it (clipped and escaped: the token is user input landing
        # in an HTML message), offer the near-misses, then the grouped
        # vocabulary as the fallback reference.
        shown = perm_key[:_PERM_ECHO_CHARS]
        # Suggest against the CLIPPED token: a 4096-char paste cannot be
        # a typo of a 22-char key anyway, and this keeps the diffing
        # bounded by a constant instead of by what the operator sent.
        suggestions = _perm_suggestions(shown)
        body = t("h_rankadm_unknown_perm", lang, perm=html.escape(shown))
        if suggestions:
            body += "\n\n" + t(
                "h_rankadm_perm_did_you_mean",
                lang,
                keys=" · ".join(f"<code>{key}</code>" for key in suggestions),
            )
        body += "\n\n" + t("h_rankadm_perm_vocabulary", lang) + "\n"
        body += _render_perm_vocabulary(lang)
        await message.reply(body)
        return
    # Fold the vocabulary spelling onto the key that is actually read
    # at the enforcement seam. Without this ``/perm set 3 can_unwarn on``
    # wrote a cell nothing ever looks at and still answered "✅ Ранг…"
    # (#808). The echo below deliberately reports the FOLDED key: what
    # got written is what the operator needs to see.
    perm_key = storage_permission(perm_key)
    allowed = _parse_toggle(parts[4])
    if allowed is None:
        await message.reply(t("h_rankadm_bad_toggle", lang))
        return

    try:
        async with session_for(registry, DBName.MODERATION) as session:
            await RankRepo(session).set_permission(rank, perm_key, allowed)
    except Exception as exc:  # noqa: BLE001 — surface a clean error, log the cause
        log.warning("/perm set failed (rank={r}, perm={p}): {exc!r}", r=rank, p=perm_key, exc=exc)
        await message.reply(t("h_rankadm_save_fail", lang))
        return
    # AFTER the session closed, i.e. after the commit: clearing inside
    # the transaction lets a concurrent update re-fill the cache from
    # the not-yet-visible pre-write row for another full TTL (see the
    # rank_repo module docstring).
    clear_rank_matrix_cache()

    await message.reply(
        t(
            "h_rankadm_set_ok",
            lang,
            rank=rank,
            permission=perm_key,
            value=on_off_text(allowed, lang),
        )
    )
    log.bind(actor=message.from_user.id, rank=rank, perm=perm_key, allowed=allowed).info(
        "/perm set applied"
    )


# -- /cmdcfg --------------------------------------------------------------------


def _render_cmdcfg_list(
    overrides: dict[str, int], lang: str, *, categories: tuple[str, ...]
) -> list[str]:
    """The whole catalog, grouped by category, with the 0..6 legend.

    Legacy rendered every category with its numeric ids and current
    ranks (bot.py:43153-43171); the port had shrunk this to "only the
    rows you already changed", which is precisely the view an operator
    does *not* need — you look at ``list`` to find out what a command's
    access is, and a command you have never touched is exactly the one
    you have to look up.

    Kept from the port: overridden rows are marked, so the "what did I
    change" question the shrunken version answered is still answerable
    at a glance. Legacy had no such marker.

    Paginated for the same reason ``/filter_list`` and ``/aliases`` are.
    The catalog alone is ~1500 parsed characters today, one line per
    command and growing with every new one; on top of it the "outside
    the catalog" section adds a line per override on a key the catalog
    never listed, and ``set`` accepts ANY well-formed key by design
    (:data:`_COMMAND_KEY_RE`), so every mistyped command an operator
    ever pinned is in that section forever. A few dozen of them at the
    64-character key cap push the reply past 4096, Telegram refuses it,
    and what the operator loses is the one view that would have told
    them what to reset.
    """
    lines = ["", t("h_cmdcfg_legend", lang)]
    for category in categories:
        lines.extend(("", t(f"h_cmdcfg_cat_{category}", lang)))
        for entry in entries_in_category(category):
            overridden = entry.key in overrides
            lines.append(
                t(
                    "h_cmdcfg_row",
                    lang,
                    id=entry.id,
                    cmd=entry.key,
                    rank=overrides.get(entry.key, entry.default_rank),
                    mark=_OVERRIDE_MARK if overridden else "",
                )
            )
    # Overrides on commands the legacy catalog never listed have no row
    # of their own above — and without this section they would be
    # completely invisible: the footer would count them, no line would
    # show them, and `reset all` would be the only way to find out they
    # existed. `set` accepts such keys by design, so `list` must too.
    if len(categories) == len(COMMAND_CATEGORIES):
        extra = sorted(key for key in overrides if command_entry(key) is None)
        if extra:
            lines.extend(("", t("h_cmdcfg_cat_other", lang)))
            lines.extend(
                t(
                    "h_cmdcfg_row_uncataloged",
                    lang,
                    cmd=html.escape(key),
                    rank=overrides[key],
                    mark=_OVERRIDE_MARK,
                )
                for key in extra
            )

    pages = paginate_lines(
        t("h_cmdcfg_list_header", lang),
        lines,
        more_line=lambda left: t("h_cmdcfg_list_more", lang, count=left),
    )
    footer = "\n".join(
        (
            "",
            t("h_cmdcfg_changed", lang, count=len(overrides))
            if overrides
            else t("h_cmdcfg_all_default", lang),
            t("h_cmdcfg_list_footer", lang),
        )
    )
    # The footer is appended after pagination rather than fed through it
    # because it carries ``reset all`` — the operator's only way out of
    # a catalog bloated by phantom overrides. Inside ``lines`` it would
    # be the first thing dropped in exactly the case that needs it, the
    # one where the tail was replaced by "…and N more".
    if parsed_length(pages[-1]) + parsed_length(footer) + 1 <= PAGE_BUDGET:
        pages[-1] = f"{pages[-1]}\n{footer}"
    else:
        pages.append(footer.lstrip("\n"))
    return pages


def _render_cmdcfg_show(key: str, current: int, lang: str) -> str:
    """The command card: id, aliases, category, current vs default.

    Two shapes, because the port deliberately accepts commands the
    legacy catalog never listed (module docstring) — those have no id,
    no category and no alias list to show, and inventing "—" for three
    fields reads worse than a card that simply omits them.
    """
    entry = command_entry(key)
    if entry is None:
        return t(
            "h_cmdcfg_show_uncataloged",
            lang,
            cmd=key,
            current=current,
            default=default_min_rank(key),
        )
    card = t(
        "h_cmdcfg_show",
        lang,
        cmd=entry.key,
        id=entry.id,
        category=t(f"h_cmdcfg_cat_{entry.category}", lang),
        aliases=", ".join(f"/{alias}" for alias in (entry.key, *synonym_aliases(entry))),
        current=current,
        default=entry.default_rank,
    )
    if not entry.subcommands:
        return card
    # "How to call it" used to list these too, so an admin reading the
    # card for /cpc was told that /cpc_cancel was another way to say
    # /cpc (#164). They belong on their own line — and with the fact
    # that matters to whoever is about to touch the switch: one rank
    # covers the row, so turning /cpc off takes /cpc_cancel with it.
    return card + t(
        "h_cmdcfg_show_subcmds",
        lang,
        cmd=entry.key,
        subcommands=", ".join(f"/{token}" for sub in entry.subcommands for token in sub.aliases),
    )


async def _resolve_ref_or_reply(message: Message, raw: str, lang: str) -> str | None:
    """``<id|cmd>`` → catalog key, replying with the reason on failure.

    The numeric branch is what the port was missing: ``/cmdcfg set 67 3``
    is documented (legacy ``resolve_command_entry``, bot.py:42799-42801)
    and used to slip through :func:`_parse_command_key` as the *literal*
    key ``"67"``, quietly writing an override for a command that does
    not exist while the admin believed they had restricted ``/ban``.
    """
    token = raw.strip().lstrip("/").lower()
    if is_int_token(token):
        # An all-digit token is ALWAYS an id reference, never a command
        # name — falling through to the name branch on, say, a 13-digit
        # typo would pin an override onto a phantom key again.
        entries = entries_for_id(int(token)) if len(token) <= _MAX_ID_DIGITS else ()
        if not entries:
            await message.reply(t("h_cmdcfg_unknown_cmd", lang))
            return None
        if len(entries) > 1:
            # Unreachable against today's catalog — #121 renumbered
            # legacy's three duplicated ids and
            # ``test_catalog_ids_are_unique`` holds the line. Kept as
            # the fail-safe for a future row that reintroduces one:
            # legacy's COMMAND_BY_ID silently kept the last match, and
            # asking beats writing an override onto a command the admin
            # never named.
            await message.reply(
                t(
                    "h_cmdcfg_ambiguous_id",
                    lang,
                    id=int(token),
                    cmds=", ".join(f"/{entry.key}" for entry in entries),
                )
            )
            return None
        return entries[0].key

    key = _parse_command_key(raw)
    if key is None:
        await message.reply(t("h_cmdcfg_unknown_cmd", lang))
    return key


async def handle_cmdcfg(
    message: Message,
    settings: Settings,
    registry: EngineRegistry,
    lang: str,
) -> None:
    """/cmdcfg list | show <cmd> | set <cmd> <0-6> | reset <cmd|all>.

    Legacy ``cmd_cmdcfg`` (bot.py:43125-43237), developer-gated.
    """
    if message.from_user is None:
        return
    if not settings.bot.is_developer(message.from_user.id):
        await message.reply(t("owner_only_short", lang))
        return

    parts = command_body(message).split()
    action = parts[1].lower() if len(parts) > 1 else ""

    if action == "list":
        categories = COMMAND_CATEGORIES
        if len(parts) > 2:
            requested = parts[2].strip().lower()
            if requested not in COMMAND_CATEGORIES:
                await message.reply(
                    t(
                        "h_cmdcfg_bad_category",
                        lang,
                        category=html.escape(requested[:32]),
                        known=", ".join(f"<code>{name}</code>" for name in COMMAND_CATEGORIES),
                    )
                )
                return
            categories = (requested,)
        async with session_for(registry, DBName.MODERATION) as session:
            overrides = await RankRepo(session).command_overrides()
        pages = _render_cmdcfg_list(overrides, lang, categories=categories)
        await message.reply(pages[0])
        # Only the first page quotes the command, same as /filter_list.
        for page in pages[1:]:
            await message.answer(page)
        return

    if action == "show":
        if len(parts) < 3:
            await message.reply(t("h_cmdcfg_usage", lang))
            return
        key = await _resolve_ref_or_reply(message, parts[2], lang)
        if key is None:
            return
        async with session_for(registry, DBName.MODERATION) as session:
            overrides = await RankRepo(session).command_overrides()
        current = overrides.get(key, default_min_rank(key))
        await message.reply(_render_cmdcfg_show(key, current, lang))
        return

    if action == "set":
        if len(parts) < 4:
            await message.reply(t("h_cmdcfg_usage", lang))
            return
        key = await _resolve_ref_or_reply(message, parts[2], lang)
        if key is None:
            return
        min_rank = _parse_rank(parts[3])
        if min_rank is None:
            await message.reply(
                t("h_cmdcfg_bad_rank", lang, min=MIN_SETTABLE_RANK, max=MAX_SETTABLE_RANK)
            )
            return
        try:
            async with session_for(registry, DBName.MODERATION) as session:
                repo = RankRepo(session)
                if min_rank == default_min_rank(key):
                    # Value == catalog default → drop the override row
                    # (legacy set_command_required_rank, bot.py:42840-42841).
                    await repo.reset_command_override(key)
                else:
                    await repo.set_command_override(key, min_rank)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "/cmdcfg set failed (cmd={c}, rank={r}): {exc!r}",
                c=key,
                r=min_rank,
                exc=exc,
            )
            await message.reply(t("h_cmdcfg_save_fail", lang))
            return
        # After the commit, never inside it — this is the kill switch,
        # and a stale re-fill would keep a disabled command running for
        # a further TTL (see the rank_repo module docstring).
        clear_command_override_cache()
        if min_rank == DISABLED_MIN_RANK:
            await message.reply(t("h_cmdcfg_set_disabled", lang, cmd=key))
        else:
            await message.reply(t("h_cmdcfg_set_ok", lang, cmd=key, rank=min_rank))
        log.bind(actor=message.from_user.id, cmd=key, min_rank=min_rank).info("/cmdcfg set applied")
        return

    if action == "reset":
        if len(parts) < 3:
            await message.reply(t("h_cmdcfg_usage", lang))
            return
        target = parts[2].lower()
        # Resolved BEFORE the session opens, the way the ``set`` branch
        # above already does it: it calls ``_resolve_ref_or_reply`` and
        # only then enters ``session_for``. The resolver
        # replies on failure, and a Telegram round-trip must not run
        # while we hold the MODERATION writer connection — a slow or
        # retrying API call would block every other moderation write for
        # its duration (#749). ``None`` means "reset everything": the one
        # target that needs no lookup, and the flag both the repo call
        # and the reply below branch on.
        # (No annotation: ``key`` is already bound as ``str | None`` by
        # the ``show``/``set`` branches above, and mypy reads a repeat
        # annotation in the same function scope as a redefinition.)
        key = None
        if target != "all":
            key = await _resolve_ref_or_reply(message, target, lang)
            if key is None:
                return
        # Both replies are deliberately OUTSIDE the session block: the
        # cache clear has to land after the commit, and neither reply
        # may run before it (see the rank_repo module docstring).
        reset_count: int | None = None
        try:
            async with session_for(registry, DBName.MODERATION) as session:
                repo = RankRepo(session)
                if key is None:
                    reset_count = await repo.reset_all_command_overrides()
                else:
                    # Legacy replied "reset" whether or not an override
                    # row existed (bot.py:43229-43235) — same here.
                    await repo.reset_command_override(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("/cmdcfg reset failed (target={t}): {exc!r}", t=target, exc=exc)
            await message.reply(t("h_cmdcfg_save_fail", lang))
            return
        clear_command_override_cache()
        if key is None:
            await message.reply(t("h_cmdcfg_reset_all_ok", lang, count=reset_count))
            log.bind(actor=message.from_user.id, count=reset_count).info(
                "/cmdcfg reset all applied"
            )
            return
        await message.reply(t("h_cmdcfg_reset_ok", lang, cmd=key))
        log.bind(actor=message.from_user.id, cmd=key).info("/cmdcfg reset applied")
        return

    await message.reply(t("h_cmdcfg_usage", lang))


# -- /rank ----------------------------------------------------------------------


async def handle_rank(
    message: Message,
    settings: Settings,
    registry: EngineRegistry,
    lang: str,
) -> None:
    """/rank [reply] — read-only rank card (legacy ``your_rank`` copy)."""
    if message.from_user is None:
        return
    reply = message.reply_to_message
    target = reply.from_user if reply is not None and reply.from_user is not None else None

    ranks = RankService(registry, settings)
    in_group = message.chat.type in GROUP_TYPES
    if target is None or target.id == message.from_user.id:
        rank = await ranks.get_rank(message.from_user.id)
        await message.reply(t("your_rank", lang, rank=rank_name(rank, lang, in_group=in_group)))
        return

    rank = await ranks.get_rank(target.id)
    name = html.escape(target.full_name or str(target.id))
    await message.reply(
        t(
            "h_rankadm_rank_of",
            lang,
            name=name,
            rank=rank_name(rank, lang, in_group=in_group),
        )
    )


# -- router factory ----------------------------------------------------------------


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Build the rank-management router (/perm, /cmdcfg, /rank).

    No session middleware — see module docstring. Aliases are the
    legacy ones: /rankperm (bot.py:42248), /cmdaccess (bot.py:43125).
    """
    router = Router(name="rank_admin")

    async def _perm(message: Message, lang: str) -> None:
        await handle_perm(message, settings, registry, lang)

    async def _cmdcfg(message: Message, lang: str) -> None:
        await handle_cmdcfg(message, settings, registry, lang)

    async def _rank(message: Message, lang: str) -> None:
        await handle_rank(message, settings, registry, lang)

    router.message.register(_perm, Command("perm", "rankperm", ignore_case=True))
    router.message.register(_cmdcfg, Command("cmdcfg", "cmdaccess", ignore_case=True))
    router.message.register(_rank, Command("rank", "ранг", ignore_case=True))
    return router
