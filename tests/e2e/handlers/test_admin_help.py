"""End-to-end ``/admin_help``.

Pins:

* Non-developer → silent drop.
* Developer in private → card lists every wired /admin_* command.
* Developer ids surfaced — operator can confirm their id is
  recognised.
* ``/owner_help`` alias matches.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

import html
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.config.settings import Settings
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.mark.asyncio
async def test_silent_for_non_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_help", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_for_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_help", user_id=555, chat_type="private")
    )
    # The catalog does not fit in one message and is paginated — the
    # contract is "every command is listed", not "in one bubble".
    assert len(sent) > 1
    text = "\n".join(m["text"] for m in sent)
    assert "Admin command index" in sent[0]["text"]
    # Every wired /admin_* command must be listed — drift between the
    # router include set and this card is exactly the regression the
    # hand-written index is meant to catch (see module docstring).
    for cmd in (
        "/admin_status",
        "/admin_botstats",
        "/admin_shop_prices",
        "/admin_rate_stats",
        "/admin_check_groups",
        "/admin_test_log",
        "/admin_loguru",
        "/admin_bot_session",
        "/admin_withdrawals",
        "/admin_transactions",
        "/admin_donations",
        "/admin_top_users",
        "/admin_recent_signups",
        "/admin_marriages",
        "/admin_relations",
        "/admin_pragmas",
        "/admin_db_sizes",
        "/admin_modules",
        "/admin_python",
        "/admin_proc",
        "/admin_fdlimit",
        "/admin_uptime",
        "/admin_clock",
        "/admin_routes",
        "/admin_middlewares",
        "/admin_settings",
        "/admin_integrity",
        "/admin_engines",
        "/admin_disk",
        "/admin_tables",
        "/admin_indexes",
        "/admin_tasks",
        "/admin_threads",
        "/admin_gc",
        "/admin_signals",
        "/admin_hostinfo",
        "/admin_ssl",
        "/admin_locale",
        "/admin_pythonpath",
        "/admin_warnings",
        "/admin_flags",
        "/admin_cpu",
        "/admin_runtime",
        "/admin_fds",
        "/admin_memory",
        "/admin_rusage",
        "/admin_tempdir",
        "/admin_kernel",
        "/admin_hashlib",
        "/admin_imports",
        "/admin_dns",
        "/admin_dbprobe",
        "/admin_telegram_api",
        "/admin_envscan",
        "/admin_certfp",
        "/admin_codecs",
        "/admin_random",
        "/admin_netconns",
        "/admin_oom",
        "/admin_capabilities",
        "/admin_cgroup",
        "/admin_io",
        "/admin_smaps",
        "/admin_resolver",
        "/admin_limits",
        "/admin_loop",
        "/admin_audit",
        "/admin_meminfo",
        "/admin_mounts",
        "/admin_loadavg",
        "/admin_diskstats",
        "/admin_sysctl",
        "/admin_swaps",
        "/admin_route",
        "/admin_tcpext",
        "/admin_psi",
        "/admin_netdev",
        "/admin_vmstat",
        "/admin_sockstat",
        "/admin_softirqs",
        "/admin_interrupts",
        "/admin_buddyinfo",
        "/admin_arp",
        "/admin_zoneinfo",
        "/admin_slabinfo",
        "/admin_locks",
        "/admin_partitions",
        "/admin_filesystems",
        "/admin_cmdline",
        "/admin_crypto",
        "/admin_consoles",
        "/admin_devices",
        "/admin_misc",
        "/admin_keys",
        "/admin_key_users",
        "/admin_file_nr",
        "/admin_pid_max",
        "/admin_aio_nr",
        "/admin_dirty",
        "/admin_thp",
        "/admin_max_map_count",
        "/admin_nr_open",
        "/admin_self_status",
        "/admin_stat",
        "/admin_protocols",
        "/admin_help",
        "/admin_deploy",
    ):
        assert cmd in text


@pytest.mark.asyncio
async def test_surfaces_developer_ids(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Operator must be able to confirm their id is recognised — the
    silent-drop posture of every other admin command means this card
    is the one place a misconfigured DEVELOPER_ID_* env var becomes
    visible without shell access."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(
            BOT_TOKEN=SecretStr("1:abc"),
            DEVELOPER_ID_1=555,
            DEVELOPER_ID_2=777,
        ),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_help", user_id=555, chat_type="private")
    )
    text = "\n".join(m["text"] for m in sent)
    assert "Recognised developer ids" in text
    assert "555" in text
    assert "777" in text


@pytest.mark.asyncio
async def test_owner_help_alias(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Legacy parity: ``/owner_help`` is an alias of ``/admin_help``."""
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/owner_help", user_id=42, chat_type="private")
    )
    assert sent, "the alias must answer"
    assert "Admin command index" in sent[0]["text"]


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update("/admin_help", user_id=42, chat_id=-100_555, chat_type="supergroup"),
    )
    assert result is UNHANDLED
    assert sent == []


