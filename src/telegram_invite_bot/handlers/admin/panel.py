"""``/admin`` — unified developer-panel entry point (T-024.3).

Until now the admin control plane was a flat set of ~100 dev-gated
``/admin_*`` slash commands with no single entry point: an operator
had to *remember and type* each command. ``/admin`` existed only as
an alias for the ``/admin_help`` text index.

This handler turns ``/admin`` into a real inline-button panel:

* the **root** screen shows one button per category (Платежи,
  Выводы, Статистика, Здоровье/БД, Рантайм, Система);
* tapping a category **edits the same message** into that category's
  command list (each command + a one-line description), with a
  "⬅️ Назад" button back to root.

The panel is a *navigator*, not an *invoker*: aiogram can't synthesize
a slash command from a callback, and wiring every one of ~100 handlers
behind a button would couple this module to every admin router's
internals. Instead the operator reads the curated list and types (or
taps the rendered ``/command`` — Telegram makes ``<code>``-wrapped
slash commands tappable in many clients) the one they want. The two
commands that *do* have their own inline UI (``/admin_withdrawals``,
``/payment_keys``) are reachable the same way and keep their own
buttons once opened.

Same posture as every other admin surface:

* **Silent-drop for non-devs** on both the command and every
  callback — existence must not enumerate dev IDs.
* **Private-only** at the router level — the category lists name
  payment/PII-adjacent tooling that must never render in a group.
* The category catalog is **hand-written** (not introspected from
  the router tree) for the same reasons ``admin/help.py`` documents:
  introspection would couple the menu to internal aiogram shapes,
  the one-line descriptions are editorial, and a hand list is
  git-greppable so a new ``/admin_*`` command lands its menu entry
  in the same PR.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import AdminNav

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.config.settings import Settings


log = logger.bind(component="handlers.admin.panel")

_ROOT = "root"

# Curated category catalog. Each entry: (key, button-label, header,
# ((command, one-line-description), ...)). Keep descriptions terse —
# a category screen must stay under Telegram's 4096-char limit.
#
# A new /admin_* command should add its line to the matching category
# here in the same PR that wires its router, so the panel stays an
# honest map of the operator-facing surface.
_Bi = tuple[str, str]  # (ru, en) — same shape as economy._BALANCE_GAMES
_Section = tuple[str, _Bi, _Bi, tuple[tuple[str, _Bi], ...]]


def _pick(pair: _Bi, lang: str) -> str:
    """Russian on ``ru``, English on anything else.

    The catalog stays an inline bilingual table rather than ~120 YAML
    keys: every entry is one editorial line that must be added in the
    same PR as the command it documents, and splitting it across two
    locale files would guarantee the halves drift apart. Same idiom as
    ``economy._BALANCE_GAMES`` and ``marriage._RELATIONSHIP_LEVEL_NAMES``.
    """
    return pair[0] if lang == "ru" else pair[1]


_SECTIONS: tuple[_Section, ...] = (
    (
        "payments",
        ("💳 Платежи", "💳 Payments"),
        ("💳 <b>Платежи</b>", "💳 <b>Payments</b>"),
        (
            (
                "/payment_keys",
                (
                    "статус платёжных ключей (маскированный отпечаток)",
                    "payment-key status (masked fingerprint)",
                ),
            ),
            (
                "/set_crypto_token",
                (
                    "задать токен Crypto Pay (сообщение удаляется)",
                    "set the Crypto Pay token (the message is deleted)",
                ),
            ),
            (
                "/clear_crypto_token",
                ("удалить сохранённый токен Crypto Pay", "delete the stored Crypto Pay token"),
            ),
            (
                "/admin_transactions",
                (
                    "последние 10 строк леджера (свежие сверху)",
                    "last 10 ledger rows (newest first)",
                ),
            ),
            (
                "/admin_donations",
                (
                    "сводка донатов: всего + топ + последние",
                    "donation summary: total + top + recent",
                ),
            ),
        ),
    ),
    (
        "withdrawals",
        ("💸 Выводы", "💸 Withdrawals"),
        ("💸 <b>Выводы средств</b>", "💸 <b>Withdrawals</b>"),
        (
            (
                "/admin_withdrawals",
                (
                    "очередь заявок + кнопки ✅ approve / 🚫 reject",
                    "request queue + ✅ approve / 🚫 reject buttons",
                ),
            ),
        ),
    ),
    (
        "stats",
        ("📊 Статистика", "📊 Statistics"),
        ("📊 <b>Статистика</b>", "📊 <b>Statistics</b>"),
        (
            ("/admin_botstats", ("счётчики пользователей и групп", "user and group counters")),
            (
                "/admin_top_users",
                ("топ-10 по балансу + last_seen", "top 10 by balance + last_seen"),
            ),
            (
                "/admin_recent_signups",
                ("10 свежих регистраций — спот рейдов", "10 newest signups — spot raids"),
            ),
            ("/admin_marriages", ("браки: всего + топ-5 чатов", "marriages: total + top 5 chats")),
            (
                "/admin_relations",
                ("отношения: всего + топ-5 чатов", "relationships: total + top 5 chats"),
            ),
            (
                "/admin_rate_stats",
                ("троттлинг: самые нагруженные пользователи", "throttling: heaviest users"),
            ),
            ("/admin_shop_prices", ("дамп каталога магазина", "shop catalog dump")),
        ),
    ),
    (
        "health",
        ("🩺 Здоровье/БД", "🩺 Health/DB"),
        ("🩺 <b>Здоровье и базы данных</b>", "🩺 <b>Health and databases</b>"),
        (
            (
                "/admin_status",
                (
                    "снимок здоровья пайплайна (БД, Sentry, версия)",
                    "pipeline health snapshot (DB, Sentry, version)",
                ),
            ),
            (
                "/admin_dbprobe",
                ("SELECT 1 по каждому движку — латентность", "SELECT 1 per engine — latency"),
            ),
            (
                "/admin_integrity",
                ("integrity_check + foreign_key_check", "integrity_check + foreign_key_check"),
            ),
            (
                "/admin_db_sizes",
                ("размеры файлов БД + логический + WAL", "DB file sizes + logical + WAL"),
            ),
            (
                "/admin_pragmas",
                ("PRAGMA по движкам — дрейф WAL/FK/sync", "PRAGMAs per engine — WAL/FK/sync drift"),
            ),
            (
                "/admin_engines",
                ("снимок пула соединений по движкам", "connection-pool snapshot per engine"),
            ),
            ("/admin_tables", ("каталог таблиц + счётчики строк", "table catalog + row counts")),
            (
                "/admin_telegram_api",
                (
                    "getMe + getWebhookInfo — токен + бэклог",
                    "getMe + getWebhookInfo — token + backlog",
                ),
            ),
            (
                "/admin_dns",
                (
                    "проба getaddrinfo до api.telegram.org",
                    "getaddrinfo probe against api.telegram.org",
                ),
            ),
            (
                "/admin_certfp",
                (
                    "TLS-хендшейк — отпечаток + срок сертификата",
                    "TLS handshake — fingerprint + certificate expiry",
                ),
            ),
        ),
    ),
    (
        "runtime",
        ("⚙️ Рантайм", "⚙️ Runtime"),
        ("⚙️ <b>Рантайм процесса</b>", "⚙️ <b>Process runtime</b>"),
        (
            (
                "/admin_settings",
                (
                    "редактированный вывод Settings (секреты set/unset)",
                    "redacted Settings dump (secrets shown as set/unset)",
                ),
            ),
            (
                "/admin_routes",
                (
                    "карта маршрутов диспетчера — что реально подключено",
                    "dispatcher route map — what is actually wired",
                ),
            ),
            (
                "/admin_middlewares",
                ("цепочка middleware по обсёрверам", "middleware chain per observer"),
            ),
            (
                "/admin_python",
                (
                    "версия/исполняемый/префикс интерпретатора",
                    "interpreter version/executable/prefix",
                ),
            ),
            (
                "/admin_modules",
                (
                    "версии ключевых зависимостей (aiogram, sqla, …)",
                    "key dependency versions (aiogram, sqla, …)",
                ),
            ),
            (
                "/admin_memory",
                ("VmRSS/Peak/Swap — утечки/своп/инфляция", "VmRSS/Peak/Swap — leaks/swap/bloat"),
            ),
            (
                "/admin_tasks",
                (
                    "живые asyncio-таски — утечки/застрявшие корутины",
                    "live asyncio tasks — leaks/stuck coroutines",
                ),
            ),
            (
                "/admin_loop",
                (
                    "реализация event-loop + debug + slow-callback порог",
                    "event-loop implementation + debug + slow-callback threshold",
                ),
            ),
            (
                "/admin_uptime",
                ("время старта процесса + прошедшее", "process start time + elapsed"),
            ),
            (
                "/admin_loguru",
                (
                    "настроенные loguru-синки: id + kind + уровень",
                    "configured loguru sinks: id + kind + level",
                ),
            ),
        ),
    ),
    (
        "system",
        ("🐧 Система", "🐧 System"),
        ("🐧 <b>Система / диагностика хоста</b>", "🐧 <b>System / host diagnostics</b>"),
        (
            (
                "/admin_help",
                (
                    "полный индекс всех ~100 /admin_* команд",
                    "full index of all ~100 /admin_* commands",
                ),
            ),
            (
                "/admin_meminfo",
                ("host /proc/meminfo — давление памяти", "host /proc/meminfo — memory pressure"),
            ),
            ("/admin_loadavg", ("/proc/loadavg — перегрузка", "/proc/loadavg — overload")),
            (
                "/admin_psi",
                ("PSI cpu/memory/io — давление ресурсов", "PSI cpu/memory/io — resource pressure"),
            ),
            (
                "/admin_oom",
                (
                    "OOM-killer posture: score + adj + overcommit",
                    "OOM-killer posture: score + adj + overcommit",
                ),
            ),
            ("/admin_cpu", ("ядра + аффинити + load avg", "cores + affinity + load avg")),
            ("/admin_fds", ("перепись открытых fd по типам", "open-fd census by type")),
            (
                "/admin_netconns",
                ("перепись TCP-состояний — TIME_WAIT ⚠", "TCP-state census — TIME_WAIT ⚠"),
            ),
            (
                "/admin_disk",
                (
                    "free/used/total по каталогам — низкий запас ⚠",
                    "free/used/total per directory — low headroom ⚠",
                ),
            ),
            (
                "…",
                (
                    "и ещё ~70 системных /admin_* команд — см. /admin_help",
                    "and ~70 more system /admin_* commands — see /admin_help",
                ),
            ),
        ),
    ),
)

# key → section, built once at import for O(1) callback dispatch.
_BY_KEY: dict[str, _Section] = {key: sec for sec in _SECTIONS for key in (sec[0],)}


def _root_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Root menu: one button per category, two per row."""
    buttons = [
        InlineKeyboardButton(text=_pick(label, lang), callback_data=AdminNav(section=key).pack())
        for key, label, _header, _cmds in _SECTIONS
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _section_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Sub-screen: a single "⬅️ Назад" button back to root."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_admin_panel_back", lang),
                    callback_data=AdminNav(section=_ROOT).pack(),
                )
            ]
        ]
    )


