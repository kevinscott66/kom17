"""T-024 backstop: every ported command must stay registered on the main router.

This is the regression "tripwire" gate for T-011 (delete ``legacy/bot.py``).
The per-handler e2e suites under ``tests/e2e/handlers/`` already prove
behaviour; the whole-tree ``tests/integration/test_main_router_wiring.py``
already feeds one update per ported command and asserts non-``UNHANDLED``.

What's missing — and what this file owns — is a **route-existence audit**
that does not depend on schemas, middlewares, or i18n state. We introspect
the dispatcher's :class:`aiogram.filters.Command` filters and assert that
an explicit, hand-curated whitelist of expected command names is fully
present. The list groups the surface by category so a future stage that
forgets to ``include_router(...)`` something visible (or accidentally
drops an alias when refactoring) trips one assertion per missing token
with a category-tagged message.

Why a separate file rather than extending ``test_main_router_wiring.py``:

* That file's tests are *parameterised over feed_update* — every row pays
  for one full middleware pass, a DB roundtrip for the session-scoped
  handlers, and an outgoing-capture monkeypatch. Adding 200+ rows just
  to count names would 30x the runtime of an already-slow integration
  test.
* Introspection has different failure semantics: a missing alias here
  is a wiring/refactor bug surfaced as ``KeyError``-shaped output, not
  a silent fallthrough. Keeping the two layers distinct means a future
  reader sees the difference between "command doesn't route" and
  "command was never registered".

Maintaining this list: when a new command is ported, add its canonical
name (and every legacy alias it claims) to the appropriate category.
When a command is intentionally NOT ported (kept in legacy by design),
add it to ``_INTENTIONALLY_LEGACY`` with a one-line reason. Both lists
combined must cover every legacy command — the final ``test_surface_completeness``
assertion proves that gap == 0 so a forgotten case can't slip past
unnoticed.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# Categories mirror the legacy ``bot.py`` grouping comments. Each entry
# is the canonical command name OR an alias — every alias gets its own
# entry so the diff against legacy is exact. When a category is fully
# ported, every alias from ``bot.py`` lives here.
_PORTED: dict[str, frozenset[str]] = {
    # RR-6 #115: the ``kom_*`` spellings below were parked in
    # ``_INTENTIONALLY_LEGACY`` as "canonical is ported, the alias can
    # wait". That reasoning held only while legacy still answered them —
    # it doesn't run any more, and the generated command index advertises
    # the rows, so the alias answered with silence. Registered on their
    # handlers and moved here.
    "start": frozenset({"start", "kom_start"}),
    "help": frozenset({"help", "h", "commands", "kom_help"}),
    "admin_help": frozenset({"admin_help", "owner_help", "admin"}),
    # #2003: same reasoning as the ``kom_*`` note above — the short name
    # was left to a bridge that T-011 removed, so it answered with
    # silence. Both spellings sit behind the same developer gate.
    "admin_deploy": frozenset({"admin_deploy", "deploy"}),
    "language": frozenset({"lang", "language", "язык", "kom_lang", "settings", "настройки"}),
    "time": frozenset({"time", "время", "time_msk", "kom_time"}),
    "timezone": frozenset({"timezone", "часовой_пояс", "tz", "kom_timezone"}),
    "weather": frozenset({"weather", "погода", "kom_weather"}),
    # CMD-4: /forecast — multi-day card over the extended WeatherService.
    "forecast": frozenset({"forecast", "прогноз", "kom_forecast"}),
    # RR-6 #74: /city — the saved-city setter both of the above read
    # from. ``location`` is in this set but NOT in the legacy token dump
    # the sibling tests check: legacy's own catalog advertised it
    # (bot.py:42347) while registering nothing, so it is new surface, not
    # a ported alias.
    "city": frozenset({"city", "город", "location", "kom_city"}),
    "calc": frozenset({"calc", "калькулятор", "kom_calc"}),
    "games": frozenset({"dice", "кубик", "roll", "flip", "монетка", "kom_flip"}),
    # Wave1 L-46: /setrules admin write form joins the read-side /rules.
    "rules": frozenset(
        {"rules", "правила", "kom_rules", "setrules", "установить_правила", "kom_setrules"}
    ),
    "heartbeat": frozenset({"ping", "kom_ping", "botcheck", "alive", "kom_botcheck"}),
    "profile": frozenset(
        {"profile", "info", "инфо", "whoami", "me", "kom_whoami", "kom_profile", "профиль"}
    ),
    "stats": frozenset({"stats", "статистика", "group_stats"}),
    "groupstats": frozenset(
        {"groupstats", "статистика_группы", "group_treasury", "казна_группы", "kom_groupstats"}
    ),
    "top": frozenset({"top", "kom_top"}),
    "economy": frozenset({"balance", "bal", "баланс", "kom_balance", "kom_bal"}),
    "shop": frozenset(
        {
            "shop",
            "kom_shop",
            "магазин",
            "buy",
            "купить",
            "kom_buy",
            "inventory",
            "inv",
            "инвентарь",
        }
    ),
    "daily": frozenset({"daily", "kom_daily"}),
    "donate": frozenset({"donate", "донат", "kom_donate"}),
    "donaters": frozenset({"donaters", "донатеры", "kom_donaters"}),
    "mydonates": frozenset({"mydonates", "мои_донаты"}),
    # A-07/A-08/A-09: read-only ports lifted out of _INTENTIONALLY_LEGACY.
    # /achievements (+/ach) renders the historical achievement card;
    # /chatstats (+/cstats) the group activity card; /rate + /convert the
    # COM→currency converter. Plain /currency + /crypto stay in legacy.
    "achievements": frozenset({"achievements", "ach"}),
    "chatstats": frozenset({"chatstats", "cstats", "chatinfo"}),
    # CMD-2: /topactive — window-parametrised message leaderboard.
    "topactive": frozenset({"topactive", "top_activity"}),
    "currency": frozenset(
        {
            "rate",
            "курс",
            "курсы",
            "convert",
            "конверт",
            "конвертация",
            # CMD-3: /crypto + read-only /currency listing over CurrencyService.
            "crypto",
            "крипта",
            "криптовалюта",
            "kom_crypto",
            "currency",
            "валюта",
            "kom_currency",
        }
    ),
    # A-04: cross-group donations leaderboard. ``/top_groups`` /
    # ``/rating_groups`` share the handler; ``/rating`` is private-only
    # (group → refusal). ``/top`` + ``/kom_top`` stay with the messages
    # ladder (see ``top``) — a deliberate divergence from legacy.
    # Wave1 L-38: donations-rating write-side admin toggles + dev recalc.
    "rating": frozenset(
        {
            "rating",
            "рейтинг",
            "top_groups",
            "rating_groups",
            "rating_include",
            "рейтинг_включить",
            "rating_exclude",
            "рейтинг_исключить",
            "rating_recalc",
            "рейтинг_пересчет",
        }
    ),
    # Wave1 L-24: dev-gated /give (/выдать) coin grant (new — not in legacy
    # snapshot; legacy granted coins via debug/economy admin paths).
    "admin_give": frozenset({"give", "выдать"}),
    "send": frozenset({"send"}),
    "withdraw_status": frozenset({"withdraw_status"}),
    "withdraw": frozenset({"withdraw", "вывод"}),
    "referral": frozenset({"referral", "реферал", "реферальная_ссылка"}),
    "referrals": frozenset({"referrals", "рефералы", "мои_рефералы"}),
    "commission": frozenset({"commission", "комиссии", "мои_комиссии"}),
    # Wave2 L-62/63/76: AI context-injection adds mode/memory/Kom-session
    # commands on top of the /ai family.
    "ai": frozenset(
        {
            "ai",
            "ии",
            "ask",
            "chat",
            "gpt",
            "kom_ai",
            "reset",
            "mode",
            "режим",
            "kom_mode",
            "kom",
            "enter_kom",
            "войти_в_ком",
            "exit_kom",
            "выйти_из_ком",
        }
    ),
    "support": frozenset(
        {
            "support",
            "ticket",
            "my_tickets",
            "мои_обращения",
            "мои_тикеты",
            "мои_обращения_help",
            "ticket_reply",
            "ticket_close",
            "feedback",
            "отзыв",
            "kom_feedback",
            "faq",
            "вопросы",
            "kom_faq",
        }
    ),
    # #26: coin-code vouchers — /check (claim/info) + /create_check (dev).
    # Took over /check + /чек from the support stub.
    # Wave1 L-30: /check_create FSM create flow joins the claim/info surface.
    "checks": frozenset(
        {"check", "чек", "create_check", "создать_чек", "check_create", "чек_создать"}
    ),
    # Wave1 L-96: /promo redeem + /promo_create mint (new feature; legacy used
    # checks for gift codes, so these tokens are NOT in the legacy snapshot).
    "promo": frozenset({"promo", "промокод", "promo_create", "создать_промокод"}),
    "faq2": frozenset({"faq2", "faq_2"}),
    # New surface, not a legacy port: the public offer / privacy policy /
    # support card an acquiring bank requires to be user-reachable.
    "legal": frozenset({"legal", "terms", "privacy", "offer", "docs"}),
    "jokes": frozenset({"joke18", "шутка18", "анекдот18", "kom_joke18"}),
    # CMD-1: offline static-pool ports of the SFW /joke and /quote
    # content commands (mirrors /joke18). Both private-only.
    "sfw_jokes": frozenset({"joke", "шутка", "анекдот", "kom_joke"}),
    "quote": frozenset({"quote", "цитата", "kom_quote"}),
    "duel": frozenset({"duel", "дуэль"}),
    "duel_stats": frozenset({"duel_stats"}),
    # A-10: /roulette — single-player RUSSIAN ROULETTE over economy.db.
    # Legacy also had /kom_roulette, but the parity snapshot lists only
    # ``roulette``, so just the canonical is registered.
    "roulette": frozenset({"roulette"}),
    # AUD-2: /pvp_coin + /pvp_dice — PvP escrow stake games.
    "pvp_coin": frozenset({"pvp_coin", "пвп_монета"}),
    "pvp_dice": frozenset({"pvp_dice", "пвп_кости"}),
    "rps": frozenset({"cpc", "кнб", "knb", "cpc_cancel", "кнб_отмена"}),
    "marriage": frozenset(
        {
            "marry",
            "брак",
            "жениться",
            "marry_accept",
            "принять_брак",
            "marry_decline",
            "отклонить_брак",
            "divorce",
            "развод",
            "breakup",
            "расстаться",
            "relationship",
            "rel",
            "отношения",
            "в_отношениях",
            # Wave1 L-03/04/05/06/07: second-tier marriage command surface.
            "marriage",
            "my_marriage",
            "брак_статус",
            "marry_top_on",
            "marry_top_off",
            "брак_рейтинг_вкл",
            "брак_рейтинг_выкл",
            "marry_extend",
            "брак_продлить",
            "marry_auto_divorce",
            "брак_режим_развода",
            "marry_other",
            "твой_брак",
        }
    ),
    "relations": frozenset({"marriages", "браки", "пары", "relations", "отношения_список", "отны"}),
    # FEAT-RP: /rp_commands action-discovery list (+ ru alias). The
    # prefixed RP-action verbs (.обнять etc.) are NOT slash commands —
    # they route via a parse filter, not a Command(...) — so only the
    # discovery command appears in the slash-command surface.
    "rp": frozenset({"rp_commands", "рп_команды"}),
    # FEAT-COUPLE-ACT: /activities (+ ru alias) — paid joint-activity
    # menu. Legacy reached this via an inline button on the relationship/
    # marriage card, not a slash command, so these tokens are NEW (not in
    # the legacy snapshot). They live here so the ported-surface audit
    # registers them; the do-activity click is a pure callback (no entry
    # needed). The bonus tokens carry no legacy-coverage obligation —
    # ``test_surface_coverage_includes_every_legacy_command`` only checks
    # that every LEGACY command is classified, not that every ported
    # token is legacy.
    "couple_activities": frozenset({"activities", "совместные"}),
    # Wave2 L-47/L-52/L-43/L-57: rank-independent moderation (live-TG-admin).
    "clear": frozenset({"clear", "очистить", "очистка"}),
    "wordfilter": frozenset(
        {
            "filter_add",
            "фильтр_добавить",
            "filter_remove",
            "фильтр_удалить",
            "filter_list",
            "фильтр_список",
        }
    ),
    "modcfg": frozenset({"modcfg", "модконфиг"}),
    # Wave2 batch7: per-group dynamic aliases (L-60), treasury payout
    # (L-41), advertiser-request funnel (L-61).
    "group_aliases": frozenset({"alias", "aliases"}),
    "group_pay": frozenset({"group_pay", "выплата_из_казны"}),
    "ads": frozenset({"ad", "ads", "reklama"}),
    # EPIC ranks (R3): TG-admin DM self-promotion.
    "staff_me": frozenset({"staff_me"}),
    # EPIC ranks (R2): permission/command-access management + rank card.
    # EPIC P2P: /p2p marketplace entry (new-pipeline command; legacy reached
    # the market via the withdraw menu only — no legacy-coverage obligation).
    # Tail sweep: discovery + quota + voice alias + challenge-answer commands.
    "games_menu": frozenset({"games", "игры", "kom_games"}),
    "ai_limits": frozenset({"ai_limits"}),
    "voice_settings_alias": frozenset({"voice_settings_ru"}),
    "challenge_commands": frozenset({"accept", "decline", "принять", "отклонить"}),
    # Phase A: top-up menu + owner broadcast (new-pipeline commands).
    "topup": frozenset({"topup", "buy_coins", "пополнить"}),
    "broadcast": frozenset({"broadcast"}),
    "p2p": frozenset({"p2p", "п2п"}),
    # Ranks wave 2: /groupadmin gateway + /transfer_rights ownership move.
    "groupadmin": frozenset({"groupadmin", "group_admin", "управлениегруппой"}),
    "transfer_rights": frozenset({"transfer_rights", "передать_права"}),
    "rank_admin": frozenset({"perm", "rankperm", "cmdcfg", "cmdaccess", "rank", "ранг"}),
    "welcome_config": frozenset(
        {
            "setwelcome",
            "set_welcome",
            "приветствие_текст",
            "welcome_off",
            "приветствие_выкл",
            "welcome_on",
            "приветствие_вкл",
            "welcome_test",
            "приветствие_тест",
        }
    ),
    "moderation": frozenset(
        {
            # #116: the positive halves got their ``kom_*`` spelling here
            # too — the guide documents all of them, and the negative
            # halves had it from day one.
            "ban",
            "бан",
            "kom_ban",
            "kick",
            "кик",
            "kom_kick",
            "mute",
            "мут",
            "kom_mute",
            "unmute",
            "размут",
            "kom_unmute",
            "unban",
            "разбан",
            "kom_unban",
            "warn",
            "варн",
            "предупреждение",
            "kom_warn",
            "unwarn",
            "снять_варн",
            "разварн",
            "снять_предупреждение",
            "kom_unwarn",
            "warnings",
            "предупреждения",
            "варны",
            "pin",
            "закрепить",
            "kom_pin",
            "unpin",
            "открепить",
            "kom_unpin",
            "fine",
            "штраф",
            "penalty",
        }
    ),
    "cancel": frozenset({"cancel"}),
    "mygroups": frozenset({"mygroups", "мои_группы"}),
    "nick": frozenset({"nick", "setnick", "ник", "никнейм"}),
    "admin_withdrawals": frozenset({"admin_withdrawals"}),
    "vip": frozenset(
        {
            "vip",
            "vip_shop",
            "emojis",
            "эмодзи",
            "emoji_set",
            "emoji_buy",
            "emoji_preview",
            "voice",
            "голос",
            "voice_settings",
            "voice_stats",
            "voice_vip",
            "голос_вип",
        }
    ),
}

# Commands that exist in ``legacy/bot.py`` but are intentionally NOT
# ported. Each line is annotated with the reason so the gap is auditable.
# These are EXPECTED to fall through to legacy under the strangler bridge
# until T-011 — after T-011 they will simply be unrecognised, which the
# pre-cutover-checklist memo flags as acceptable (admin/dev-only or
# deprecated). If you port one of these, MOVE the entry to ``_PORTED``;
# do not delete it from this file silently.
_INTENTIONALLY_LEGACY: dict[str, str] = {
    # Admin / owner-only debug + ops — moved to per-feature admin_* set
    "set_admin_password": "owner-only bootstrap; staying in legacy",
    # #2003: ``deploy`` used to sit here, on the same "unrecognised after
    # T-011 is acceptable" reading as its neighbours. It is not the same
    # case. ``/reload``, ``/logs``, ``/backup`` have no ported twin, so
    # silence is simply the absence of a feature; ``/deploy`` has one,
    # and ``handlers/admin/deploy_hint.py`` exists precisely because an
    # operator who types the deploy command and hears nothing concludes
    # the deploy is wedged. Moved to _PORTED["admin_deploy"].
    "reload": "owner-only ops shortcut; staying in legacy",
    "maintenance": "owner-only toggle; staying in legacy",
    "logs": "owner-only ops; deferred",
    "backup": "owner-only ops; deferred",
    "botstats": "owner-only; admin_botstats covers the new equivalent",
    "sql": "owner-only debug REPL; intentionally never ported",
    "sql_logs": "owner-only debug; deferred",
    "test_emoji": "dev-only smoke; intentionally not ported",
    "test_daily": "dev-only smoke; intentionally not ported",
    "test_logs": "dev-only smoke; intentionally not ported",
    "debug": "dev-only; deferred",
    "debug_balance": "dev-only; deferred",
    "debug_economy": "dev-only; deferred",
    "debug_vip": "dev-only; deferred",
    "reset_my_balance": "dev-only utility; staying in legacy",
    "сброс_баланса": "alias of /reset_my_balance",
    "check_groups": "dev-only audit; admin_check_groups covers prod-side",
    "check_rates": "dev-only audit; deferred",
    "clear_cache": "dev-only ops; deferred",
    "clearlogs": "dev-only ops; deferred",
    "rate_stats": "owner-only; admin_rate_stats covers the new equivalent",
    "version_check": "dev-only; deferred",
    "shop_prices": "owner-only; admin_shop_prices covers the new equivalent",
    "add_required_chat": "owner-only; deferred",
    "dev": "dev menu; staying in legacy",
    "developer": "alias of /dev",
    # Per-group config — large surface, kept in legacy until config rewrite
    "cfg_button": "group-config FSM; deferred",
    "setbutton": "alias of /cfg_button",
    # Wave2 L-43: /modcfg per-group moderation config now PORTED.
    # Wave1 L-46: /setrules write form now PORTED → moved to _PORTED["rules"].
    # Wave2 L-47: /clear now PORTED → moved to _PORTED["clear"].
    # Wave1 L-03/04/05/06/07: second-tier marriage surface now PORTED →
    # moved to _PORTED["marriage"].
    # Games — extended set staying in legacy until games rewrite
    # Finance utilities — currency / crypto helpers, low-traffic, deferred
    # Misc utilities — low-traffic, kept in legacy
    # RR-6 #74: /city + its aliases now PORTED → moved to _PORTED["city"].
    "chatpreview": "group-preview admin tool; deferred",
    # #252(16): ``penalty`` used to sit here as "covered via /fine
    # canonical". It was not covered — an alias nobody registers is
    # an alias that answers nobody. It is registered now, so the row
    # moved into _PORTED["ban"] alongside "fine" and "штраф".
    # #115/#116: this block used to park the ``kom_*`` aliases of ported
    # commands as "low-impact, the canonical works". #115 registered the
    # five the *command catalog* advertised; #116 registered the nine the
    # *prose guide* advertises — which the #115 note wrongly claimed were
    # "advertised nowhere", having looked only at ``core/ranks``. The
    # lesson is in the guard now rather than in a comment: nothing may
    # sit here while any user-facing surface prints it, and
    # ``test_copy_command_references`` reads both surfaces.
}


def _all_ported_tokens() -> set[str]:
    out: set[str] = set()
    for tokens in _PORTED.values():
        out.update(tokens)
    return out


@pytest.mark.parametrize(
    ("category", "tokens"),
    sorted(_PORTED.items()),
    ids=sorted(_PORTED.keys()),
)
def test_ported_category_fully_registered(
    category: str, tokens: frozenset[str], registered_commands: set[str]
) -> None:
    """Every alias of a ported command group must be registered.

    If this fails for a single token, the failure tag points straight at
    the alias that went missing — usually a refactor dropped a
    ``Command("x", "y")`` argument or a ``router.include`` call.
    """
    missing = sorted(tokens - registered_commands)
    assert not missing, (
        f"category {category!r} lost aliases: {missing}. "
        "Either re-add them to the handler's Command(...) filter, or "
        "move them to _INTENTIONALLY_LEGACY with a reason."
    )


def test_no_intentionally_legacy_token_is_silently_ported(
    registered_commands: set[str],
) -> None:
    """If a token in ``_INTENTIONALLY_LEGACY`` shows up registered, the
    table is stale — likely someone ported the command without moving
    its row to ``_PORTED``. Surface that mismatch so the audit list
    stays the source of truth.
    """
    accidentally_ported = sorted(set(_INTENTIONALLY_LEGACY) & registered_commands)
    assert not accidentally_ported, (
        f"these commands are registered but listed as intentionally-legacy: "
        f"{accidentally_ported}. Move the rows to _PORTED."
    )


def test_surface_coverage_includes_every_legacy_command() -> None:
    """The combined ``_PORTED`` + ``_INTENTIONALLY_LEGACY`` set must
    cover every command the legacy ``bot.py`` registers. A new entry in
    legacy (rare — legacy is frozen) or, more likely, a typo here that
    drops a token would otherwise let a real gap slip through unnoticed.

    The legacy command list is hard-coded below from the T-014 audit
    snapshot. If legacy genuinely changes (e.g. someone adds a row to
    bot.py during cutover), update this list AND the appropriate side
    of the audit — do not just delete the offender.
    """
    legacy_commands = frozenset(
        {
            # Snapshot of every command-name appearing in any
            # ``@bot.message_handler(commands=[...])`` decorator in
            # legacy/bot.py as of T-024. Sorted alphabetically (ASCII
            # then Cyrillic) for easy diffing if legacy ever changes.
            "ach",
            "achievements",
            "ad",
            "add_required_chat",
            "admin",
            "admin_help",
            "admin_withdrawals",
            "ads",
            "ai",
            "ai_limits",
            "alias",
            "aliases",
            "alive",
            "ask",
            "backup",
            "bal",
            "balance",
            "ban",
            "botcheck",
            "botstats",
            "breakup",
            "broadcast",
            "buy",
            "calc",
            "cancel",
            "cfg_button",
            "chat",
            "chatinfo",
            "chatpreview",
            "chatstats",
            "check",
            "check_groups",
            "check_rates",
            "city",
            "clear",
            "clear_cache",
            "clearlogs",
            "cmdaccess",
            "cmdcfg",
            "commands",
            "commission",
            "convert",
            "create_check",
            "crypto",
            "cstats",
            "currency",
            "daily",
            "debug",
            "debug_balance",
            "debug_economy",
            "debug_vip",
            "deploy",
            "dev",
            "developer",
            "dice",
            "divorce",
            "donate",
            "donaters",
            "duel",
            "duel_stats",
            "faq",
            "faq2",
            "faq_2",
            "feedback",
            "fine",
            "flip",
            "forecast",
            "games",
            "group_admin",
            "group_pay",
            "group_treasury",
            "groupadmin",
            "groupstats",
            "h",
            "help",
            "info",
            "inv",
            "inventory",
            "joke",
            "joke18",
            "kick",
            "kom_ai",
            "kom_bal",
            "kom_balance",
            "kom_ban",
            "kom_botcheck",
            "kom_buy",
            "kom_calc",
            "kom_city",
            "kom_crypto",
            "kom_currency",
            "kom_daily",
            "kom_donate",
            "kom_donaters",
            "kom_faq",
            "kom_feedback",
            "kom_flip",
            "kom_forecast",
            "kom_games",
            "kom_groupstats",
            "kom_help",
            "kom_joke",
            "kom_joke18",
            "kom_kick",
            "kom_lang",
            "kom_mute",
            "kom_pin",
            "kom_ping",
            "kom_profile",
            "kom_quote",
            "kom_rules",
            "kom_shop",
            "kom_start",
            "kom_time",
            "kom_timezone",
            "kom_top",
            "kom_unban",
            "kom_unmute",
            "kom_unpin",
            "kom_unwarn",
            "kom_warn",
            "kom_weather",
            "kom_whoami",
            "lang",
            "language",
            "logs",
            "maintenance",
            "marriage",
            "marriages",
            "marry",
            "marry_accept",
            "marry_auto_divorce",
            "marry_decline",
            "marry_extend",
            "marry_other",
            "marry_top_off",
            "marry_top_on",
            "me",
            "modcfg",
            "mute",
            "my_marriage",
            "mydonates",
            "mygroups",
            "nick",
            "owner_help",
            "penalty",
            "perm",
            "pin",
            "ping",
            "profile",
            "pvp_coin",
            "pvp_dice",
            "quote",
            "rankperm",
            "rate",
            "rate_stats",
            "rating",
            "rating_groups",
            "referral",
            "referrals",
            "reklama",
            "rel",
            "relations",
            "relationship",
            "reload",
            "reset_my_balance",
            "roll",
            "roulette",
            "rp_commands",
            "rules",
            "send",
            "set_admin_password",
            "setbutton",
            "setnick",
            "setrules",
            "settings",
            "shop",
            "shop_prices",
            "sql",
            "sql_logs",
            "staff_me",
            "start",
            "stats",
            "support",
            "test_daily",
            "test_emoji",
            "test_logs",
            "ticket",
            "time",
            "time_msk",
            "timezone",
            "top",
            "top_activity",
            "top_groups",
            "topactive",
            "transfer_rights",
            "tz",
            "unban",
            "unmute",
            "unpin",
            "unwarn",
            "version_check",
            "voice_settings",
            "voice_settings_ru",
            "voice_vip",
            "warn",
            "warnings",
            "weather",
            "whoami",
            "withdraw",
            "withdraw_status",
            # Cyrillic aliases
            "анекдот",
            "анекдот18",
            "бан",
            "брак",
            "брак_продлить",
            "брак_режим_развода",
            "брак_рейтинг_вкл",
            "брак_рейтинг_выкл",
            "брак_статус",
            "браки",
            "в_отношениях",
            "валюта",
            "варн",
            "варны",
            "время",
            "вывод",
            "выплата_из_казны",
            "город",
            "донатеры",
            "жениться",
            "закрепить",
            "игры",
            "инфо",
            "казна_группы",
            "калькулятор",
            "кик",
            "комиссии",
            "конверт",
            "конвертация",
            "крипта",
            "криптовалюта",
            "кубик",
            "курс",
            "курсы",
            "мои_группы",
            "мои_донаты",
            "мои_комиссии",
            "мои_рефералы",
            "монетка",
            "мут",
            "настройки",
            "ник",
            "никнейм",
            "открепить",
            "отношения",
            "отношения_список",
            "отны",
            "очистить",
            "очистка",
            "пары",
            "погода",
            "правила",
            "предупреждения",
            "прогноз",
            "разбан",
            "разварн",
            "развод",
            "размут",
            "расстаться",
            "рейтинг",
            "реферал",
            "рефералы",
            "реферальная_ссылка",
            "рп_команды",
            "сброс_баланса",
            "снять_предупреждение",
            "статистика_группы",
            "твой_брак",
            "управлениегруппой",
            "установить_правила",
            "цитата",
            "часовой_пояс",
            "штраф",
            "шутка",
            "шутка18",
            "язык",
        }
    )

    audit_covered = _all_ported_tokens() | set(_INTENTIONALLY_LEGACY)
    uncovered = sorted(legacy_commands - audit_covered)
    assert not uncovered, (
        f"legacy commands not classified in the T-024 audit: {uncovered}. "
        "Add each to _PORTED or _INTENTIONALLY_LEGACY."
    )