def test_every_page_fits_telegram_ceiling() -> None:
    """The card that could not be delivered at all until it was split.

    108 entries measure ~8 600 parsed characters; Telegram accepts
    4 096. Every ``/admin_help`` was a silent 400. This pin fails the
    moment the catalog grows past what the pager can carry, which is
    the only warning anyone gets — the failure mode on the wire is an
    empty chat.
    """
    from telegram_invite_bot.handlers.admin.help import _ADMIN_COMMANDS, _render_pages
    from telegram_invite_bot.utils.render import TELEGRAM_TEXT_LIMIT, parsed_length

    settings = cast(
        "Settings",
        SimpleNamespace(bot=SimpleNamespace(developer_ids={555, 777})),
    )
    pages = _render_pages(settings)

    assert pages, "the index must render at least one page"
    for index, page in enumerate(pages):
        assert parsed_length(page) <= TELEGRAM_TEXT_LIMIT, (
            f"page {index + 1}/{len(pages)} is {parsed_length(page)} chars"
        )

    joined = "\n".join(pages)
    assert "truncated" not in joined, "the pager ran out of pages — raise max_pages"
    # Escaped needle: the renderer escapes each entry, so an entry
    # documenting its argument (``/set_crypto_token <token>``) is on
    # the page as ``&lt;token&gt;``. Comparing against the raw catalog
    # string would report that entry as dropped forever — and, worse,
    # would make the honest fix to the 400 look like a regression
    # here. What this pin is for is the pager silently losing a tail,
    # and escaping both sides keeps exactly that falsifiable.
    missing = [cmd for cmd, _ in _ADMIN_COMMANDS if html.escape(cmd) not in joined]
    assert not missing, f"pagination dropped commands: {missing}"


@pytest.mark.asyncio
async def test_index_covers_every_wired_admin_command(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Turn "the same PR adds its line here" into something CI enforces.

    The module docstring argues — correctly — against *rendering* the
    index by introspection: the one-line descriptions are editorial and
    a Dispatcher refactor must not be able to silently empty the card.
    That argument does not extend to *checking*. Rendering stays hand-
    written; this test walks the live tree and fails when a command is
    wired under an ``admin.*`` router but missing from the catalog, so
    the omission surfaces in the PR that caused it rather than the next
    time an operator goes looking for a command that is not listed.
    """
    from telegram_invite_bot.handlers.admin.help import _ADMIN_COMMANDS
    from telegram_invite_bot.handlers.admin.routes import _walk

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    capture_outgoing(bot)

    wired = {
        f"/{command}"
        for name, commands in _walk(dispatcher)
        if name is not None and name.startswith("admin.")
        for command in commands
    }
    # An entry may carry aliases ("/admin_help /owner_help") and argument
    # placeholders ("/set_crypto_token <token>"); only the ``/``-prefixed
    # tokens are commands.
    listed = {
        token for entry, _ in _ADMIN_COMMANDS for token in entry.split() if token.startswith("/")
    }

    missing = sorted(wired - listed)
    assert not missing, f"wired but absent from /admin_help: {missing}"
