"""End-to-end ``/admin_crypto``.

Pins:

* Non-developer → silent drop.
* Card renders (Linux) OR unavailable note (macOS / non-procfs).
* Parser eats blank-line-separated key:value blocks; unknown keys
  ignored (forward-compat); blocks missing a required key dropped.
* Trailing block without blank-line terminator is still emitted.
* Self-test discriminator: 'passed' → selftest_passed=True;
  'failed' and 'unknown' both → False (operator-response-same).
* ⚠ fires iff failed_selftest_count > 0; must NOT fire when all
  passed.
* By-type aggregation surfaces in render.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.handlers.admin.crypto import (
    _capture,
    _CryptoAlgo,
    _CryptoSnapshot,
    _parse_crypto,
    _render,
)
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from tests.e2e.handlers.conftest import WiredFactory


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_crypto", user_id=42, chat_type="private"),
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
        make_message_update("/admin_crypto", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Kernel crypto algorithms" in text


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
            "/admin_crypto",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []


# --- parser ----------------------------------------------------------------


_SAMPLE = (
    "name         : sha256\n"
    "driver       : sha256-generic\n"
    "module       : kernel\n"
    "priority     : 100\n"
    "refcnt       : 4\n"
    "selftest     : passed\n"
    "internal     : no\n"
    "type         : shash\n"
    "blocksize    : 64\n"
    "digestsize   : 32\n"
    "\n"
    "name         : aes\n"
    "driver       : aes-aesni\n"
    "module       : kernel\n"
    "priority     : 300\n"
    "refcnt       : 1\n"
    "selftest     : passed\n"
    "internal     : no\n"
    "type         : cipher\n"
    "blocksize    : 16\n"
    "\n"
    "name         : md5\n"
    "driver       : md5-generic\n"
    "module       : kernel\n"
    "priority     : 0\n"
    "refcnt       : 1\n"
    "selftest     : passed\n"
    "internal     : no\n"
    "type         : shash\n"
    "blocksize    : 64\n"
)


def test_parse_canonical() -> None:
    algos = _parse_crypto(_SAMPLE)
    assert len(algos) == 3
    by_name = {a.name: a for a in algos}
    assert by_name["sha256"].driver == "sha256-generic"
    assert by_name["sha256"].type == "shash"
    assert by_name["aes"].driver == "aes-aesni"
    assert by_name["aes"].selftest_passed is True


def test_parse_trailing_block_without_blank_line() -> None:
    """The last block in /proc/crypto often has no trailing blank
    line. Pinned because a naive parser that flushes only on blank
    lines would lose the last algorithm — and in practice the
    last block can be the most operationally interesting one."""
    text = "name : x\ndriver : x-gen\nmodule : kernel\ntype : cipher\nselftest : passed\n"
    algos = _parse_crypto(text)
    assert [a.name for a in algos] == ["x"]


def test_parse_block_missing_required_key_dropped() -> None:
    """A block missing any of (name, driver, module, type,
    selftest) is dropped — defensive against partial reads.
    Pinned because silent acceptance of incomplete data could
    produce a None-laden _CryptoAlgo and crash render."""
    text = (
        "name : incomplete\n"  # missing driver, etc.
        "type : cipher\n"
        "\n"
        "name : ok\ndriver : ok-gen\nmodule : kernel\n"
        "type : cipher\nselftest : passed\n"
    )
    algos = _parse_crypto(text)
    assert [a.name for a in algos] == ["ok"]


def test_parse_unknown_keys_ignored() -> None:
    """Future kernels may add keys we don't model. The parser
    must ignore them, not crash. Pinned forward-compat."""
    text = (
        "name : x\ndriver : x-gen\nmodule : kernel\ntype : cipher\n"
        "selftest : passed\n"
        "future_field_2030 : magic_value\n"
        "yet_another_extension : xyz\n"
    )
    algos = _parse_crypto(text)
    assert len(algos) == 1
    assert algos[0].name == "x"


def test_parse_selftest_unknown_treated_as_not_passed() -> None:
    """``selftest : unknown`` — operator response is the same as
    'failed' (investigate), so we surface it identically. Pinned
    because conflating 'unknown' with 'passed' would silently hide
    untested drivers."""
    text = "name : t\ndriver : t-gen\nmodule : kernel\ntype : cipher\nselftest : unknown\n"
    algos = _parse_crypto(text)
    assert algos[0].selftest_passed is False
    assert algos[0].selftest_raw == "unknown"


def test_parse_empty() -> None:
    assert _parse_crypto("") == ()


# --- capture ---------------------------------------------------------------


def test_capture_missing(tmp_path: Path) -> None:
    snap = _capture(path=tmp_path / "absent")
    assert not snap.available
    assert snap.algos == ()


def test_capture_present(tmp_path: Path) -> None:
    p = tmp_path / "crypto"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    assert snap.available
    assert len(snap.algos) == 3
    assert snap.failed_selftest_count == 0


# --- render ----------------------------------------------------------------


def test_render_unavailable() -> None:
    snap = _CryptoSnapshot(algos=(), available=False)
    text = _render(snap)
    assert "unavailable" in text
    assert "⚠" not in text


def test_render_empty_but_available() -> None:
    snap = _CryptoSnapshot(algos=(), available=True)
    text = _render(snap)
    assert "No crypto algorithms registered" in text
    assert "⚠" not in text


def test_render_canonical(tmp_path: Path) -> None:
    p = tmp_path / "crypto"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "shash" in text  # By-type aggregation surfaces.
    assert "cipher" in text
    assert "Algorithms registered" in text


def test_render_no_warning_when_all_passed(tmp_path: Path) -> None:
    """The cry-wolf pin: ⚠ MUST NOT appear when every algorithm
    passed self-test. Pinned because the entire predicate is
    self-test failure — accidentally warning on passing kernels
    would burn the operator on every host."""
    p = tmp_path / "crypto"
    p.write_text(_SAMPLE)
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" not in text


def test_render_warning_on_failed_selftest(tmp_path: Path) -> None:
    """The single ⚠ predicate: any failed self-test fires."""
    p = tmp_path / "crypto"
    p.write_text(
        _SAMPLE + "\nname : broken\ndriver : broken-hw\nmodule : kernel\n"
        "type : cipher\nselftest : failed\n"
    )
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "broken" in text
    assert "Failed self-test" in text


def test_render_warning_on_selftest_unknown(tmp_path: Path) -> None:
    """'unknown' self-test must ALSO fire ⚠ — same operator
    response as failed. Pinned distinct from the failed case
    because the rendered selftest_raw must say 'unknown' so the
    operator can distinguish the diagnostic path."""
    p = tmp_path / "crypto"
    p.write_text(
        "name : maybe\ndriver : maybe-hw\nmodule : kernel\ntype : cipher\nselftest : unknown\n"
    )
    snap = _capture(path=p)
    text = _render(snap)
    assert "⚠" in text
    assert "selftest=<code>unknown</code>" in text


def test_crypto_algo_fields() -> None:
    a = _CryptoAlgo(
        name="x",
        driver="x-aesni",
        module="kernel",
        type="cipher",
        selftest_raw="passed",
    )
    assert a.selftest_passed is True
