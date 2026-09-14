"""A Telegram handle is ASCII, and three places used to disagree (#1484).

Each of the three sites below turns a configured username into a
``https://t.me/<handle>`` link and guards it with the same predicate:
``all(c.isalnum() or c == "_")``. ``str.isalnum`` is Unicode-aware —
``"поддержка".isalnum()`` is ``True`` — so every one of them accepted a
Cyrillic handle and minted a link that 404s the moment anybody taps it.

That is worse than no link. Two of these links are printed on the legal
and contact pages, which exist because an acquiring bank asks to see
them; a dead support contact there is the single thing on the page a
reviewer is most likely to try.

The tests are written against the *outputs* rather than the predicate,
so a future rewrite of the check is free to look different as long as a
non-ASCII handle still cannot reach a URL. The lookalike alphabet is
deliberately varied: Cyrillic (which reads as Latin), a full-width Latin
``ｍ`` (which normalises to ASCII under NFKC but is not ASCII), and a
Cyrillic ``о`` hidden inside an otherwise-Latin word — the homograph
form an operator is most likely to paste without noticing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_invite_bot.cms.guide_site.context import GuideSiteContext
from telegram_invite_bot.cms.legal.context import LegalContext
from telegram_invite_bot.config.settings import LegalConfig

#: Handles that must never produce a link. Every one of them passes
#: ``all(c.isalnum() or c == "_")``, which is the whole point.
NON_ASCII_HANDLES = [
    "поддержка",
    "ｍybot",
    "kоm17_support",  # the ``о`` is U+043E
    "ボット",
]

#: The control group. If these ever stop working the fix has overshot.
ASCII_HANDLES = ["kom17bot", "my_test_bot", "Bot_2024"]


@pytest.mark.parametrize("handle", NON_ASCII_HANDLES)
def test_a_non_ascii_handle_yields_no_guide_site_link(handle: str) -> None:
    ctx = GuideSiteContext(
        guide_file_ru=Path("ru.md"),
        guide_file_en=Path("en.md"),
        bot_username=handle,
    )
    assert ctx.tme_url == "https://t.me"


@pytest.mark.parametrize("handle", ASCII_HANDLES)
def test_an_ascii_handle_still_reaches_the_guide_site_link(handle: str) -> None:
    ctx = GuideSiteContext(
        guide_file_ru=Path("ru.md"),
        guide_file_en=Path("en.md"),
        bot_username=handle,
    )
    assert ctx.tme_url == f"https://t.me/{handle}"


@pytest.mark.parametrize("handle", NON_ASCII_HANDLES)
def test_a_non_ascii_handle_yields_no_legal_page_link(handle: str) -> None:
    assert LegalContext(bot_username=handle).tme_url == "https://t.me"


@pytest.mark.parametrize("handle", ASCII_HANDLES)
def test_an_ascii_handle_still_reaches_the_legal_page_link(handle: str) -> None:
    assert LegalContext(bot_username=handle).tme_url == f"https://t.me/{handle}"


@pytest.mark.parametrize("handle", NON_ASCII_HANDLES)
def test_a_non_ascii_support_handle_is_dropped_at_settings_load(handle: str) -> None:
    """The third site refuses earlier than the other two.

    ``LegalConfig`` normalises the value in a validator, so a bad handle
    becomes ``None`` and ``support_url`` — the property that builds the
    button — has nothing to build from. Asserting on ``support_url``
    rather than on the field keeps the test pointed at the consequence.
    """
    cfg = LegalConfig(SUPPORT_USERNAME=handle)
    assert cfg.support_username is None
    assert cfg.support_url is None


@pytest.mark.parametrize("handle", ASCII_HANDLES)
def test_an_ascii_support_handle_survives_settings_load(handle: str) -> None:
    cfg = LegalConfig(SUPPORT_USERNAME=f"@{handle}")
    assert cfg.support_username == handle
    assert cfg.support_url == f"https://t.me/{handle}"


def test_the_lookalikes_are_the_ones_the_old_predicate_accepted() -> None:
    """Without this, the parametrisation above could rot into a set of
    handles the *old* code already refused, and the tests would pass on
    a reverted fix.
    """
    for handle in NON_ASCII_HANDLES:
        assert all(c.isalnum() or c == "_" for c in handle), handle
        assert not handle.isascii(), handle