def _render_root(settings: Settings, lang: str) -> str:
    lines = [t("h_admin_panel_title", lang), ""]
    lines.append(t("h_admin_panel_hint", lang))
    lines.append("")
    dev_ids = sorted(settings.bot.developer_ids)
    if dev_ids:
        ids_str = ", ".join(f"<code>{i}</code>" for i in dev_ids)
        lines.append(t("h_admin_panel_devs", lang, ids=ids_str))
    else:
        lines.append(t("h_admin_panel_devs_none", lang))
    return "\n".join(lines)


def _render_section(section: _Section, lang: str) -> str:
    _key, _label, header, cmds = section
    lines = [_pick(header, lang), ""]
    for cmd, desc in cmds:
        # Same escape as ``admin/help.py`` and for the same reason:
        # an entry documenting its argument (``/cmd <value>``) is
        # markup Telegram refuses, and here it costs the whole
        # category screen rather than one line. The header above is
        # deliberately NOT escaped — it carries the ``<b>`` the
        # catalog authors on purpose.
        lines.append(f"• <code>{html.escape(cmd)}</code> — {html.escape(_pick(desc, lang))}")
    return "\n".join(lines)


async def handle_admin_panel(message: Message, settings: Settings, lang: str) -> None:
    """``/admin`` — render the root panel."""
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin; silently dropped"
        )
        return
    await message.answer(_render_root(settings, lang), reply_markup=_root_keyboard(lang))
    log.bind(user_id=user.id, lang=lang).info("/admin panel rendered")


