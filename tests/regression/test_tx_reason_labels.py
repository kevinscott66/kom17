"""Regression guard: the finances panel never prints a raw ledger ``reason``.

``transactions.reason`` is machine data — the writer picks a slug, the
reader picks the words. Three writers broke that rule and stored Russian
sentences instead, and ``handlers/profile.py`` printed the column
verbatim, so a group owner reading an English card was told «С покупки в
группе -100123 (за вычетом комиссии)» (#1547). Two more stored bare
ASCII slugs (``marriage_extend``, ``marry:flowers``) that read as
nothing at all in either language.

The fix localises at the READER: ``_tx_label`` maps every reason this
codebase writes onto an i18n key, and the three Russian sentences are
matched whole so that the rows ALREADY in the production ledger render
correctly too — a writer-side re-spelling could only ever fix rows not
yet written, and ``TransactionsRepo.recent`` is a top-five rather than a
window, so an inactive user's card keeps its oldest rows indefinitely.

Because the mapping is keyed on the writer's exact string, a reworded
writer does not fail loudly — it falls back to printing the raw string,
which is the very bug this repaired. So the guard has two halves:

* every writer's ``reason=`` source fragment is pinned here, so a
  rewording fails CI at the writer;
* every pinned sample is rendered through :func:`_tx_label` in both
  languages, so a mapping that stops covering it fails CI at the reader.

The developer-commission sentence is deliberately NOT re-spelled at the
writer: it matches legacy byte-for-byte so admin audits grep identically
across both systems during the strangler window
(``ReferralCommissionService.apply_developer_commission``). #1582 extended
the same treatment to the four purchase writers and the group-treasury
payout, for the same reason and with no writer touched.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from telegram_invite_bot.core.couple_activities import (
    MARRIAGE_BY_KEY,
    RELATIONSHIP_BY_KEY,
)
from telegram_invite_bot.handlers.profile import _tx_label

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "telegram_invite_bot"

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")

# (module, exact ``reason=`` source fragment, how many times it occurs,
#  a concrete value that fragment produces at runtime).
#
# The count is part of the pin: a second call site spelled the same way
# is fine, a call site that quietly loses one is not.
_WRITERS: tuple[tuple[str, str, int, str], ...] = (
    (
        "handlers/marriage.py",
        'reason="marriage_extend"',
        1,
        "marriage_extend",
    ),
    (
        "handlers/marriage.py",
        'reason="marriage_extend_refund"',
        1,
        "marriage_extend_refund",
    ),
    (
        "handlers/couple_activities.py",
        'reason=f"marry:{act.key}"',
        2,
        "marry:flowers_m",
    ),
    (
        "handlers/couple_activities.py",
        'reason=f"rel:{act.key}"',
        2,
        "rel:walk_invite",
    ),
    (
        "services/referral_commission_service.py",
        'reason=f"referral_purchase_commission:{buyer_id}"',
        1,
        "referral_purchase_commission:123456789",
    ),
    (
        "services/referral_commission_service.py",
        'reason=f"Комиссия с покупки монет (покупатель {buyer_id})"',
        1,
        "Комиссия с покупки монет (покупатель 123456789)",
    ),
    (
        "services/group_donation_service.py",
        'reason=f"С покупки в группе {group_id} (за вычетом комиссии)"',
        1,
        "С покупки в группе -1001234567890 (за вычетом комиссии)",
    ),
    (
        "services/group_donation_service.py",
        'reason=f"Комиссия с покупки в группе {group_id}"',
        1,
        "Комиссия с покупки в группе -1001234567890",
    ),
    # #1582 — the same class, four more writers. These rows are
    # self-addressed (the buyer reads his own purchase), so nobody is
    # handed a stranger's language; an English-speaking buyer is still
    # handed Russian on his own card. The sample item name is Latin on
    # purpose: it travels through the English render, which
    # ``test_english_render_carries_no_russian`` scans for Cyrillic.
    (
        "services/purchase_service.py",
        'reason=f"Покупка: {item.name}"',
        1,
        "Покупка: VIP",
    ),
    (
        "services/payments/yookassa.py",
        'reason="Покупка (ЮKassa)"',
        1,
        "Покупка (ЮKassa)",
    ),
    (
        "services/payments/crypto.py",
        'reason="Покупка криптой (Crypto Pay)"',
        1,
        "Покупка криптой (Crypto Pay)",
    ),
    (
        "services/payments/stripe.py",
        'reason="Покупка (Stripe)"',
        1,
        "Покупка (Stripe)",
    ),
    (
        "services/treasury_service.py",
        'reason=f"group_treasury_payout:{group_id}"',
        1,
        "group_treasury_payout:-1001234567890",
    ),
    # #2007 — /donate writes three rows of its own. The donor's row is
    # the one that matters most here: unlike the purchase lines above it
    # is written on the donor's own initiative in a GROUP, so a reader
    # of either language lands on it, and it is the only one of the
    # three that is a spend rather than a payout.
    (
        "services/group_donation_service.py",
        'reason=f"Донат в группу {group_id}"',
        1,
        "Донат в группу -1001234567890",
    ),
    (
        "services/group_donation_service.py",
        'reason=f"Донат в группу {group_id} (за вычетом комиссии)"',
        1,
        "Донат в группу -1001234567890 (за вычетом комиссии)",
    ),
    (
        "services/group_donation_service.py",
        'reason=f"Комиссия с доната в группу {group_id}"',
        1,
        "Комиссия с доната в группу -1001234567890",
    ),
)


@pytest.mark.parametrize(
    ("module", "fragment", "count", "sample"),
    _WRITERS,
    ids=[f"{m.split('/')[-1]}:{s}" for m, _f, _c, s in _WRITERS],
)
def test_writer_still_spells_the_reason_the_reader_maps(
    module: str, fragment: str, count: int, sample: str
) -> None:
    """The reason the writer stores is the one ``_tx_label`` knows."""
    source = (SRC_ROOT / module).read_text(encoding="utf-8")
    assert source.count(fragment) == count, (
        f"{module} no longer writes {fragment!r} {count}x. If the reason was "
        f"reworded, teach handlers/profile.py's _TX_* tables the new spelling "
        f"in the SAME commit — otherwise the finances panel silently goes back "
        f"to printing it raw (#1547)."
    )


@pytest.mark.parametrize(
    ("module", "fragment", "count", "sample"),
    _WRITERS,
    ids=[f"{m.split('/')[-1]}:{s}" for m, _f, _c, s in _WRITERS],
)
def test_every_written_reason_renders_as_words(
    module: str, fragment: str, count: int, sample: str
) -> None:
    """Each sample resolves through the tables, not the fallback.

    Checked across both languages at once on purpose. The Russian
    values of the three prose keys are byte-identical to what the
    writers store — that is deliberate, so a Russian reader sees no
    change at all — and per-language the ``ru`` render of those is
    therefore indistinguishable from the fallback. The fallback
    returns the same string in EVERY language, so a difference in
    either one proves the mapping fired.
    """
    ru = _tx_label(sample, "unused_type", "ru")
    en = _tx_label(sample, "unused_type", "en")
    raw = html.escape(sample)
    assert (ru, en) != (raw, raw), f"{sample!r} fell through _tx_label to the raw-string fallback"
    for label in (ru, en):
        assert not label.startswith("h_"), f"{sample!r} rendered a missing key"
        assert label.strip()


@pytest.mark.parametrize(
    ("module", "fragment", "count", "sample"),
    _WRITERS,
    ids=[f"{m.split('/')[-1]}:{s}" for m, _f, _c, s in _WRITERS],
)
def test_english_render_carries_no_russian(
    module: str, fragment: str, count: int, sample: str
) -> None:
    """The whole point of #1547: no Cyrillic on an English card."""
    assert not _CYRILLIC.search(_tx_label(sample, "unused_type", "en"))


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("key", sorted(MARRIAGE_BY_KEY))
def test_marriage_activity_reason_renders_its_name(key: str, lang: str) -> None:
    """``marry:<key>`` borrows the activity's own localised name."""
    label = _tx_label(f"marry:{key}", "couple_activity", lang)
    assert label == _activity_name(key, lang)


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("key", sorted(RELATIONSHIP_BY_KEY))
def test_relationship_activity_reason_renders_its_name(key: str, lang: str) -> None:
    """``rel:<key>`` does the same for the relationship half."""
    label = _tx_label(f"rel:{key}", "couple_activity", lang)
    assert label == _activity_name(key, lang)


