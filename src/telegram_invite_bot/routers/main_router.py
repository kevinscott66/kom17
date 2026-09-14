"""Top-level aiogram router — aggregates per-feature routers.

Each migrated handler module appends its router here; the dispatcher
wires this single tree. Routers that need extra DB engines (e.g.
``economy``) receive the :class:`EngineRegistry` directly so they can
attach a scoped middleware without forcing every other handler to pay
for an unused session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router

from telegram_invite_bot.handlers.achievements import (
    build_router as build_achievements_router,
)
from telegram_invite_bot.handlers.admin.aio_nr import (
    build_router as build_admin_aio_nr_router,
)
from telegram_invite_bot.handlers.admin.arp import (
    build_router as build_admin_arp_router,
)
from telegram_invite_bot.handlers.admin.audit import (
    build_router as build_admin_audit_router,
)
from telegram_invite_bot.handlers.admin.bot_session import (
    build_router as build_admin_bot_session_router,
)
from telegram_invite_bot.handlers.admin.botstats import (
    build_router as build_admin_botstats_router,
)
from telegram_invite_bot.handlers.admin.buddyinfo import (
    build_router as build_admin_buddyinfo_router,
)
from telegram_invite_bot.handlers.admin.capabilities import (
    build_router as build_admin_capabilities_router,
)
from telegram_invite_bot.handlers.admin.certfp import (
    build_router as build_admin_certfp_router,
)
from telegram_invite_bot.handlers.admin.cgroup import (
    build_router as build_admin_cgroup_router,
)
from telegram_invite_bot.handlers.admin.check_groups import (
    build_router as build_admin_check_groups_router,
)
from telegram_invite_bot.handlers.admin.clock import (
    build_router as build_admin_clock_router,
)
from telegram_invite_bot.handlers.admin.cmdline import (
    build_router as build_admin_cmdline_router,
)
from telegram_invite_bot.handlers.admin.codecs import (
    build_router as build_admin_codecs_router,
)
from telegram_invite_bot.handlers.admin.consoles import (
    build_router as build_admin_consoles_router,
)
from telegram_invite_bot.handlers.admin.cpu import (
    build_router as build_admin_cpu_router,
)
from telegram_invite_bot.handlers.admin.crypto import (
    build_router as build_admin_crypto_router,
)
from telegram_invite_bot.handlers.admin.db_sizes import (
    build_router as build_admin_db_sizes_router,
)
from telegram_invite_bot.handlers.admin.dbprobe import (
    build_router as build_admin_dbprobe_router,
)
from telegram_invite_bot.handlers.admin.deploy_hint import (
    build_router as build_admin_deploy_hint_router,
)
from telegram_invite_bot.handlers.admin.devices import (
    build_router as build_admin_devices_router,
)
from telegram_invite_bot.handlers.admin.dirty import (
    build_router as build_admin_dirty_router,
)
from telegram_invite_bot.handlers.admin.disk import (
    build_router as build_admin_disk_router,
)
from telegram_invite_bot.handlers.admin.diskstats import (
    build_router as build_admin_diskstats_router,
)
from telegram_invite_bot.handlers.admin.dns import (
    build_router as build_admin_dns_router,
)
from telegram_invite_bot.handlers.admin.donations import (
    build_router as build_admin_donations_router,
)
from telegram_invite_bot.handlers.admin.engines import (
    build_router as build_admin_engines_router,
)
from telegram_invite_bot.handlers.admin.envscan import (
    build_router as build_admin_envscan_router,
)
from telegram_invite_bot.handlers.admin.fdlimit import (
    build_router as build_admin_fdlimit_router,
)
from telegram_invite_bot.handlers.admin.fds import (
    build_router as build_admin_fds_router,
)
from telegram_invite_bot.handlers.admin.file_nr import (
    build_router as build_admin_file_nr_router,
)
from telegram_invite_bot.handlers.admin.filesystems import (
    build_router as build_admin_filesystems_router,
)
from telegram_invite_bot.handlers.admin.flags import (
    build_router as build_admin_flags_router,
)
from telegram_invite_bot.handlers.admin.gc import (
    build_router as build_admin_gc_router,
)
from telegram_invite_bot.handlers.admin.give import (
    build_router as build_admin_give_router,
)
from telegram_invite_bot.handlers.admin.group_migrate import (
    build_router as build_admin_group_migrate_router,
)
from telegram_invite_bot.handlers.admin.hashlib_info import (
    build_router as build_admin_hashlib_router,
)
from telegram_invite_bot.handlers.admin.help import (
    build_router as build_admin_help_router,
)
from telegram_invite_bot.handlers.admin.hostinfo import (
    build_router as build_admin_hostinfo_router,
)
from telegram_invite_bot.handlers.admin.imports import (
    build_router as build_admin_imports_router,
)
from telegram_invite_bot.handlers.admin.indexes import (
    build_router as build_admin_indexes_router,
)
from telegram_invite_bot.handlers.admin.integrity import (
    build_router as build_admin_integrity_router,
)
from telegram_invite_bot.handlers.admin.interrupts import (
    build_router as build_admin_interrupts_router,
)
from telegram_invite_bot.handlers.admin.io import (
    build_router as build_admin_io_router,
)
from telegram_invite_bot.handlers.admin.kernel import (
    build_router as build_admin_kernel_router,
)
from telegram_invite_bot.handlers.admin.key_users import (
    build_router as build_admin_key_users_router,
)
from telegram_invite_bot.handlers.admin.keys import (
    build_router as build_admin_keys_router,
)
from telegram_invite_bot.handlers.admin.limits import (
    build_router as build_admin_limits_router,
)
from telegram_invite_bot.handlers.admin.loadavg import (
    build_router as build_admin_loadavg_router,
)
from telegram_invite_bot.handlers.admin.locale_info import (
    build_router as build_admin_locale_router,
)
from telegram_invite_bot.handlers.admin.locks import (
    build_router as build_admin_locks_router,
)
from telegram_invite_bot.handlers.admin.loguru_sinks import (
    build_router as build_admin_loguru_router,
)
from telegram_invite_bot.handlers.admin.loop import (
    build_router as build_admin_loop_router,
)
from telegram_invite_bot.handlers.admin.marriages import (
    build_router as build_admin_marriages_router,
)
from telegram_invite_bot.handlers.admin.max_map_count import (
    build_router as build_admin_max_map_count_router,
)
from telegram_invite_bot.handlers.admin.meminfo import (
    build_router as build_admin_meminfo_router,
)
from telegram_invite_bot.handlers.admin.memory import (
    build_router as build_admin_memory_router,
)
from telegram_invite_bot.handlers.admin.middlewares import (
    build_router as build_admin_middlewares_router,
)
from telegram_invite_bot.handlers.admin.misc import (
    build_router as build_admin_misc_router,
)
from telegram_invite_bot.handlers.admin.modules import (
    build_router as build_admin_modules_router,
)
from telegram_invite_bot.handlers.admin.mounts import (
    build_router as build_admin_mounts_router,
)
from telegram_invite_bot.handlers.admin.netconns import (
    build_router as build_admin_netconns_router,
)
from telegram_invite_bot.handlers.admin.netdev import (
    build_router as build_admin_netdev_router,
)
from telegram_invite_bot.handlers.admin.nr_open import (
    build_router as build_admin_nr_open_router,
)
from telegram_invite_bot.handlers.admin.oom import (
    build_router as build_admin_oom_router,
)
from telegram_invite_bot.handlers.admin.p2p_disputes import (
    build_router as build_admin_p2p_disputes_router,
)
from telegram_invite_bot.handlers.admin.panel import (
    build_router as build_admin_panel_router,
)
from telegram_invite_bot.handlers.admin.partitions import (
    build_router as build_admin_partitions_router,
)
from telegram_invite_bot.handlers.admin.payment_keys import (
    build_router as build_admin_payment_keys_router,
)
from telegram_invite_bot.handlers.admin.pid_max import (
    build_router as build_admin_pid_max_router,
)
from telegram_invite_bot.handlers.admin.pragmas import (
    build_router as build_admin_pragmas_router,
)
from telegram_invite_bot.handlers.admin.proc import (
    build_router as build_admin_proc_router,
)
from telegram_invite_bot.handlers.admin.protocols import (
    build_router as build_admin_protocols_router,
)
from telegram_invite_bot.handlers.admin.psi import (
    build_router as build_admin_psi_router,
)
from telegram_invite_bot.handlers.admin.python import (
    build_router as build_admin_python_router,
)
from telegram_invite_bot.handlers.admin.pythonpath import (
    build_router as build_admin_pythonpath_router,
)
from telegram_invite_bot.handlers.admin.random_info import (
    build_router as build_admin_random_router,
)
from telegram_invite_bot.handlers.admin.rate_stats import (
    build_router as build_admin_rate_stats_router,
)
from telegram_invite_bot.handlers.admin.recent_signups import (
    build_router as build_admin_recent_signups_router,
)
from telegram_invite_bot.handlers.admin.relations import (
    build_router as build_admin_relations_router,
)
from telegram_invite_bot.handlers.admin.resolver import (
    build_router as build_admin_resolver_router,
)
from telegram_invite_bot.handlers.admin.route import (
    build_router as build_admin_route_router,
)
from telegram_invite_bot.handlers.admin.routes import (
    build_router as build_admin_routes_router,
)
from telegram_invite_bot.handlers.admin.runtime import (
    build_router as build_admin_runtime_router,
)
from telegram_invite_bot.handlers.admin.rusage import (
    build_router as build_admin_rusage_router,
)
from telegram_invite_bot.handlers.admin.self_status import (
    build_router as build_admin_self_status_router,
)
from telegram_invite_bot.handlers.admin.settings_view import (
    build_router as build_admin_settings_router,
)
from telegram_invite_bot.handlers.admin.shop_prices import (
    build_router as build_admin_shop_prices_router,
)
from telegram_invite_bot.handlers.admin.signals import (
    build_router as build_admin_signals_router,
)
from telegram_invite_bot.handlers.admin.slabinfo import (
    build_router as build_admin_slabinfo_router,
)
from telegram_invite_bot.handlers.admin.smaps import (
    build_router as build_admin_smaps_router,
)
from telegram_invite_bot.handlers.admin.sockstat import (
    build_router as build_admin_sockstat_router,
)
from telegram_invite_bot.handlers.admin.softirqs import (
    build_router as build_admin_softirqs_router,
)
from telegram_invite_bot.handlers.admin.ssl_info import (
    build_router as build_admin_ssl_router,
)
from telegram_invite_bot.handlers.admin.stat import (
    build_router as build_admin_stat_router,
)
from telegram_invite_bot.handlers.admin.status import build_router as build_admin_status_router
from telegram_invite_bot.handlers.admin.swaps import (
    build_router as build_admin_swaps_router,
)
from telegram_invite_bot.handlers.admin.sysctl import (
    build_router as build_admin_sysctl_router,
)
from telegram_invite_bot.handlers.admin.tables import (
    build_router as build_admin_tables_router,
)
from telegram_invite_bot.handlers.admin.tasks import (
    build_router as build_admin_tasks_router,
)
from telegram_invite_bot.handlers.admin.tcpext import (
    build_router as build_admin_tcpext_router,
)
from telegram_invite_bot.handlers.admin.telegram_api import (
    build_router as build_admin_telegram_api_router,
)
from telegram_invite_bot.handlers.admin.tempdir import (
    build_router as build_admin_tempdir_router,
)
from telegram_invite_bot.handlers.admin.test_log import (
    build_router as build_admin_test_log_router,
)
from telegram_invite_bot.handlers.admin.thp import (
    build_router as build_admin_thp_router,
)
from telegram_invite_bot.handlers.admin.threads import (
    build_router as build_admin_threads_router,
)
from telegram_invite_bot.handlers.admin.top_users import (
    build_router as build_admin_top_users_router,
)
from telegram_invite_bot.handlers.admin.transactions import (
    build_router as build_admin_transactions_router,
)
from telegram_invite_bot.handlers.admin.uptime import (
    build_router as build_admin_uptime_router,
)
from telegram_invite_bot.handlers.admin.vmstat import (
    build_router as build_admin_vmstat_router,
)
from telegram_invite_bot.handlers.admin.warnings_view import (
    build_router as build_admin_warnings_router,
)
from telegram_invite_bot.handlers.admin.withdrawals import (
    build_router as build_admin_withdrawals_router,
)
from telegram_invite_bot.handlers.admin.zoneinfo import (
    build_router as build_admin_zoneinfo_router,
)
from telegram_invite_bot.handlers.ads import build_router as build_ads_router
from telegram_invite_bot.handlers.ai import build_router as build_ai_router
from telegram_invite_bot.handlers.antiflood import AntifloodMiddleware
from telegram_invite_bot.handlers.broadcast import (
    build_router as build_broadcast_router,
)
from telegram_invite_bot.handlers.calc import build_router as build_calc_router
from telegram_invite_bot.handlers.cancel import build_router as build_cancel_router
from telegram_invite_bot.handlers.challenge_commands import (
    build_router as build_challenge_commands_router,
)
from telegram_invite_bot.handlers.chatstats import build_router as build_chatstats_router
from telegram_invite_bot.handlers.checks import build_router as build_checks_router
from telegram_invite_bot.handlers.city import build_router as build_city_router
from telegram_invite_bot.handlers.clear import build_router as build_clear_router
from telegram_invite_bot.handlers.command_access import CommandAccessMiddleware
from telegram_invite_bot.handlers.commission import build_router as build_commission_router
from telegram_invite_bot.handlers.couple_activities import (
    build_router as build_couple_activities_router,
)
from telegram_invite_bot.handlers.currency import build_router as build_currency_router
from telegram_invite_bot.handlers.daily import build_router as build_daily_router
from telegram_invite_bot.handlers.donate import build_router as build_donate_router
from telegram_invite_bot.handlers.donaters import build_router as build_donaters_router
from telegram_invite_bot.handlers.duel import build_router as build_duel_router
from telegram_invite_bot.handlers.duel_stats import build_router as build_duel_stats_router
from telegram_invite_bot.handlers.economy import build_router as build_economy_router
from telegram_invite_bot.handlers.emoji import build_router as build_emoji_router
from telegram_invite_bot.handlers.errors import build_errors_router
from telegram_invite_bot.handlers.faq import build_router as build_faq_router
from telegram_invite_bot.handlers.games import build_router as build_games_router
from telegram_invite_bot.handlers.games_menu import (
    build_router as build_games_menu_router,
)
from telegram_invite_bot.handlers.group_aliases import (
    GroupAliasMiddleware,
)
from telegram_invite_bot.handlers.group_aliases import (
    build_router as build_group_aliases_router,
)
from telegram_invite_bot.handlers.group_events import build_router as build_group_events_router
from telegram_invite_bot.handlers.group_migration import (
    build_router as build_group_migration_router,
)
from telegram_invite_bot.handlers.group_pay import build_router as build_group_pay_router
from telegram_invite_bot.handlers.groupadmin import build_router as build_groupadmin_router
from telegram_invite_bot.handlers.groupstats import build_router as build_groupstats_router
from telegram_invite_bot.handlers.heartbeat import build_router as build_heartbeat_router
from telegram_invite_bot.handlers.help import build_router as build_help_router
from telegram_invite_bot.handlers.jokes import build_router as build_jokes_router
from telegram_invite_bot.handlers.language import build_router as build_language_router
from telegram_invite_bot.handlers.legal import build_router as build_legal_router
from telegram_invite_bot.handlers.main_menu import build_router as build_main_menu_router
from telegram_invite_bot.handlers.marriage import build_router as build_marriage_router
from telegram_invite_bot.handlers.modcfg import build_router as build_modcfg_router
from telegram_invite_bot.handlers.moderation import build_router as build_moderation_router
from telegram_invite_bot.handlers.mydonates import build_router as build_mydonates_router
from telegram_invite_bot.handlers.mygroups import build_router as build_mygroups_router
from telegram_invite_bot.handlers.nick import build_router as build_nick_router
from telegram_invite_bot.handlers.p2p import build_router as build_p2p_router
from telegram_invite_bot.handlers.p2p_trade import (
    build_router as build_p2p_trade_router,
)
from telegram_invite_bot.handlers.profile import build_router as build_profile_router
from telegram_invite_bot.handlers.promo import build_router as build_promo_router
from telegram_invite_bot.handlers.pvp_stake import build_router as build_pvp_stake_router
from telegram_invite_bot.handlers.quotes import build_router as build_quotes_router
from telegram_invite_bot.handlers.rank_admin import build_router as build_rank_admin_router
from telegram_invite_bot.handlers.rank_self import build_router as build_rank_self_router
from telegram_invite_bot.handlers.rating import build_router as build_rating_router
from telegram_invite_bot.handlers.referral import build_router as build_referral_router
from telegram_invite_bot.handlers.referrals import build_router as build_referrals_router
from telegram_invite_bot.handlers.relations import build_router as build_relations_router
from telegram_invite_bot.handlers.report import build_router as build_report_router
from telegram_invite_bot.handlers.roulette import build_router as build_roulette_router
from telegram_invite_bot.handlers.rp import build_router as build_rp_router
from telegram_invite_bot.handlers.rps import build_router as build_rps_router
from telegram_invite_bot.handlers.rules import build_router as build_rules_router
from telegram_invite_bot.handlers.send import build_router as build_send_router
from telegram_invite_bot.handlers.shop import build_router as build_shop_router
from telegram_invite_bot.handlers.stale_callback import build_stale_callback_router
from telegram_invite_bot.handlers.start import build_router as build_start_router
from telegram_invite_bot.handlers.stats import build_router as build_stats_router
from telegram_invite_bot.handlers.support import build_router as build_support_router
from telegram_invite_bot.handlers.time import build_router as build_time_router
from telegram_invite_bot.handlers.timezone import build_router as build_timezone_router
from telegram_invite_bot.handlers.top import build_router as build_top_router
from telegram_invite_bot.handlers.topup import build_router as build_topup_router
from telegram_invite_bot.handlers.transfer_rights import (
    build_router as build_transfer_rights_router,
)
from telegram_invite_bot.handlers.unknown_form import build_unknown_form_router
from telegram_invite_bot.handlers.vip import build_router as build_vip_router
from telegram_invite_bot.handlers.vip_emoji_voice import (
    build_router as build_vip_emoji_voice_router,
)
from telegram_invite_bot.handlers.voice_settings import (
    build_router as build_voice_settings_router,
)
from telegram_invite_bot.handlers.voice_transcribe import (
    build_router as build_voice_transcribe_router,
)
from telegram_invite_bot.handlers.weather import build_router as build_weather_router
from telegram_invite_bot.handlers.withdraw import build_router as build_withdraw_router
from telegram_invite_bot.handlers.withdraw_status import (
    build_router as build_withdraw_status_router,
)
from telegram_invite_bot.handlers.wordfilter import WordFilterAutomodMiddleware
from telegram_invite_bot.handlers.wordfilter import build_router as build_wordfilter_router
from telegram_invite_bot.middlewares.ai_rate_limit import (
    AiRateLimitMiddleware,
    WeatherRateLimitMiddleware,
)
from telegram_invite_bot.middlewares.language import LanguageMiddleware
from telegram_invite_bot.middlewares.legacy_reply_keyboard import (
    LegacyReplyKeyboardMiddleware,
)
from telegram_invite_bot.middlewares.message_activity import MessageActivityMiddleware
from telegram_invite_bot.middlewares.text_alias import TextAliasMiddleware
from telegram_invite_bot.services.currency_service import CurrencyService
from telegram_invite_bot.services.joke_service import JokeService
from telegram_invite_bot.services.payments.fx import FX_UPSTREAM_TIMEOUT_SECONDS
from telegram_invite_bot.services.weather_service import WeatherService

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Dispatcher

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.middlewares.throttling import ThrottlingMiddleware


def build_main_router(
    registry: EngineRegistry,
    settings: Settings,
    *,
    throttle: ThrottlingMiddleware,
    get_dispatcher: Callable[[], Dispatcher],
) -> Router:
    root = Router(name="main")
    # CLUSTER INFRA: single source of truth for the effective bot
    # language. Registered as the FIRST outer middleware on BOTH event
    # types so ``data["lang"]`` is resolved (stored user.language >
    # Telegram language_code > "ru") before any other outer middleware or
    # handler runs — every downstream handler injects ``lang: str`` rather
    # than re-deriving it from the Telegram client locale. Attached on
    # ``callback_query`` too so inline-button handlers get the same
    # effective language as the message that rendered them.
    language_middleware = LanguageMiddleware(registry)
    root.message.outer_middleware(language_middleware)
    root.callback_query.outer_middleware(language_middleware)
    # Retires the monolith's stuck reply keyboard. Registered right after
    # Language (it renders a localised note) and BEFORE the alias
    # middlewares, so the three legacy button labels are claimed here
    # rather than by an alias map that is edited far more often than this
    # line. It also has to precede MessageActivityMiddleware for the same
    # reason TextAlias does: a tap that is really a command must not be
    # counted as chatter or earn coins.
    root.message.outer_middleware(LegacyReplyKeyboardMiddleware())
    # A-06: legacy plain-text command-alias router. Registered as the
    # first *rewriting* message middleware — Language above is outer to
    # it but only fills ``lang`` in — so a recognised shortcut (e.g.
    # group ``.баланс`` or private ``топ``) is rewritten to ``/command`` BEFORE
    # MessageActivityMiddleware sees it — an intercepted shortcut must not
    # count as chatter / earn coins, matching legacy. Non-alias messages
    # pass through untouched.
    root.message.outer_middleware(TextAliasMiddleware())
    # L-60: per-group dynamic aliases — rewrites a matching first token to
    # its mapped /command BEFORE dispatch (static TextAlias first, then
    # per-group). TTL-cached; never raises/consumes.
    root.message.outer_middleware(GroupAliasMiddleware(registry))
    # A-03: passive per-message earning + stats counting for group
    # messages. An OUTER middleware on the root router so it runs for
    # every message — including plain chatter no command handler claims
    # (an inner middleware would only fire on a matched handler).
    root.message.outer_middleware(
        MessageActivityMiddleware(registry, settings.economy, settings.stats)
    )
    # L-52: word/profanity automod. Outer middleware so it screens every
    # group message (incl. plain chatter) and deletes banned-word hits
    # best-effort BEFORE any handler runs; never raises/consumes.
    # Ordering: aiogram wraps outer middlewares with ``reversed()``
    # (aiogram/dispatcher/middlewares/manager.py:66), so the *first*
    # registered is the outermost and runs *first*. The activity counter
    # above has therefore already counted and paid for a message by the
    # time this deletes it. That is legacy parity — bot.py credited
    # coins and bumped the counter at :43836-43850 and only then ran the
    # profanity check at :43852 — so the order stays as it is.
    root.message.outer_middleware(WordFilterAutomodMiddleware(registry, settings))
    # L-56: per-group antiflood. Sliding-window burst detector; best-effort
    # mute + one notice per mute, skips the owner (#1863) and admins,
    # never raises/consumes.
    root.message.outer_middleware(AntifloodMiddleware(registry, settings))
    # EPIC ranks (R4): /cmdcfg min-rank enforcement for every /command —
    # override else catalog default; 6=disabled; dev + live-TG-admin
    # bypass; fail-OPEN on infra errors.
    # #1428: attached to ``callback_query`` as well, and as the SAME
    # instance. The private /start welcome menu taps reach eight of these
    # very commands (handlers/main_menu), so a gate on ``message`` alone
    # left ``/cmdcfg set balance 6`` switching off the typed command and
    # leaving the button — and the kill switch has no handler-side twin.
    # One instance because the stale-override snapshot that keeps the
    # switch alive through a DB failure lives on the instance.
    # #1918: registered LAST of the message outer middlewares, i.e. after
    # both automods. A rank denial CONSUMES the update (the kill-switch
    # and below-rank branches both return without calling the handler),
    # so every middleware registered after the gate never saw a denied
    # command — ``/dev <banned word>`` from a below-rank caller walked
    # past the word filter AND the antiflood counter. The gate used to sit
    # ahead of MessageActivityMiddleware to keep a denied command from
    # earning coins; that reason was void, because MessageActivity skips
    # any text starting with ``/`` on its own (middlewares/message_activity
    # :364 and :472), so no command has ever earned coins at any position.
    # Moving it here costs nothing and closes both automod holes; denied
    # commands now count toward the flood window, which is the point.
    command_access = CommandAccessMiddleware(registry, settings)
    root.message.outer_middleware(command_access)
    root.callback_query.outer_middleware(command_access)
    root.include_router(build_start_router(registry, settings))
    # A-01 Part B: callbacks for the private /start main-menu keyboard
    # (profile / balance / referral). Right after /start so the menu its
    # welcome attaches is owned adjacent to its taps.
    root.include_router(build_main_menu_router(registry, settings))
    # /cancel needs to win against every later router's command-bound
    # handlers that might also match the bare word "cancel" in some
    # FSM-prompt branch. Putting it before every FSM-owning router
    # keeps the escape-hatch promise: no later flow can swallow
    # /cancel. (The two routers ahead of it are /start and the main
    # menu; the latter registers ``callback_query`` only, so neither
    # can take a message away from it.)
    root.include_router(build_cancel_router())
    # M-P-9: single ``WeatherService`` shared across requests so the
    # in-memory TTL cache (keyed by lat/lon/UTC-date) is reused. A
    # fresh instance per call would defeat the cache — that was the
    # pre-fix bug the audit flagged.
    weather_service = WeatherService()
    # #421: ONE limiter for the whole geocoder surface. The bucket table
    # is per-instance, so the three routers below building one each meant
    # a single user got 5/min on /weather PLUS 5/min on /city PLUS 5/min
    # on /time — fifteen calls a minute against a limit the code
    # documents as five. Same ownership model as the service above:
    # built once here, injected into everything that can geocode.
    weather_rate_limit = WeatherRateLimitMiddleware()
    root.include_router(build_weather_router(weather_service, weather_rate_limit))
    # RR-6 #74: /city geocodes through the SAME service instance — it
    # hits the same upstream host, so sharing the cache (and the
    # per-user rate-limit posture) is the point. There is no shared
    # client to speak of: no ``client=`` is passed above, so each
    # uncached call still dials out on its own (#423). Right after
    # /weather because the two are one feature: /city is where the
    # saved default that /weather reads comes from.
    root.include_router(build_city_router(weather_service, weather_rate_limit))
    # A-09: /rate + /convert (COM→currency). Single shared
    # ``CurrencyService`` so its 1h TTL rate cache is reused across
    # requests — same closure-injection posture as the weather service
    # above. Sits next to weather because both are HTTP-backed, cached,
    # any-chat-type read-only commands. The optional keyed v6 endpoint is
    # selected when ``EXCHANGERATE_API_KEY`` is set (else the keyless v4).
    currency_service = CurrencyService(
        api_key=(
            settings.currency.api_key.get_secret_value()
            if settings.currency.api_key is not None
            else None
        ),
        # Capped, not taken at face value: this instance also prices
        # the RollyPay rows in /topup, and a money path waits behind
        # ``FX_TIMEOUT_SECONDS`` — the client has to give up first so
        # the fallback table reaches the cache (#1614). ``/rate``,
        # ``/convert`` and the profile card ride the same instance and
        # inherit the shorter wait; they only ever read a rate.
        timeout=min(settings.currency.timeout_seconds, FX_UPSTREAM_TIMEOUT_SECONDS),
        cache_ttl_seconds=settings.currency.cache_ttl_seconds,
        # T-019 (R4): quote coins at the rate they actually cash out for,
        # so /rate can never advertise a price the /withdraw desk won't
        # honour.
        coins_per_usdt=settings.withdraw.coins_per_usdt,
    )
    root.include_router(build_currency_router(currency_service, registry))
    # The profile card's "≈ N ₽" line rides the SAME instance: a second
    # one would keep its own cold cache and could quote a different
    # number than /rate did a second earlier.
    root.include_router(build_profile_router(registry, settings.stats, settings, currency_service))
    root.include_router(build_help_router(registry, settings))
    # A-07: /achievements read-only card. Mounts its own
    # EconomyMiddleware (achievements_repo) and reads ``user_service``
    # off the dispatcher-level SessionMiddleware — same contract as
    # /profile, so it sits adjacent.
    root.include_router(build_achievements_router(registry))
    root.include_router(build_economy_router(registry))
    # /daily uses the same EconomyMiddleware as /balance — order vs.
    # build_economy_router doesn't matter, the commands are disjoint.
    root.include_router(build_daily_router(registry, settings))
    # /send shares EconomyMiddleware with /balance + /daily; order
    # vs them is immaterial (commands are disjoint).
    root.include_router(build_send_router(registry, settings))
    # /cpc (Stage 34) — first FSM-driven flow. Shares EconomyMiddleware
    # with /balance/daily/send; the commands are disjoint so include
    # order vs neighbours doesn't matter. Sits next to /send because
    # both ride the SessionMiddleware (UsersRepo lookup for @username).
    root.include_router(build_rps_router(registry))
    # /duel (T-018) — group-chat dice PvP. Twin of /cpc, same
    # no-escrow / atomic-resolve posture via DuelService.
    root.include_router(build_duel_router(registry))
    # AUD-2: /pvp_coin + /pvp_dice PvP escrow stake games. Group-only,
    # EconomyMiddleware-backed; sits next to /duel (its closest analog).
    root.include_router(build_pvp_stake_router(registry))
    # /roulette (A-10) — single-player RUSSIAN ROULETTE over economy.db.
    # Group-only (in-handler gate); mounts its own EconomyMiddleware for
    # the atomic stake debit + win credit. Anti-abuse caps (cooldown /
    # per-hour / per-day) are PERSISTENT: GameLimitService stamps them
    # into economy.game_plays (L-25), on a rolling 24h window shared
    # with /roll and /flip and with no dev exemption (#222-A, #222-C).
    # Since A-11/A-12 the success path also writes the games row with
    # the signed profit. Until #1939 this comment still described the
    # pre-L-25 shape — an in-process timestamp limiter that reset on
    # every redeploy, and no games row at all — i.e. it advertised a
    # money path as guarded by something that no longer exists.
    root.include_router(build_roulette_router(registry))
    root.include_router(build_donaters_router(registry))
    root.include_router(build_mydonates_router(registry))
    root.include_router(build_mygroups_router(registry, settings.stats))
    root.include_router(build_duel_stats_router(registry))
    root.include_router(build_groupstats_router(registry))
    # Group onboarding (bot-added notice + new-member welcome). DB-less
    # event router: handles ``my_chat_member`` (bot self-join) and
    # ``new_chat_members`` service messages. No commands, so it doesn't
    # compete with any command-bound handler; placed next to the other
    # group-only routers for locality.
    root.include_router(build_group_events_router(registry, settings))
    # Supergroup upgrade (#110): carries every group-keyed row from the
    # old chat_id to the new one. Another DB-less-command event router —
    # it binds no command and answers nothing, it only reacts to the
    # ``migrate_to_chat_id`` / ``migrate_from_chat_id`` service messages,
    # so it cannot shadow anything included after it.
    root.include_router(build_group_migration_router(registry))
    # /chatstats + /cstats (A-08) — group activity card (members /
    # message activity / top-3). Reads message_stats.db via its own
    # scoped MessageStatsMiddleware + user_service (SessionMiddleware)
    # for the caller's language. Group-gated in-handler so a private
    # call gets the localised refusal.
    root.include_router(build_chatstats_router(registry, settings.stats))
    # /rating + /top_groups + private-DM /groupstats leaderboard (A-04).
    # Reads economy.db via registry + user_service (SessionMiddleware);
    # no scoped middleware. Sits next to groupstats since it owns the
    # private-DM branch of the same command spellings.
    root.include_router(build_rating_router(registry, settings))
    # L-41: /group_pay — owner payout from the group treasury. The
    # minimum is the operator's, not a constant: the default only stands
    # when GROUP_TREASURY_MIN_WITHDRAWAL is unset.
    root.include_router(
        build_group_pay_router(
            registry, settings, min_withdrawal=settings.economy.group_treasury_min_withdrawal
        )
    )
    # #2007: /donate — a member funds the group's rating out of their own
    # wallet. Next to /group_pay because they are the two ends of the same
    # money: this one pays the group's owner, that one pays the owner out
    # of the treasury.
    root.include_router(build_donate_router(registry, settings))
    root.include_router(build_referral_router(settings))
    root.include_router(build_referrals_router(registry))
    # L-24: dev-gated /give (/выдать) — ledger-credit coins to a user.
    root.include_router(build_admin_give_router(registry, settings))
    root.include_router(build_commission_router(registry, settings))
    root.include_router(build_shop_router(registry, settings))
    root.include_router(build_stats_router(registry, settings.stats))
    root.include_router(build_games_router(registry))
    # EPIC ranks (R3): /staff_me + bang-rank commands. Included BEFORE the
    # AI router so the narrow bang-regexp wins over the broad plain-text
    # AI trigger.
    root.include_router(build_rank_self_router(registry, settings))
    # EPIC ranks (R2): /perm /cmdcfg (dev-only) + /rank read-only card.
    root.include_router(build_rank_admin_router(registry, settings))
    # #262(d): hand the AI router the SAME ``WeatherService`` the /weather,
    # /city and /time routers share. Omitting it made ``ai.build_router``
    # fall back to a second, cold instance with its own empty TTL cache and
    # its own ``KeyedLocks``, so an AI-triggered lookup never saw the cache
    # /weather had warmed and the two instances could issue duplicate
    # concurrent upstream calls for the same city.
    # #1103: ONE limiter across every command that reaches DeepSeek.
    # The bucket table is per-instance, so /ai + /ask on one instance
    # and /quote on another gave a single user the full per-minute
    # allowance twice over — against one paid key. Same fix, and the
    # same reason, as ``weather_rate_limit`` above.
    ai_rate_limit = AiRateLimitMiddleware()
    root.include_router(
        build_ai_router(registry, settings.ai, settings.ai_quota, weather_service, ai_rate_limit)
    )
    # #117: /report — tell a group's admins about a message. Sits AFTER
    # the AI router deliberately: both have a private-chat half, and a
    # user who opened a «Войти в Ком» session must keep getting the
    # model. /report itself is safe either way (the AI catch-all skips
    # anything starting with ``/``), so the later slot costs nothing and
    # removes the whole class of ordering surprise. No registry arg — the
    # handler reads live Telegram state only.
    root.include_router(build_report_router())
    root.include_router(build_support_router(settings.bot.admin_chat_id, settings))
    # /check + /create_check (#26) — coin-code vouchers. Mounts its own
    # EconomyMiddleware (injects check_service) like /vip_shop. Sits
    # next to /support because it took over /support's old static /check
    # stub. Private-only; a group call gets the #123 refusal twin (the
    # legacy telebot bridge this comment used to name was removed in
    # T-011, so "falls through" meant silence until #123).
    root.include_router(build_checks_router(registry, settings))
    # /promo + /promo_create (L-96) — redeemable promo/gift codes; private-only
    root.include_router(build_promo_router(registry, settings))
    # EPIC P2P (#64): /p2p marketplace — menu + sell FSM (+ buy/trade UI).
    root.include_router(build_p2p_router(registry, settings))
    # Phase A: /topup (Stars + CryptoPay invoices, RollyPay rouble
    # checkout, degraded YooKassa/Stripe) + dev-only /broadcast.
    #
    # Takes the SAME ``currency_service`` /rate and /profile ride: the
    # RollyPay rows quote roubles through the live USD/RUB fix, and a
    # second instance would answer from its own cold cache — /topup and
    # /rate could then show two different rates in the same minute.
    root.include_router(build_topup_router(registry, settings, currency_service=currency_service))
    root.include_router(build_broadcast_router(settings))
    root.include_router(build_p2p_trade_router(registry, settings))
    # L-61: /ad /ads /reklama — advertiser-request funnel (private form
    # -> one message forwarded to ADMIN_CHAT_ID, 24h cooldown).
    root.include_router(build_ads_router(registry, settings))
    root.include_router(build_vip_router(registry, settings.stats))
    # /emojis + /emoji_set + /emoji_preview + /emoji_buy (#25) — VIP
    # cosmetic emoji badge. Took over the static emoji stubs that used to
    # live in handlers/vip.py. Mounts its own EconomyMiddleware (injects
    # emoji_badge_service); free-for-VIP, no money path. Private-only.
    root.include_router(build_emoji_router(registry))
    # T-023: /voice — VIP-only TTS over OpenAI. Shares the
    # OpenAI key with /ai (T-021); reads VipRepo off the
    # EconomyMiddleware its build_router mounts internally.
    root.include_router(
        build_vip_emoji_voice_router(
            registry,
            settings.openai,
            settings.tts,
            settings.features,
        )
    )
    root.include_router(build_relations_router(registry))
    # T-019: marriage proposal flow + divorce + breakup.
    # Sits right after build_relations_router because both touch the
    # same tables (marriages, relationships) and share the
    # SessionMiddleware(users.db) contract.
    root.include_router(build_marriage_router(registry))
    # FEAT-RP: romance RP-actions (.обнять / бот поцеловать → pair XP) +
    # /rp_commands. Sits right after marriage because it reads the same
    # marriages/relationships tables via the same SessionMiddleware(users.db)
    # contract. The action handler is registered behind a parse filter so
    # only valid group RP messages are intercepted; ordinary chatter falls
    # through to every later router untouched.
    root.include_router(build_rp_router(registry))
    # FEAT-COUPLE-ACT: /activities — paid joint-activity menu that grants
    # pair XP. Sits next to marriage/rp because it reads the same
    # marriages/relationships tables (SessionMiddleware/users.db) AND
    # debits the wallet (EconomyMiddleware/economy.db); it mounts both
    # middlewares internally. Group-only message + a pure callback.
    root.include_router(build_couple_activities_router(registry))
    # L-70: group voice-message STT via OpenAI Whisper API. Group-only
    # F.voice handler — only fires when the group enabled transcription
    # (default off), so ordinary voice chatter falls through untouched.
    # Degrades silently when OPENAI_API_KEY is unset. Closes over settings
    # for the OpenAI key at build time.
    root.include_router(build_voice_transcribe_router(registry, settings))
    # L-71/L-72: group-admin /voice_settings menu (toggle/target/language +
    # 📊 stats callback). Registers the SAME /voice_settings tokens as the
    # vip private stub but under a GROUP filter — disjoint chat-type filters,
    # so no runtime conflict; this is the real menu (legacy cmd_voice_settings
    # was group-only).
    root.include_router(build_voice_settings_router(registry, settings))
    # T-020: group moderation commands (/ban, /kick, /mute, /warn, /unwarn,
    # /warnings, /pin, /unpin, /fine). Sits after marriage because both
    # are group-only command sets; moderation owns a separate DB
    # (moderation.db via ModerationMiddleware) and also mounts
    # SessionMiddleware (for @username lookups) and EconomyMiddleware
    # (for /fine). A private-chat invocation gets the #123 refusal twin.
    root.include_router(build_moderation_router(registry, settings))
    # L-47/L-52/L-43: rank-independent moderation (live-TG-admin gated).
    root.include_router(build_clear_router(registry, settings))
    root.include_router(build_wordfilter_router(registry, settings))
    # L-60: /alias add|del|list — per-group dynamic command aliases.
    root.include_router(build_group_aliases_router(registry, settings))
    root.include_router(build_modcfg_router(registry, settings))
    # Ranks wave 2 (L-42): /groupadmin read-only control card.
    root.include_router(build_groupadmin_router(registry, settings))
    # Tail sweep: /games discovery card + /accept //decline challenge commands.
    root.include_router(build_games_menu_router())
    root.include_router(build_challenge_commands_router(registry))
    # L-49: /transfer_rights — DM transfer of bot-side group ownership.
    root.include_router(build_transfer_rights_router(registry))
    root.include_router(build_rules_router(registry))
    root.include_router(build_top_router(registry, settings.stats))
    root.include_router(build_heartbeat_router())
    # RR-6 #72: one long-lived JokeService. It does NOT carry a warm
    # httpx pool — no ``client=`` is passed here or anywhere else, so
    # every online fetch pays its own TLS handshake (#423); a shared
    # client is a separate change. ``JOKE_OFFLINE_ONLY`` degrades the
    # command to its local pool without a redeploy.
    root.include_router(
        build_jokes_router(JokeService(enabled=not settings.features.joke_offline_only))
    )
    # /quote — AI-generated wise quote with the local pool as fallback
    # (RR-6 #71). Rides the same daily AI quota as /ask, so it sits next
    # to /joke as a content leaf but needs the registry + AI config.
    root.include_router(
        build_quotes_router(registry, settings.ai, settings.ai_quota, ai_rate_limit)
    )
    # /faq2 is a leaf static-text handler with no shared middlewares —
    # order vs. neighbouring routers is immaterial. Sits next to jokes
    # because both are content-only commands with no DB dependency.
    # ``settings.help`` carries the optional command-guide URL rendered
    # as a button under part 2 (RR-6 #70), same source as /help.
    root.include_router(build_faq_router(settings.help))
    # /legal, /terms, /privacy, /offer, /docs — the offer and the privacy
    # policy, one tap from any chat. Static text plus URL buttons, so it
    # belongs with the other leaf handlers; it is registered
    # unconditionally because "permanently available to the user" is the
    # acquiring bank's condition, not a feature flag.
    root.include_router(build_legal_router(settings))
    root.include_router(build_language_router())
    root.include_router(build_timezone_router())
    # Shares the weather service and the limiter with /weather and /city
    # (RR-6 #69, #421): all three reach the same geocoding host, so they
    # share one HTTP client and one per-user allowance. NOT a cache
    # argument — ``resolve_city`` deliberately caches nothing
    # (weather_service.py:356-359); only the forecast endpoint does.
    root.include_router(build_time_router(weather_service, weather_rate_limit))
    root.include_router(build_nick_router())
    # /calc is a thin stateless leaf — no DB engines or registry args.
    # Sits next to /nick / /jokes / /faq — all session-only commands
    # with no shared middlewares beyond SessionMiddleware(users.db).
    root.include_router(build_calc_router())
    root.include_router(build_admin_status_router(settings, registry))
    root.include_router(build_admin_botstats_router(settings, registry))
    root.include_router(build_admin_shop_prices_router(settings, registry))
    root.include_router(build_admin_rate_stats_router(settings, throttle))
    root.include_router(build_admin_check_groups_router(settings, registry))
    root.include_router(build_admin_test_log_router(settings))
    root.include_router(build_admin_help_router(settings))
    root.include_router(build_admin_panel_router(settings))
    root.include_router(build_admin_deploy_hint_router(settings))
    root.include_router(build_admin_transactions_router(settings, registry))
    root.include_router(build_admin_donations_router(settings, registry))
    root.include_router(build_admin_top_users_router(settings, registry))
    root.include_router(build_admin_recent_signups_router(settings, registry))
    root.include_router(build_admin_marriages_router(settings, registry))
    root.include_router(build_admin_relations_router(settings, registry))
    root.include_router(build_admin_pragmas_router(settings, registry))
    root.include_router(build_admin_db_sizes_router(settings, registry))
    root.include_router(build_admin_modules_router(settings))
    root.include_router(build_admin_python_router(settings))
    root.include_router(build_admin_proc_router(settings))
    root.include_router(build_admin_fdlimit_router(settings))
    root.include_router(build_admin_uptime_router(settings))
    root.include_router(build_admin_clock_router(settings))
    root.include_router(build_admin_settings_router(settings))
    root.include_router(build_admin_integrity_router(settings, registry))
    root.include_router(build_admin_engines_router(settings, registry))
    root.include_router(build_admin_disk_router(settings))
    root.include_router(build_admin_tables_router(settings, registry))
    root.include_router(build_admin_indexes_router(settings, registry))
    root.include_router(build_admin_tasks_router(settings))
    root.include_router(build_admin_threads_router(settings))
    root.include_router(build_admin_gc_router(settings))
    root.include_router(build_admin_signals_router(settings))
    root.include_router(build_admin_hostinfo_router(settings))
    root.include_router(build_admin_ssl_router(settings))
    root.include_router(build_admin_locale_router(settings))
    root.include_router(build_admin_pythonpath_router(settings))
    root.include_router(build_admin_warnings_router(settings))
    root.include_router(build_admin_flags_router(settings))
    root.include_router(build_admin_cpu_router(settings))
    root.include_router(build_admin_runtime_router(settings))
    root.include_router(build_admin_fds_router(settings))
    root.include_router(build_admin_memory_router(settings))
    root.include_router(build_admin_rusage_router(settings))
    root.include_router(build_admin_tempdir_router(settings))
    root.include_router(build_admin_kernel_router(settings))
    root.include_router(build_admin_hashlib_router(settings))
    root.include_router(build_admin_imports_router(settings))
    root.include_router(build_admin_dns_router(settings))
    root.include_router(build_admin_dbprobe_router(settings, registry))
    root.include_router(build_admin_telegram_api_router(settings))
    root.include_router(build_admin_envscan_router(settings))
    root.include_router(build_admin_certfp_router(settings))
    root.include_router(build_admin_codecs_router(settings))
    root.include_router(build_admin_random_router(settings))
    root.include_router(build_admin_netconns_router(settings))
    root.include_router(build_admin_oom_router(settings))
    root.include_router(build_admin_capabilities_router(settings))
    root.include_router(build_admin_cgroup_router(settings))
    root.include_router(build_admin_group_migrate_router(registry, settings))
    root.include_router(build_admin_io_router(settings))
    root.include_router(build_admin_smaps_router(settings))
    root.include_router(build_admin_resolver_router(settings))
    root.include_router(build_admin_limits_router(settings))
    root.include_router(build_admin_audit_router(settings))
    root.include_router(build_admin_meminfo_router(settings))
    root.include_router(build_admin_mounts_router(settings))
    root.include_router(build_admin_loadavg_router(settings))
    root.include_router(build_admin_diskstats_router(settings))
    root.include_router(build_admin_sysctl_router(settings))
    root.include_router(build_admin_swaps_router(settings))
    root.include_router(build_admin_route_router(settings))
    root.include_router(build_admin_tcpext_router(settings))
    root.include_router(build_admin_psi_router(settings))
    root.include_router(build_admin_netdev_router(settings))
    root.include_router(build_admin_vmstat_router(settings))
    root.include_router(build_admin_sockstat_router(settings))
    root.include_router(build_admin_softirqs_router(settings))
    root.include_router(build_admin_interrupts_router(settings))
    root.include_router(build_admin_buddyinfo_router(settings))
    root.include_router(build_admin_arp_router(settings))
    root.include_router(build_admin_zoneinfo_router(settings))
    root.include_router(build_admin_slabinfo_router(settings))
    root.include_router(build_admin_locks_router(settings))
    root.include_router(build_admin_partitions_router(settings))
    root.include_router(build_admin_filesystems_router(settings))
    root.include_router(build_admin_cmdline_router(settings))
    root.include_router(build_admin_crypto_router(settings))
    root.include_router(build_admin_consoles_router(settings))
    root.include_router(build_admin_devices_router(settings))
    root.include_router(build_admin_misc_router(settings))
    root.include_router(build_admin_keys_router(settings))
    root.include_router(build_admin_key_users_router(settings))
    root.include_router(build_admin_file_nr_router(settings))
    root.include_router(build_admin_pid_max_router(settings))
    root.include_router(build_admin_aio_nr_router(settings))
    root.include_router(build_admin_dirty_router(settings))
    root.include_router(build_admin_thp_router(settings))
    root.include_router(build_admin_max_map_count_router(settings))
    root.include_router(build_admin_nr_open_router(settings))
    root.include_router(build_admin_self_status_router(settings))
    root.include_router(build_admin_stat_router(settings))
    root.include_router(build_admin_protocols_router(settings))
    root.include_router(build_admin_loop_router(settings))
    root.include_router(build_admin_loguru_router(settings))
    root.include_router(build_admin_bot_session_router(settings))
    root.include_router(build_admin_middlewares_router(get_dispatcher, settings))
    # /admin_routes walks the dispatcher tree at message-time — pass a
    # closure that returns ``root`` so the router can introspect the
    # tree it is itself a member of (the alternative — passing ``root``
    # directly — would either capture the root pre-include and miss
    # everything wired after, or force the routes router to be the very
    # last include with a fragile ordering invariant).
    root.include_router(build_admin_routes_router(lambda: root, settings))
    root.include_router(build_admin_withdrawals_router(settings, registry))
    # #1687: the dispute queue an operator can re-open, since the
    # delivery-time card in handlers/p2p_trade.py is best-effort.
    root.include_router(build_admin_p2p_disputes_router(settings, registry))
    root.include_router(build_admin_payment_keys_router(settings, registry))
    root.include_router(build_withdraw_status_router(registry))
    root.include_router(build_withdraw_router(registry, settings))
    # #158: the last child that can match a message. Every module
    # above guards its handlers on the argument shape and lets anything
    # off-contract fall through — a contract written when legacy owned
    # those forms and answered with a usage hint. Legacy is gone, so a
    # fall-through is silence; this router turns it back into an answer.
    # Built from ``root`` BEFORE being included in it, so the walk that
    # collects the command words cannot see the router's own
    # registrations.
    root.include_router(build_unknown_form_router(root))
    # #159: the message tail's twin, for inline buttons. No filter at
    # all — a callback query that got past every registration above is
    # a tap on a card that outlived its handler, and without an
    # ``answerCallbackQuery`` Telegram spins the button for ~15s and
    # gives up silently. Built from ``root`` before inclusion for the
    # same reason as #158: the prefix walk must see the tree, not
    # itself.
    root.include_router(build_stale_callback_router(root))
    # Errors router MUST come last. aiogram dispatches event handlers
    # in include-order; the @router.error() catcher fires for any
    # handler in any earlier router that raised. If we registered it
    # first, a raised handler in a later router still triggers it
    # (errors are not order-sensitive within a dispatcher tree) — but
    # putting it last keeps the include list readable as "feature
    # routers, then the safety net" and aligns with the convention
    # most aiogram codebases follow.
    root.include_router(build_errors_router())
    return root