async def handle_admin_nav(
    callback: CallbackQuery, callback_data: AdminNav, settings: Settings, lang: str
) -> None:
    """A category / back tap — edit the card in place to the target screen."""
    user = callback.from_user
    if not settings.bot.is_developer(user.id):
        await callback.answer()
        return

    section_key = callback_data.section
    if section_key == _ROOT:
        text_out = _render_root(settings, lang)
        markup = _root_keyboard(lang)
    else:
        section = _BY_KEY.get(section_key)
        if section is None:
            # Unknown key (stale button after a deploy that renamed a
            # category) — ack so the spinner stops, render nothing.
            await callback.answer()
            return
        text_out = _render_section(section, lang)
        markup = _section_keyboard(lang)

    msg = callback.message
    if isinstance(msg, MessageType):
        try:
            await msg.edit_text(text_out, reply_markup=markup)
        except (TelegramBadRequest, TelegramForbiddenError):
            # Identical content (double-tap) or a too-old message — the
            # navigation is idempotent, so swallow and just ack.
            log.bind(section=section_key).debug("/admin nav edit swallowed")
    await callback.answer()
    log.bind(user_id=user.id, section=section_key).debug("/admin nav")


def build_router(settings: Settings) -> Router:
    """Private-only on both event types — the category lists name
    payment/PII-adjacent tooling (``/payment_keys``,
    ``/admin_withdrawals``) and the dev-id list on the root screen
    would be a privacy leak in a group.
    """
    router = Router(name="admin.panel")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)
    # Both event types: a router-level ``message.filter`` does NOT
    # propagate to ``callback_query`` in aiogram 3, so without this the
    # card's buttons stayed live after a forward into a group.
    router.callback_query.filter(
        lambda c: c.message is not None and c.message.chat.type == ChatType.PRIVATE
    )

    async def _entry(message: Message, lang: str) -> None:
        await handle_admin_panel(message, settings, lang)

    async def _nav(callback: CallbackQuery, callback_data: AdminNav, lang: str) -> None:
        await handle_admin_nav(callback, callback_data, settings, lang)

    # ``/admin`` used to open this panel too. It is the multi-group
    # admin panel now (``handlers/mygroups.py``) — the word every
    # group admin reaches for, and the catalog has described it as
    # "Админ-панель групп" all along. The developer surface keeps the
    # spelling that says what it is.
    router.message.register(_entry, Command("admin_panel", ignore_case=True))
    router.callback_query.register(_nav, AdminNav.filter(), F.from_user)
    return router
