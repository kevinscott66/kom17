"""End-to-end ``/admin_resolver``.

Pins:

* Non-developer → silent drop.
* Live card renders.
* No /etc/resolv.conf → informational, NO ⚠.
* resolv.conf exists, empty nameservers → ⚠ (the unambiguous
  static misconfig this card detects).
* resolv.conf with nameservers → NO ⚠ regardless of which IPs.
* search + options surfaced verbatim.
* No /etc/nsswitch.conf → informational note, no ⚠.
* nsswitch hosts: line parsed; bracketed action specifiers
  stripped from rendered output.
* Multiple hosts: lines → last one wins (matches glibc).
* Comments stripped in both parsers.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.resolver import (
    _capture_nsswitch,
    _capture_resolv,
    _empty_nameservers_concerning,
    _NsswitchSnapshot,
    _parse_nsswitch_hosts,
    _parse_resolv_conf,
    _render,
    _ResolvSnapshot,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


def _resolv(
    *,
    nameservers: tuple[str, ...] = (),
    search: tuple[str, ...] = (),
    options: tuple[str, ...] = (),
    status_present: bool = True,
) -> _ResolvSnapshot:
    return _ResolvSnapshot(
        nameservers=nameservers,
        search=search,
        options=options,
        status_present=status_present,
    )


def _nsswitch(*, sources: tuple[str, ...] = (), status_present: bool = True) -> _NsswitchSnapshot:
    return _NsswitchSnapshot(sources=sources, status_present=status_present)


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_resolver", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_card_renders(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_resolver", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "DNS resolver configuration" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_resolver",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


def test_no_resolv_conf_no_warn() -> None:
    """Absent /etc/resolv.conf → informational, zero ⚠."""
    rendered = _render(_resolv(status_present=False), _nsswitch(status_present=False))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0
    assert "file not present" in head


def test_empty_nameservers_warn() -> None:
    """resolv.conf present + zero nameservers → ⚠. The single
    unambiguous static misconfig this card detects."""
    rendered = _render(_resolv(), _nsswitch(status_present=False))
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 1
    assert "no nameservers configured" in head


def test_with_nameservers_no_warn() -> None:
    """Any nameserver value → NO ⚠. Stub resolver at 127.0.0.53
    is systemd's default and not a problem — pinned here so a
    future tightening doesn't accidentally cry wolf."""
    rendered = _render(
        _resolv(nameservers=("127.0.0.53", "8.8.8.8")),
        _nsswitch(sources=("files", "dns")),
    )
    head, _, _ = rendered.partition("<i>⚠ markers")
    assert head.count("⚠") == 0
    assert "127.0.0.53" in head
    assert "8.8.8.8" in head


def test_search_and_options_surfaced() -> None:
    """search domains + options must appear verbatim in the
    card — they're the footgun signal."""
    rendered = _render(
        _resolv(
            nameservers=("1.1.1.1",),
            search=("local", "internal.example.com"),
            options=("ndots:5", "timeout:1"),
        ),
        _nsswitch(status_present=False),
    )
    assert "internal.example.com" in rendered
    assert "ndots:5" in rendered
    assert "timeout:1" in rendered


def test_nsswitch_sources_in_order() -> None:
    """The lookup-order chain must render in the order it
    appears in nsswitch.conf — operator's mental model is
    'first match wins'."""
    rendered = _render(
        _resolv(nameservers=("1.1.1.1",)),
        _nsswitch(sources=("files", "mdns4_minimal", "dns")),
    )
    # Render uses " → " between sources for the chain feel.
    assert "files" in rendered
    assert "mdns4_minimal" in rendered
    # Order: files appears before dns in the rendered string.
    assert rendered.index("files") < rendered.index("dns")


def test_parse_resolv_conf_basic() -> None:
    """Pin the basic shape."""
    text = (
        "# managed by systemd-resolved\n"
        "nameserver 127.0.0.53\n"
        "nameserver 1.1.1.1\n"
        "search local.example.com\n"
        "options ndots:5 timeout:1\n"
    )
    ns, search, opts = _parse_resolv_conf(text)
    assert ns == ("127.0.0.53", "1.1.1.1")
    assert search == ("local.example.com",)
    assert opts == ("ndots:5", "timeout:1")


def test_parse_resolv_conf_strips_comments() -> None:
    """Both ``#`` and ``;`` are comment markers per resolv.conf(5)."""
    text = "nameserver 1.1.1.1 ; trailing comment\n# entire line\n"
    ns, _, _ = _parse_resolv_conf(text)
    assert ns == ("1.1.1.1",)


def test_parse_resolv_conf_legacy_domain_keyword() -> None:
    """``domain`` is the legacy single-arg form of ``search``;
    treat it as a one-entry search list so the operator's mental
    model stays uniform."""
    text = "domain corp.example.com\n"
    _, search, _ = _parse_resolv_conf(text)
    assert search == ("corp.example.com",)


def test_parse_nsswitch_hosts_strips_brackets() -> None:
    """Bracketed action specifiers like [NOTFOUND=return] must
    not appear in the rendered source list."""
    text = "hosts: files mdns4_minimal [NOTFOUND=return] dns\n"
    sources = _parse_nsswitch_hosts(text)
    assert sources == ("files", "mdns4_minimal", "dns")


def test_parse_nsswitch_hosts_last_line_wins() -> None:
    """Multiple hosts: lines → last one wins, matching glibc.
    Rare but legal and a real footgun in stacked include files."""
    text = "hosts: files\nhosts: files dns\n"
    sources = _parse_nsswitch_hosts(text)
    assert sources == ("files", "dns")


def test_parse_nsswitch_hosts_missing_returns_empty() -> None:
    """No hosts: line → empty tuple. Render shows the glibc
    fallback note rather than ⚠'ing."""
    text = "passwd: files\ngroup: files\n"
    sources = _parse_nsswitch_hosts(text)
    assert sources == ()


def test_empty_nameservers_concerning_predicate() -> None:
    """⚠ predicate. Only fires when the file IS present AND
    the nameserver list is empty — the unambiguous static
    misconfig case."""
    # Present + empty → ⚠
    assert _empty_nameservers_concerning(_resolv())
    # Present + populated → no ⚠
    assert not _empty_nameservers_concerning(_resolv(nameservers=("1.1.1.1",)))
    # Absent → no ⚠ (non-Linux signal, not a problem)
    assert not _empty_nameservers_concerning(_resolv(status_present=False))


def test_capture_resolv_with_fixture(tmp_path: Path) -> None:
    """Real _capture against tmp_path — hermetic."""
    f = tmp_path / "resolv.conf"
    f.write_text("nameserver 8.8.8.8\nsearch corp\n")
    snap = _capture_resolv(path=f)
    assert snap.status_present
    assert snap.nameservers == ("8.8.8.8",)
    assert snap.search == ("corp",)


def test_capture_resolv_missing(tmp_path: Path) -> None:
    """Absent → status_present=False, all fields empty."""
    snap = _capture_resolv(path=tmp_path / "does_not_exist")
    assert not snap.status_present
    assert snap.nameservers == ()
    assert snap.search == ()
    assert snap.options == ()


def test_capture_nsswitch_with_fixture(tmp_path: Path) -> None:
    f = tmp_path / "nsswitch.conf"
    f.write_text("passwd: files\nhosts: files dns\n")
    snap = _capture_nsswitch(path=f)
    assert snap.status_present
    assert snap.sources == ("files", "dns")


def test_capture_nsswitch_missing(tmp_path: Path) -> None:
    snap = _capture_nsswitch(path=tmp_path / "does_not_exist")
    assert not snap.status_present
    assert snap.sources == ()