def _activity_name(key: str, lang: str) -> str:
    from telegram_invite_bot.i18n import t

    name = t(f"h_couple_act_name_{key}", lang)
    assert name != f"h_couple_act_name_{key}", f"no name key for {key}"
    return name


def test_an_activity_key_that_does_not_exist_is_not_invented() -> None:
    """``t`` answers an unknown key with the key, which must never ship.

    The membership check in ``_tx_label`` is what stops that: a
    ``marry:`` reason naming an activity this build does not have falls
    through to the raw-string fallback instead of printing
    ``h_couple_act_name_ghost`` onto the card.
    """
    assert "ghost" not in MARRIAGE_BY_KEY
    assert _tx_label("marry:ghost", "couple_activity", "en") == "marry:ghost"


def test_an_unknown_reason_still_shows_something() -> None:
    """The fallback is the pre-#1547 behaviour, escaping included."""
    assert _tx_label("p2p order #7 escrow", "p2p", "en") == "p2p order #7 escrow"
    assert _tx_label("<b>x</b>", "p2p", "en") == "&lt;b&gt;x&lt;/b&gt;"


def test_a_missing_reason_falls_back_to_the_type_column() -> None:
    """Unchanged from before: ``reason`` is nullable, ``type`` is not."""
    assert _tx_label(None, "daily", "en") == "daily"
    assert _tx_label("", "<b>", "en") == "&lt;b&gt;"


def test_the_id_inside_a_prefixed_reason_is_escaped() -> None:
    """The argument comes out of a column and is spliced into HTML."""
    label = _tx_label("referral_purchase_commission:<b>", "referral", "en")
    assert "&lt;b&gt;" in label
    assert "<b>" not in label
