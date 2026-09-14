"""``/admin_help`` — developer-only index of admin commands.

Legacy ``/admin_help`` / ``/owner_help`` (bot.py:25504) renders a long
free-form text plus an inline-keyboard button that points to the
Telegraph guide URL. The Telegraph integration is part of the
``cms/guide_site/`` package that hasn't migrated yet — and the
"long free-form text" content was hand-curated against the legacy
command surface, most of which no longer maps 1:1 onto the new
pipeline anyway.

What an operator actually needs from ``/admin_help`` during a real
session is **"which admin commands does THIS bot currently expose,
and what does each do?"**. We render that as a structured index of
every ``/admin_*`` handler wired into the new pipeline. The list is
written by hand (not introspected from the router tree) because:

* Introspection would couple the help text to internal aiogram
  router shapes — a future Dispatcher refactor would silently drop
  entries from the help card.
* The one-line description per command is editorial content that
  doesn't exist on the router (Command filter doesn't carry a
  docstring), so we'd need a parallel table anyway. Better to have
  the help text BE that table.
* A hand-written index is git-greppable: when a new ``/admin_*``
  command lands, the same PR adds its line here, so reviewers see
  the operator-facing surface change.

Relying on reviewers to notice missed six commands, so the coverage
half of that bullet is now a test: ``test_index_covers_every_wired_
admin_command`` walks the live tree and fails when something is wired
under an ``admin.*`` router but absent here. Rendering stays hand-
written for the reasons above — only the completeness check is
introspected. An entry's first token is the command; further ``/``-
prefixed tokens are aliases (``/admin_help /owner_help``), and
non-``/`` tokens are argument placeholders.

Silent-drop for non-devs, same posture as every other admin command:
existence of the command must not leak dev IDs. Private-only at the
router level — legacy command is gated identically (the admin
control plane never renders in groups).

When the Telegraph guide migrates (under ``cms/``), this handler
gains a second line pointing at the URL — but the inline button is
out of scope here because aiogram's keyboard plumbing isn't worth
pulling in for one URL the operator can copy-paste.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.utils.render import paginate_lines

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.help")


# One-line description per migrated /admin_* command. Order matches
# main_router.py's include order, so an operator reading top-to-bottom
# sees commands in roughly the same grouping they'd find in code.
# Keep entries terse — long descriptions push the card past Telegram's
# 4096-char limit once the catalog grows.
_ADMIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/admin_status", "pipeline health snapshot (DBs, Sentry, throttle, version)"),
    ("/admin_botstats", "users + groups row counts"),
    ("/admin_shop_prices", "shop catalog dump"),
    ("/admin_rate_stats", "throttling bucket snapshot — top pressured users"),
    ("/admin_check_groups", "groups-DB diagnostics (counts + sample rows)"),
    ("/admin_test_log", "emit a synthetic ERROR — verifies the log sink chain"),
    ("/admin_loguru", "configured loguru sinks: id + kind + min level"),
    ("/admin_bot_session", "aiogram Bot session: API base, timeout, default parse_mode"),
    ("/admin_withdrawals", "pending withdrawal requests + fiat-sum + sample"),
    ("/admin_p2p_disputes", "open P2P disputes + re-issued resolve buttons"),
    ("/admin_transactions", "latest 10 ledger rows (newest first) for anomaly scan"),
    ("/admin_donations", "donations summary: total + top donors + recent sample"),
    ("/admin_top_users", "top 10 economy.users by balance + last_seen"),
    ("/admin_recent_signups", "newest 10 economy.users by registered — spot raids"),
    ("/admin_marriages", "active-marriage totals + top 5 chats by count"),
    ("/admin_relations", "active-relationship totals + top 5 chats by count"),
    ("/admin_pragmas", "per-engine PRAGMA readout — verify WAL/FK/sync drift"),
    ("/admin_db_sizes", "per-DB file + logical + WAL sizes; flags stuck checkpoints"),
    ("/admin_modules", "installed versions of load-bearing deps (aiogram, sqla, …)"),
    ("/admin_python", "interpreter identity: version, executable, prefix, platform"),
    ("/admin_proc", "PID + peak RSS + cumulative user/system CPU (leak/hot triage)"),
    ("/admin_fdlimit", "NOFILE/AS/RSS rlimits (soft/hard) — predict EMFILE/OOM"),
    ("/admin_uptime", "process boot time + NTP-safe elapsed (spot silent restarts)"),
    ("/admin_clock", "host wall/local/monotonic + tzname — NTP-step + TZ misconfig"),
    ("/admin_routes", "live dispatcher route map — verify what's actually wired"),
    ("/admin_middlewares", "per-observer outer/inner middleware chain — verify throttle/DI wiring"),
    ("/admin_settings", "redacted readout of resolved Settings (secrets as set/unset)"),
    ("/admin_integrity", "per-DB integrity_check + foreign_key_check — bit-rot + orphan scan"),
    ("/admin_engines", "per-engine pool snapshot — checked_out/in, ⚠ on ceiling"),
    ("/admin_disk", "free/used/total per configured dir — ⚠ on low headroom"),
    ("/admin_tables", "per-engine table catalog + row counts (largest first)"),
    ("/admin_indexes", "per-engine index catalog grouped by table — spot missing coverage"),
    ("/admin_tasks", "live asyncio task sample — leak + stuck-coroutine spot"),
    ("/admin_threads", "live OS-thread sample — legacy-thread migration audit"),
    ("/admin_gc", "Python GC: enabled + per-gen counts/thresholds/collections"),
    ("/admin_signals", "POSIX signal handlers — ⚠ on SIG_IGN / SIG_DFL for SIGTERM"),
    ("/admin_hostinfo", "host identity: node + fqdn + uname (blue/green deploy verify)"),
    ("/admin_ssl", "OpenSSL runtime + trust-store + TLS-protocol flags"),
    ("/admin_locale", "LC_ALL + LANG + per-stream encodings — non-UTF-8 ⚠ marker"),
    ("/admin_pythonpath", "sys.path import order + PYTHONPATH env — shadowing audit"),
    ("/admin_warnings", "warnings.filters list — 🔇 ignore / 💥 error markers"),
    ("/admin_flags", "sys.flags: debug/optimize/dev_mode/no_user_site/hash_randomization etc."),
    ("/admin_cpu", "logical cores + scheduling affinity + load avg — pinning/saturation"),
    ("/admin_runtime", "recursionlimit/switchinterval/int_max_str_digits/maxsize tunables"),
    ("/admin_fds", "open fd census + by-kind tally (regular/socket/pipe/anon_inode)"),
    ("/admin_memory", "VmRSS/Peak/HWM/Size/Data/Stk/Swap — leak + swap + inflation signals"),
    ("/admin_rusage", "getrusage: page faults + ctx switches + block I/O cumulative"),
    ("/admin_tempdir", "tempfile.gettempdir + writability probe + free-space + env vars"),
    ("/admin_kernel", "kernel version (CVE awareness) + boot cmdline + concerning-token scan"),
    ("/admin_hashlib", "hashlib algorithms_available + pbkdf2/scrypt probe — FIPS smoking-gun"),
    ("/admin_imports", "sys.modules census: total + builtin/frozen + load-bearing + heaviest"),
    ("/admin_dns", "async getaddrinfo probe of api.telegram.org — latency + A records"),
    ("/admin_dbprobe", "per-engine SELECT 1 liveness — latency + ⚠ on busy_timeout"),
    ("/admin_telegram_api", "live getMe + getWebhookInfo probe — token + backlog + last_error"),
    ("/admin_envscan", "credential-shaped env-var audit (key names + masked length/preview)"),
    ("/admin_certfp", "TLS handshake to api.telegram.org — fingerprint + expiry ⚠"),
    ("/admin_codecs", "default encodings + load-bearing codec registry — mojibake diagnosis"),
    ("/admin_random", "CSPRNG source probe + /dev/urandom + kernel entropy_avail"),
    ("/admin_netconns", "TCP socket-state census from /proc/net/tcp{,6} — TIME_WAIT ⚠"),
    ("/admin_oom", "OOM-killer posture: oom_score + adj + overcommit + panic_on_oom"),
    ("/admin_capabilities", "Linux CapEff/CapPrm/CapBnd/… — root-equivalent ⚠ + CAP_SYS_ADMIN ⚠"),
    ("/admin_cgroup", "cgroup membership + memory.max/cpu.max/pids.max — pressure ⚠"),
    ("/admin_group_migrate", "re-key a group's rows old chat_id → new — supergroup-upgrade repair"),
    ("/admin_io", "/proc/self/io: rchar/wchar/syscr/syscw/read_bytes/write_bytes/cancelled ⚠"),
    ("/admin_smaps", "PSS + shared/private/swap accounting — honest per-process memory cost"),
    ("/admin_resolver", "/etc/resolv.conf nameservers + search + options + nsswitch hosts: line"),
    ("/admin_limits", "full ulimit -a — every RLIMIT_* (NOFILE/AS/RSS/NPROC/FSIZE/CPU/STACK/…)"),
    ("/admin_audit", "sys.audit (PEP 578) liveness probe — pipeline-broken ⚠"),
    ("/admin_meminfo", "host /proc/meminfo: MemTotal/Available/Free/Cached/Swap — pressure ⚠"),
    ("/admin_mounts", "/proc/self/mounts: fstype + ro/noexec/nosuid flags — rootfs-ro ⚠"),
    ("/admin_loadavg", "/proc/loadavg: 1/5/15-min + R/total tasks + last_pid — overload ⚠"),
    ("/admin_diskstats", "/proc/diskstats: per-device read/write/in-flight — saturation ⚠"),
    ("/admin_sysctl", "curated /proc/sys: somaxconn/port_range/fin_timeout/file-max — somaxconn ⚠"),
    ("/admin_swaps", "/proc/swaps: per-device swap layout (type, size, used, priority)"),
    ("/admin_route", "IPv4 /proc/net/route: destination/gateway/iface/flags — no-default-route ⚠"),
    ("/admin_tcpext", "/proc/net/netstat TcpExt counters — accept-queue overflow ⚠"),
    ("/admin_psi", "PSI /proc/pressure/{cpu,memory,io}: avg10/60/300 — full pressure ⚠"),
    ("/admin_netdev", "/proc/net/dev per-interface RX/TX bytes/packets/errs/drop — errs ⚠"),
    ("/admin_vmstat", "/proc/vmstat curated: oom_kill/pswpin/pswpout/pgmajfault — oom_kill ⚠"),
    ("/admin_sockstat", "/proc/net/sockstat aggregates: TCP inuse/orphan/tw/alloc/mem + v6"),
    ("/admin_softirqs", "/proc/softirqs per-CPU softirq distribution — NET_RX skew lens"),
    ("/admin_interrupts", "/proc/interrupts per-CPU hardware IRQ distribution"),
    ("/admin_buddyinfo", "/proc/buddyinfo per-zone free-list — high-order exhaustion ⚠"),
    ("/admin_arp", "/proc/net/arp neighbour table — many INCOMPLETE entries ⚠"),
    ("/admin_zoneinfo", "/proc/zoneinfo per-zone watermarks — kswapd / direct reclaim ⚠"),
    ("/admin_slabinfo", "/proc/slabinfo kernel slab caches — top N by footprint"),
    ("/admin_locks", "/proc/locks file-lock table — blocked-waiter count ⚠"),
    ("/admin_partitions", "/proc/partitions block-device inventory — top N by raw size"),
    ("/admin_filesystems", "/proc/filesystems registered FS drivers — block-backed vs pseudo"),
    ("/admin_cmdline", "/proc/cmdline kernel boot parameters — notable security/perf flags"),
    ("/admin_crypto", "/proc/crypto registered kernel crypto — failed self-test count ⚠"),
    ("/admin_consoles", "/proc/consoles registered kernel consoles — zero enabled ⚠"),
    ("/admin_devices", "/proc/devices registered char/block driver majors — no warnings"),
    ("/admin_misc", "/proc/misc misc-major (10) driver table — kvm/fuse/tun/hwrng presence"),
    ("/admin_keys", "/proc/keys visible kernel keyring — revoked/expired count ⚠"),
    ("/admin_key_users", "/proc/key-users per-uid keyring quota — 80% threshold ⚠"),
    ("/admin_file_nr", "/proc/sys/fs/file-nr — system-wide fd table vs fs.file-max ⚠"),
    ("/admin_pid_max", "kernel.pid_max + threads-max vs current task count ⚠"),
    ("/admin_aio_nr", "fs.aio-nr vs fs.aio-max-nr — kernel AIO context usage ⚠"),
    ("/admin_dirty", "dirty-page writeback pressure vs vm.dirty_ratio ⚠"),
    ("/admin_thp", "Transparent Huge Pages mode + AnonHugePages usage ⚠"),
    ("/admin_max_map_count", "VMA count vs vm.max_map_count — per-process mmap ceiling ⚠"),
    ("/admin_nr_open", "RLIMIT_NOFILE hard vs fs.nr_open — kernel ceiling on ceiling ⚠"),
    ("/admin_self_status", "/proc/self/status — TracerPid ⚠ + CoreDumping ⚠ + Seccomp posture"),
    ("/admin_stat", "/proc/stat scalars — btime/processes/ctxt + procs_blocked ⚠"),
    ("/admin_protocols", "/proc/net/protocols families — sockets + memory pressure ⚠"),
    ("/admin_loop", "event-loop impl + debug flag + slow-callback threshold"),
    ("/admin_help /owner_help", "this card"),
    # #2003: ``/deploy`` is the short name an operator's fingers reach
    # for; it is registered on the same handler behind the same gate,
    # so the index lists both spellings the way ``/admin_help`` does.
    ("/admin_deploy /deploy", "reminder that deploy is a shell command, not Telegram"),
    ("/admin_panel", "the same surface as buttons — categories, not one long list"),
    ("/payment_keys", "which payment credentials are configured (masked, never printed)"),
    ("/set_crypto_token <token>", "store the Crypto Pay token in the runtime settings"),
    ("/clear_crypto_token", "drop the stored Crypto Pay token"),
)


def _render_pages(settings: Settings) -> list[str]:
    """Render the index as messages Telegram will actually accept.

    The catalog outgrew a single message long ago: 108 entries measure
    ~8 600 parsed characters, more than twice the 4 096 ceiling, so
    every ``/admin_help`` invocation failed with a 400 and the operator
    got nothing back. The comment above ``_ADMIN_COMMANDS`` asking for
    terse descriptions was the mitigation, and it was never going to
    hold — the card grows by one line per migrated command by design.

    Splitting rather than truncating: this card's whole job is to be
    the complete index, and a silently-dropped tail would be worse than
    the 400 it replaces, because it looks like it worked.
    """
    header = "\n".join(
        [
            "🛠 <b>Admin command index</b>",
            "",
            "<i>Developer-only commands wired into the new pipeline. "
            "Each is silent-drop for non-devs and (where shown) "
            "private-chat-only.</i>",
            "",
        ]
    )
    # Escaped because the catalog documents arguments the way a CLI
    # does — ``/set_crypto_token <token>`` — and Telegram parses a
    # message whole or not at all. Raw, that placeholder reads as a
    # start tag the Bot API does not know, the send comes back
    # ``Unsupported start tag "token"``, and the whole page is lost:
    # the operator asked for the index of the admin surface and got
    # silence. Both halves are escaped, not just the command, because
    # a description is the other place an angle bracket lands (the
    # ``/admin_*`` cards learned this in
    # ``tests/regression/test_admin_card_html_safety.py``), and
    # neither half is markup — the ``<code>`` wrapper is.
    lines = [
        f"• <code>{html.escape(cmd)}</code> — {html.escape(desc)}" for cmd, desc in _ADMIN_COMMANDS
    ]
    lines.append("")
    # Operator wants to know which IDs are recognised as dev — the
    # rest of the admin commands silent-drop for everyone else, so
    # showing the recognised set here is the one place a new
    # operator can confirm their ID actually lands them in the dev
    # bucket. Show as a sorted list so the order is stable.
    dev_ids = sorted(settings.bot.developer_ids)
    if dev_ids:
        ids_str = ", ".join(f"<code>{i}</code>" for i in dev_ids)
        lines.append(f"<b>Recognised developer ids:</b> {ids_str}")
    else:
        lines.append("<b>Recognised developer ids:</b> <i>none configured</i>")
    return paginate_lines(
        header,
        lines,
        more_line=lambda left: f"<i>… and {left} more (index truncated)</i>",
    )


async def handle_admin_help(message: Message, settings: Settings) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_help; silently dropped"
        )
        return
    for page in _render_pages(settings):
        await message.answer(page)
    log.bind(user_id=user.id).info("/admin_help rendered")


def build_router(settings: Settings) -> Router:
    """Private-only at the router level — the legacy command short-
    circuits the admin control plane in groups (only the deep-link to
    DM is rendered there), and the dev-id list we surface here would
    be a real privacy leak in a non-private chat.
    """
    router = Router(name="admin.help")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_help(message, settings)

    # ``/admin_help`` / ``/owner_help`` render this full text index.
    # Bare ``/admin`` is now owned by ``handlers/admin/panel.py`` — the
    # unified inline-button panel — which links back here (its "🐧
    # Система" category lists ``/admin_help`` as the full index). T-024.3.
    router.message.register(
        _entry,
        Command("admin_help", "owner_help", ignore_case=True),
    )
    return router
