"""Unit tests for the /groupadmin control card (L-42, cluster GA).

Covered:

* ``_actor_allowed`` — the legacy ``has_group_admin_rights`` gate
  (bot.py:7555-7565): developer → live TG-admin, with the R-FIX-007
  fail-closed posture (admin-probe error → ``None`` "retry later",
  never a grant). #670: a global rank does NOT open this panel — the
  tests below pin that, because the revision they replace pinned the
  opposite.
* ``_render_card`` — every section key present, per-section degradation
  to ``h_ga_unavailable``, ✅/❌ marks and counters substituted.
* ``GroupAdminRefresh`` callback data — wire prefix isolation and
  round-trip.
* ``build_card_markup`` — 6 section buttons + the 3-page nav + refresh,
  all carrying ``gadm``-prefixed payloads.
* RR-4 #35/#36/#37 — ``resolve_page`` routing, the Settings keyboard's
  write vocabulary (a forged token must reach no column at all), and
  the Settings/Stats/Words renderers including their per-read
  degradation and their HTML escaping of admin-supplied text.
* RR-4 #38 — the staff page: the four demote guards, the roster
  renderer, the grant parser/resolver, and the TG-admin ceiling that
  makes an unranked chat owner able to use the panel at all.
* RR-4 #43 — the Words page write surface: the shared ``validate_word``
  both /filter_add and the panel go through, the ➖ button's visibility
  rule, and the per-word delete grid (id payloads, clipped labels).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import pytest

from telegram_invite_bot.core.moderation_reasons import CAPTCHA_FAILED
from telegram_invite_bot.core.ranks import RankLevel
from telegram_invite_bot.handlers.groupadmin import (
    PANEL_GRANT_MAX,
    PANEL_GRANT_MIN,
    GroupAdminSnapshot,
    ModStats,
    StaffMember,
    StaffRoster,
    _actor_allowed,
    _droppable,
    _member_name,
    _render_card,
    _render_settings,
    _render_staff,
    _render_stats,
    _render_words,
    _resolve_staff_target,
    _settings_markup,
    parse_staff_grant,
)
from telegram_invite_bot.handlers.wordfilter import MAX_WORD_LENGTH, validate_word
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.groupadmin import (
    MUTE_CHOICES_MINUTES,
    PAGE_HOME,
    PAGE_SETTINGS,
    PAGE_STAFF,
    PAGE_STAFF_DROP,
    PAGE_STATS,
    PAGE_WORDS,
    PAGE_WORDS_DROP,
    PICKER_FIELDS,
    SECTION_ALL,
    TOGGLE_FIELDS,
    WARN_CHOICES,
    WORD_BUTTON_MAX_LEN,
    GroupAdminPick,
    GroupAdminRefresh,
    GroupAdminSet,
    GroupAdminStaffAdd,
    GroupAdminStaffDrop,
    GroupAdminWordAdd,
    GroupAdminWordDrop,
    build_card_markup,
    build_picker_markup,
    build_settings_markup,
    build_staff_drop_markup,
    build_staff_markup,
    build_words_drop_markup,
    build_words_markup,
    format_mute_choice,
    is_known_section,
    picker_choices,
    resolve_page,
    word_button_label,
)
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigView
from telegram_invite_bot.repositories.moderation_repo import ActionRow

_CHAT = -100123
_USER = 555


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubSettings:
    """Settings stand-in exposing only ``bot.is_developer``."""

    def __init__(self, *, developer: bool) -> None:
        self.bot = self
        self._developer = developer

    def is_developer(self, user_id: int) -> bool:  # noqa: ARG002 — signature parity
        return self._developer


class _StubMember:
    def __init__(self, status: str, *, rights: bool = True) -> None:
        self.status = status
        # #337: an administrator only counts when Telegram granted a
        # moderation right. ``rights=False`` models the title-only
        # administrator, who must NOT pass the gate.
        self.can_restrict_members = rights


class _StubBot:
    """``get_chat_member`` stub: a status string, or an exception."""

    def __init__(self, status: str | None, *, rights: bool = True) -> None:
        self._status = status
        self._rights = rights

    async def get_chat_member(self, chat_id: int, user_id: int) -> _StubMember:
        if self._status is None:
            msg = "API down"
            raise RuntimeError(msg)
        return _StubMember(self._status, rights=self._rights)


class _StubRanks:
    def __init__(self, rank: int) -> None:
        self._rank = rank

    async def get_rank(self, user_id: int) -> int:  # noqa: ARG002 — signature parity
        return self._rank


async def _gate(
    *,
    developer: bool = False,
    status: str | None = "member",
    rank: int = RankLevel.USER,
    rights: bool = True,
) -> bool | None:
    # ``rank`` is still threaded in so the tests can prove it is IGNORED.
    _ = _StubRanks(rank)
    return await _actor_allowed(
        cast("Any", _StubBot(status, rights=rights)),
        cast("Any", _StubSettings(developer=developer)),
        chat_id=_CHAT,
        user_id=_USER,
    )


# ---------------------------------------------------------------------------
# _actor_allowed — gate = developer OR live TG-admin (#670)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_developer_allows_without_probes() -> None:
    # Developer wins even when the API is down and the rank is 0.
    assert await _gate(developer=True, status=None, rank=RankLevel.USER) is True


@pytest.mark.asyncio
async def test_gate_live_admin_allows() -> None:
    assert await _gate(status="administrator") is True
    assert await _gate(status="creator") is True


@pytest.mark.asyncio
async def test_gate_titular_admin_refused() -> None:
    """#337: administrator with no moderation right is not an admin here.

    Legacy resolved this gate through ``telegram_admin_has_mod_rights``
    (bot.py:7455-7476), so a member promoted purely for the title fell
    through to the rank branch — where rank 0 grants nothing.
    """
    assert await _gate(status="administrator", rights=False) is False
    # #670: and no rank rescues them here. Legacy's fall-through to the
    # rank branch belonged to ``require_group_moderation``, which gates
    # the moderation COMMANDS — not to ``has_group_admin_rights``, which
    # is what this panel used.
    assert await _gate(status="administrator", rights=False, rank=RankLevel.ADMIN) is False
    # A creator needs no explicit right — legacy short-circuits on status.
    assert await _gate(status="creator", rights=False) is True


@pytest.mark.asyncio
async def test_gate_rank_never_opens_the_panel() -> None:
    """#670: a global rank is not standing in THIS chat, and this writes.

    The revision this replaces asserted the opposite, on a docstring
    that claimed legacy's ``has_group_admin_rights`` admitted "ranked
    staff" and that DESIGN_RANKS.md §2.2 blessed a ``>= ADMIN``
    threshold. Neither holds: bot.py:7555-7565 has no rank path at all,
    and DESIGN_RANKS.md:128 defers ``/groupadmin`` to "wave 2". Ranks
    are global and the branch never looked at the chat, so every rank
    4/5 holder could rewrite moderation config, the word list and the
    staff roster of every group the bot sits in.
    """
    assert await _gate(status="member", rank=RankLevel.ADMIN) is False
    assert await _gate(status="member", rank=RankLevel.OWNER) is False


@pytest.mark.asyncio
async def test_gate_rank_below_admin_refuses() -> None:
    assert await _gate(status="member", rank=RankLevel.SENIOR_MOD) is False
    assert await _gate(status="member", rank=RankLevel.USER) is False


@pytest.mark.asyncio
async def test_gate_api_error_fails_closed_to_retry() -> None:
    # R-FIX-007 posture: probe failure → None (retry later), never a grant.
    assert await _gate(status=None, rank=RankLevel.USER) is None


@pytest.mark.asyncio
async def test_gate_api_error_is_not_rescued_by_rank() -> None:
    # #670: a Telegram hiccup has nowhere to degrade to any more — the
    # panel says "retry later" rather than admitting on a global rank.
    assert await _gate(status=None, rank=RankLevel.ADMIN) is None


# ---------------------------------------------------------------------------
# _render_card
# ---------------------------------------------------------------------------


def _cfg() -> GroupModConfigView:
    return GroupModConfigView(
        group_id=_CHAT,
        automod_enabled=True,
        profanity_enabled=False,
        max_warns=3,
        mute_minutes=60,
        autoban_enabled=True,
        antiflood_enabled=False,
        flood_max_msgs=10,
        flood_window_sec=10,
        flood_mute_minutes=10,
    )


def _full_snapshot() -> GroupAdminSnapshot:
    return GroupAdminSnapshot(
        modcfg=_cfg(),
        filter_count=7,
        welcome=(True, False),
        alias_count=2,
        treasury=(1500, 42),
        rules_set=True,
    )


def test_render_card_contains_all_sections() -> None:
    text = _render_card(_full_snapshot(), "ru", title="Тестовая группа")
    # The h_ga_* yaml keys are merged, so assert the RENDERED section
    # markers (one stable substring per section) rather than raw keys.
    for marker in (
        "Панель управления группой",  # h_ga_header
        "Модерация:",  # h_ga_mod
        "Фильтр слов:",  # h_ga_filter
        "Приветствие:",  # h_ga_welcome
        "Алиасы команд:",  # h_ga_aliases
        "Казна группы:",  # h_ga_treasury
        "Правила:</b> заданы",  # h_ga_rules_set
        "Обновлено:",  # h_ga_footer
    ):
        assert marker in text, marker
    assert "временно недоступен" not in text  # h_ga_unavailable


def test_render_card_degrades_per_section() -> None:
    snapshot = GroupAdminSnapshot(
        modcfg=None,
        filter_count=None,
        welcome=None,
        alias_count=None,
        treasury=None,
        rules_set=None,
    )
    text = _render_card(snapshot, "en", title=None)
    assert text.count("temporarily unavailable") == 6
    assert "Moderation:" not in text
    assert "Rules:" not in text


def test_render_card_rules_unset_key() -> None:
    snapshot = GroupAdminSnapshot(
        modcfg=None,
        filter_count=0,
        welcome=(False, False),
        alias_count=0,
        treasury=(0, 0),
        rules_set=False,
    )
    text = _render_card(snapshot, "ru", title="g")
    assert "Правила:</b> не заданы" in text
    assert "Правила:</b> заданы" not in text


# ---------------------------------------------------------------------------
# Callback data + markup
# ---------------------------------------------------------------------------


def test_callback_data_round_trip_and_prefix() -> None:
    packed = GroupAdminRefresh(section=SECTION_ALL).pack()
    assert packed.startswith("gadm:")
    parsed = GroupAdminRefresh.unpack(packed)
    assert parsed.section == SECTION_ALL


def test_is_known_section() -> None:
    assert is_known_section("mod")
    assert is_known_section("all")
    assert not is_known_section("moderation_words")  # legacy literal stays foreign
    assert not is_known_section("")


def test_markup_layout_and_payloads() -> None:
    markup = build_card_markup("ru")
    rows = markup.inline_keyboard
    # Six section buttons in pairs, the four sub-pages two per row, the
    # full refresh alone at the bottom.
    assert [len(row) for row in rows] == [2, 2, 2, 2, 2, 1]
    payloads = [btn.callback_data for row in rows for btn in row]
    assert len(payloads) == 11
    for payload in payloads:
        assert payload is not None and payload.startswith("gadm:")
    sections = [GroupAdminRefresh.unpack(cast("str", p)).section for p in payloads]
    assert all(is_known_section(s) for s in sections[:6])
    assert sections[6:10] == [PAGE_SETTINGS, PAGE_STATS, PAGE_WORDS, PAGE_STAFF]
    # The lone bottom button is the full refresh.
    assert GroupAdminRefresh.unpack(cast("str", rows[-1][0].callback_data)).section == SECTION_ALL


# ---------------------------------------------------------------------------
# RR-4 #35: page routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (PAGE_SETTINGS, PAGE_SETTINGS),
        (PAGE_STATS, PAGE_STATS),
        (PAGE_WORDS, PAGE_WORDS),
        (PAGE_HOME, PAGE_HOME),
        # Every overview section button refreshes the overview…
        (SECTION_ALL, PAGE_HOME),
        ("mod", PAGE_HOME),
        # …and so does anything forged, including the legacy literals
        # still owned by the telebot process.
        ("moderation_settings", PAGE_HOME),
        ("", PAGE_HOME),
        ("../../etc/passwd", PAGE_HOME),
    ],
)
def test_resolve_page(token: str, expected: str) -> None:
    assert resolve_page(token) == expected


# ---------------------------------------------------------------------------
# RR-4 #36: settings page — readout, keyboard, write vocabulary
# ---------------------------------------------------------------------------


def _modcfg(**overrides: Any) -> GroupModConfigView:
    """A full config view with every field named, tweakable per test."""
    base: dict[str, Any] = {
        "group_id": _CHAT,
        "automod_enabled": True,
        "profanity_enabled": False,
        "max_warns": 3,
        "mute_minutes": 60,
        "autoban_enabled": False,
        "antiflood_enabled": True,
        "flood_max_msgs": 5,
        "flood_window_sec": 10,
        "flood_mute_minutes": 30,
        "captcha_enabled": False,
        "captcha_timeout_sec": 120,
        "coins_enabled": True,
    }
    base.update(overrides)
    return GroupModConfigView(**base)


def test_render_settings_marks_and_units() -> None:
    text = _render_settings(_modcfg(mute_minutes=10080), "ru", title="Клуб")
    assert "Клуб" in text
    assert "✅" in text
    assert "❌" in text
    # 10080 minutes reads as days, not as "168 ч".
    assert "7 д" in text
    assert "3" in text


def test_render_settings_escapes_title() -> None:
    text = _render_settings(_modcfg(), "ru", title="<b>evil</b>")
    assert "&lt;b&gt;evil&lt;/b&gt;" in text
    assert "<b>evil</b>" not in text


def test_render_settings_degrades_without_config() -> None:
    text = _render_settings(None, "ru", title="g")
    assert t("h_ga_unavailable", "ru") in text
    # No readout means no hint about tapping controls that aren't there.
    assert t("h_ga_set_hint", "ru") not in text


def test_settings_markup_offers_no_controls_without_config() -> None:
    rows = _settings_markup(None, "ru").inline_keyboard
    payloads = [btn.callback_data for row in rows for btn in row]
    # Refresh + back only — never a write button over a config we could
    # not read (tapping one would persist a value derived from nothing).
    assert all(cast("str", p).startswith("gadm:") for p in payloads)


def test_settings_markup_toggles_write_the_opposite_value() -> None:
    rows = build_settings_markup(
        lang="ru",
        automod=True,
        profanity=False,
        autoban=False,
        antiflood=True,
        captcha=False,
        coins=True,
        max_warns=3,
        mute_minutes=60,
    ).inline_keyboard
    assert [len(row) for row in rows] == [2, 2, 2, 1, 1, 1]
    writes = {
        GroupAdminSet.unpack(cast("str", btn.callback_data)).field: GroupAdminSet.unpack(
            cast("str", btn.callback_data)
        ).value
        for row in rows
        for btn in row
        if cast("str", btn.callback_data).startswith("gadms:")
    }
    assert writes == {
        "auto": 0,  # currently on → tap turns it off
        "prof": 1,
        "aban": 1,
        "flood": 0,
        "capt": 1,
        "coins": 0,
    }
    assert set(writes) == set(TOGGLE_FIELDS)


def test_settings_markup_pickers_open_not_write() -> None:
    rows = build_settings_markup(
        lang="ru",
        automod=False,
        profanity=False,
        autoban=False,
        antiflood=False,
        captcha=False,
        coins=False,
        max_warns=5,
        mute_minutes=1440,
    ).inline_keyboard
    picks = [
        GroupAdminPick.unpack(cast("str", btn.callback_data)).field
        for row in rows
        for btn in row
        if cast("str", btn.callback_data).startswith("gadmp:")
    ]
    assert picks == ["warns", "mute"]
    assert set(picks) == set(PICKER_FIELDS)


def test_picker_markup_only_offers_allowed_values() -> None:
    for token, choices in (("warns", WARN_CHOICES), ("mute", MUTE_CHOICES_MINUTES)):
        rows = build_picker_markup(token, "ru").inline_keyboard
        values = [
            GroupAdminSet.unpack(cast("str", btn.callback_data)).value
            for row in rows
            for btn in row
            if cast("str", btn.callback_data).startswith("gadms:")
        ]
        assert values == list(choices)
        # The back button lands on its own row.
        assert len(rows[-1]) == 1


def test_picker_markup_unknown_token_offers_nothing() -> None:
    rows = build_picker_markup("max_warns", "ru").inline_keyboard
    # A forged token names a real column — and still yields no writes.
    assert not [
        btn for row in rows for btn in row if cast("str", btn.callback_data).startswith("gadms:")
    ]


def test_writable_vocabulary_stays_closed() -> None:
    """No token may name a column outside the two published maps."""
    assert set(TOGGLE_FIELDS) & set(PICKER_FIELDS) == set()
    assert set(TOGGLE_FIELDS.values()) <= set(GroupModConfigView.__dataclass_fields__)
    assert set(PICKER_FIELDS.values()) <= set(GroupModConfigView.__dataclass_fields__)
    # Nothing writable is a structural/identity column.
    assert "group_id" not in set(TOGGLE_FIELDS.values()) | set(PICKER_FIELDS.values())


def test_picker_choices_rejects_dangerous_values() -> None:
    # 0 warnings would ban on the first message; a century-long mute is
    # a ban by another name. Neither is reachable through a picker.
    assert 0 not in picker_choices("warns")
    assert 0 not in picker_choices("mute")
    assert max(picker_choices("mute")) == 10080  # one week, legacy's ceiling
    assert picker_choices("automod_enabled") == ()
    assert picker_choices("") == ()


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(60, "1 ч"), (180, "3 ч"), (720, "12 ч"), (1440, "1 д"), (10080, "7 д")],
)
def test_format_mute_choice(minutes: int, expected: str) -> None:
    assert format_mute_choice(minutes, "ru") == expected


# ---------------------------------------------------------------------------
# RR-4 #37: stats + words pages
# ---------------------------------------------------------------------------


def _row(action: str, reason: str = "") -> ActionRow:
    return ActionRow(action=action, reason=reason, date=datetime(2026, 8, 4, 12, 30))


def test_render_stats_counters_and_log() -> None:
    stats = ModStats(
        active_warns=2,
        action_counts={"warn": 7, "ban": 1, "pin": 4},
        filter_count=3,
        recent=[_row("warn", "спам"), _row("ban")],
    )
    text = _render_stats(stats, "ru", title="Клуб")
    assert "Клуб" in text
    assert "2" in text and "7" in text
    # An action with no counter row still gets a readable log label.
    assert t("h_ga_action_ban", "ru") in text
    assert "04.08 12:30" in text
    assert "спам" in text
    assert t("h_ga_stats_log_empty", "ru") not in text


def test_render_stats_escapes_and_clips_reason() -> None:
    stats = ModStats(
        active_warns=0,
        action_counts={},
        filter_count=0,
        recent=[_row("warn", "<i>x</i>" + "y" * 80)],
    )
    text = _render_stats(stats, "ru", title="g")
    assert "&lt;i&gt;x&lt;/i&gt;" in text
    assert "<i>x</i>" not in text
    assert "y" * 80 not in text  # clipped to _REASON_MAX


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_render_stats_localizes_a_bot_written_reason(lang: str) -> None:
    """#1346: the one row the bot writes itself is read in the operator's language."""
    stats = ModStats(
        active_warns=0,
        action_counts={},
        filter_count=0,
        recent=[_row("kick", CAPTCHA_FAILED)],
    )
    text = _render_stats(stats, lang, title="g")
    assert t("h_mod_reason_captcha_failed", lang) in text
    assert CAPTCHA_FAILED not in text


def test_render_stats_leaves_a_localized_reason_undressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1641: a recognised slug reaches the row whole and unescaped.

    Both real labels are plain and short, so the double-encode this
    line used to do was invisible; the locale value is dressed here
    to make it visible. Escaping would print ``&amp;`` where the
    label says ``&``, and the 40-character clip — a budget written
    for a moderator who can type a paragraph — would cut a tag in
    half. Both fail silently: a broken tag renders as nothing.
    """
    dressed = "<b>Капча</b> & " + "длинная причина " * 5

    def fake(key: str, lg: str | None = None, /, **kwargs: object) -> str:
        return dressed

    monkeypatch.setattr("telegram_invite_bot.core.moderation_reasons.t", fake)
    stats = ModStats(
        active_warns=0,
        action_counts={},
        filter_count=0,
        recent=[_row("kick", CAPTCHA_FAILED)],
    )
    text = _render_stats(stats, "ru", title="g")
    assert dressed in text
    assert "&lt;b&gt;" not in text
    assert "&amp;" not in text


def test_render_stats_unknown_action_is_escaped_not_dropped() -> None:
    stats = ModStats(
        active_warns=0,
        action_counts={},
        filter_count=0,
        recent=[_row("<b>hack</b>")],
    )
    text = _render_stats(stats, "ru", title="g")
    assert "&lt;b&gt;hack&lt;/b&gt;" in text


def test_render_stats_degrades_per_read() -> None:
    text = _render_stats(
        ModStats(active_warns=None, action_counts=None, filter_count=None, recent=None),
        "ru",
        title="g",
    )
    assert text.count(t("h_ga_unavailable", "ru")) == 3
    # A failed word count is simply omitted — it has its own page.
    assert t("h_ga_stats_words", "ru", count=0) not in text


def test_render_stats_empty_log() -> None:
    text = _render_stats(
        ModStats(active_warns=0, action_counts={}, filter_count=0, recent=[]),
        "ru",
        title="g",
    )
    assert t("h_ga_stats_log_empty", "ru") in text


def test_render_words_preview_escapes_and_overflows() -> None:
    words = ["<script>", *(f"w{i}" for i in range(25))]
    text = _render_words(words, "ru", title="g")
    assert "&lt;script&gt;" in text
    assert "<script>" not in text
    assert "<code>" in text
    # 20 previewed: the script tag plus w0..w18.
    assert "<code>w18</code>" in text
    assert "<code>w19</code>" not in text
    assert t("h_ga_words_more", "ru", count=6) in text


def test_render_words_empty_and_unavailable() -> None:
    assert t("h_ga_words_empty", "ru") in _render_words([], "ru", title="g")
    assert t("h_ga_unavailable", "ru") in _render_words(None, "ru", title="g")


# ---------------------------------------------------------------------------
# RR-4 #43: the Words page write surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  СпаМ  ", None),
        ("", "h_wf_usage_add"),
        ("   ", "h_wf_usage_add"),
        # A word long enough to be a message, not a filter.
        ("x" * (MAX_WORD_LENGTH + 1), "h_wf_too_long"),
        # Banning "/ban" would let the filter eat the moderation surface.
        ("/ban", "h_wf_command_rejected"),
    ],
)
def test_validate_word_is_one_rule_for_both_entry_points(raw: str, expected: str | None) -> None:
    # The panel passes its own empty-copy key; every other refusal is
    # shared, which is the point of the helper — the buttons must not be
    # a looser path into the same table than /filter_add.
    _, refusal = validate_word(raw, empty_key="h_wf_usage_add")
    assert refusal == expected


def test_validate_word_normalizes_before_judging() -> None:
    word, refusal = validate_word("  СпаМ  ", empty_key="h_wf_usage_add")
    assert (word, refusal) == ("спам", None)
    # Length is measured after stripping, so padding cannot fake an
    # over-long word.
    padded = " " * 40 + "x" * MAX_WORD_LENGTH + " " * 40
    assert validate_word(padded, empty_key="h_wf_usage_add")[1] is None


def test_validate_word_uses_the_callers_empty_copy() -> None:
    assert validate_word("", empty_key="h_ga_words_add_empty")[1] == "h_ga_words_add_empty"


def test_words_markup_hides_the_drop_button_on_an_empty_list() -> None:
    markup = build_words_markup("ru", has_words=False)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    # ➕ is always there — an empty list is exactly when you want it.
    assert t("h_ga_words_btn_add", "ru") in labels
    # ➖ is not: legacy showed it and answered the tap with "nothing to
    # delete" (bot.py:32375), a round trip to learn what the page said.
    assert t("h_ga_words_btn_drop", "ru") not in labels


def test_words_markup_offers_the_drop_button_when_there_is_something_to_drop() -> None:
    markup = build_words_markup("ru", has_words=True)
    payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert GroupAdminWordAdd().pack() in payloads
    # ➖ opens the grid; it does not delete anything by itself.
    assert GroupAdminRefresh(section=PAGE_WORDS_DROP).pack() in payloads
    assert GroupAdminRefresh(section=PAGE_HOME).pack() in payloads


def test_words_drop_markup_carries_ids_not_words() -> None:
    markup = build_words_drop_markup([(7, "спам"), (9, "реклама")], "ru")
    rows = markup.inline_keyboard
    # Two per row, then a full-width way back.
    assert [len(r) for r in rows] == [2, 1]
    assert rows[0][0].callback_data == GroupAdminWordDrop(word_id=7).pack()
    assert rows[0][1].callback_data == GroupAdminWordDrop(word_id=9).pack()
    # The word itself never rides on the wire — Telegram caps
    # callback_data at 64 bytes and a word may be 100 characters.
    assert "спам" not in (rows[0][0].callback_data or "")


def test_words_drop_markup_payloads_stay_inside_telegrams_64_bytes() -> None:
    packed = GroupAdminWordDrop(word_id=2**31 - 1).pack()
    assert len(packed.encode()) <= 64


def test_words_drop_markup_empty_still_offers_the_way_back() -> None:
    # The handler routes an empty list to the Words page instead, but the
    # builder must not strand anybody who gets here another way.
    rows = build_words_drop_markup([], "ru").inline_keyboard
    assert len(rows) == 1
    assert len(rows[0]) == 1
    assert rows[0][0].text == t("h_ga_btn_back", "ru")


def test_word_button_label_clips_long_words() -> None:
    assert word_button_label("спам") == "спам"
    exact = "x" * WORD_BUTTON_MAX_LEN
    assert word_button_label(exact) == exact
    clipped = word_button_label("y" * (WORD_BUTTON_MAX_LEN + 5))
    assert len(clipped) == WORD_BUTTON_MAX_LEN
    assert clipped.endswith("…")


def test_word_callback_prefixes_stay_isolated() -> None:
    # Each of these is registered with its own .filter(); a shared prefix
    # would route an add tap into the delete handler.
    prefixes = {
        GroupAdminWordAdd().pack().split(":")[0],
        GroupAdminWordDrop(word_id=1).pack().split(":")[0],
        GroupAdminStaffAdd().pack().split(":")[0],
        GroupAdminStaffDrop(user_id=1).pack().split(":")[0],
        GroupAdminRefresh(section=PAGE_WORDS).pack().split(":")[0],
    }
    assert len(prefixes) == 5


def test_words_pages_are_known_tokens() -> None:
    assert resolve_page(PAGE_WORDS_DROP) == PAGE_WORDS_DROP
    # A forged section falls back to the overview rather than 500-ing.
    assert resolve_page("wdel_") == PAGE_HOME


# ---------------------------------------------------------------------------
# RR-4 #38: staff page — roster render, demote guards, grant parsing
# ---------------------------------------------------------------------------


def _member(user_id: int, *, rank: int = 1, name: str = "N", tg_admin: bool = False) -> StaffMember:
    return StaffMember(user_id=user_id, name=name, rank=rank, tg_admin=tg_admin)


def _roster(*members: StaffMember, truncated: bool = False) -> StaffRoster:
    return StaffRoster(members=members, truncated=truncated)


def test_member_name_falls_back_to_id() -> None:
    # A rank can be granted by id to somebody the bot has never seen —
    # an empty first_name must not render a bullet with nothing after it.
    assert _member_name(None, 77) == "ID 77"
    assert _member_name("   ", 77) == "ID 77"
    assert _member_name(" Аня ", 77) == "Аня"


def test_render_staff_rows_mark_why_each_person_has_power() -> None:
    text = _render_staff(
        _roster(
            _member(1, rank=4, name="Owner", tg_admin=True),
            _member(2, rank=2, name="Mod"),
        ),
        "ru",
        title="Клуб",
        can_manage=True,
    )
    assert t("h_ga_staff_count", "ru", count=2) in text
    # 👑 vs • is the distinction legacy's roster never drew: a chat
    # admin moderates through the TG bypass even at rank 0.
    assert t("h_ga_staff_mark_admin", "ru") in text
    assert t("h_ga_staff_mark_rank", "ru") in text
    assert "<code>1</code>" in text
    assert "<code>2</code>" in text
    assert t("h_ga_staff_hint", "ru") in text
    assert t("h_ga_staff_readonly", "ru") not in text


def test_render_staff_escapes_display_names() -> None:
    # Telegram display names are user-controlled and this message is
    # sent with parse_mode=HTML.
    text = _render_staff(
        _roster(_member(1, name="<b>boss</b>")), "ru", title="<i>g</i>", can_manage=True
    )
    assert "&lt;b&gt;boss&lt;/b&gt;" in text
    assert "<b>boss</b>" not in text
    assert "<i>g</i>" not in text


def test_render_staff_empty_unavailable_truncated_readonly() -> None:
    assert t("h_ga_staff_empty", "ru") in _render_staff(_roster(), "ru", title="g", can_manage=True)
    assert t("h_ga_unavailable", "ru") in _render_staff(
        StaffRoster(members=None, truncated=False), "ru", title="g", can_manage=True
    )
    truncated = _render_staff(_roster(_member(1), truncated=True), "ru", title="g", can_manage=True)
    # The cap is disclosed, never silent — and the disclosed number is the
    # count actually printed (#120), not the probe limit: this roster holds
    # one member, so "показаны первые 1" is the honest line even though the
    # truncation itself happened upstream in the probe.
    assert t("h_ga_staff_truncated", "ru", count=1) in truncated
    readonly = _render_staff(_roster(_member(1)), "ru", title="g", can_manage=False)
    assert t("h_ga_staff_readonly", "ru") in readonly


def test_droppable_applies_all_four_guards() -> None:
    settings = cast("Any", _StubSettings(developer=False))
    roster = _roster(
        _member(10, rank=0, name="AdminNoRank", tg_admin=True),  # nothing to strip
        _member(_USER, rank=2, name="Self"),  # never yourself
        _member(11, rank=3, name="Peer"),  # at the actor's rank
        _member(12, rank=4, name="Above"),  # above the actor
        _member(13, rank=1, name="Below"),  # the only legal target
    )
    people = _droppable(roster, actor_id=_USER, actor_rank=3, settings=settings)
    assert [m.user_id for m in people] == [13]


def test_droppable_never_offers_a_developer() -> None:
    # ``_StubSettings(developer=True)`` answers True for everyone, which
    # is exactly the "target is a developer" shape.
    roster = _roster(_member(13, rank=1))
    assert (
        _droppable(
            roster,
            actor_id=_USER,
            actor_rank=int(RankLevel.DEVELOPER),
            settings=cast("Any", _StubSettings(developer=True)),
        )
        == []
    )


def test_droppable_on_unreadable_roster_offers_nothing() -> None:
    assert (
        _droppable(
            StaffRoster(members=None, truncated=False),
            actor_id=_USER,
            actor_rank=int(RankLevel.OWNER),
            settings=cast("Any", _StubSettings(developer=False)),
        )
        == []
    )


class _StubVerdict:
    def __init__(self, allowed: bool, reason: str, actor_rank: int) -> None:
        self.allowed = allowed
        self.reason = reason
        self.actor_rank = actor_rank


class _StubRankService:
    """Stands in for ``RankService`` so the verdict can be scripted."""

    verdict = _StubVerdict(True, "ok", int(RankLevel.ADMIN))

    def __init__(self, registry: Any, settings: Any) -> None:  # noqa: ARG002
        pass

    async def may_manage_ranks(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        return type(self).verdict


async def _authority(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: _StubVerdict,
    developer: bool = False,
) -> tuple[bool, int]:
    import telegram_invite_bot.handlers.groupadmin as mod

    monkeypatch.setattr(_StubRankService, "verdict", verdict)
    monkeypatch.setattr(mod, "RankService", _StubRankService)
    return await mod._staff_authority(
        cast("Any", None),
        cast("Any", None),
        cast("Any", _StubSettings(developer=developer)),
        actor_id=_USER,
        group_id=_CHAT,
    )


@pytest.mark.asyncio
async def test_staff_authority_developer_sits_at_the_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed, rank = await _authority(
        monkeypatch,
        verdict=_StubVerdict(True, "developer", int(RankLevel.USER)),
        developer=True,
    )
    assert (allowed, rank) == (True, int(RankLevel.DEVELOPER))


@pytest.mark.asyncio
async def test_staff_authority_does_not_lift_an_unranked_tg_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A chat owner who was never ranked in the bot is refused: the ranks
    # this panel writes are global, and adminship is evidence about one
    # chat only. The refusal is passed through untouched — no ceiling
    # lifts them to ADMIN any more.
    allowed, rank = await _authority(
        monkeypatch, verdict=_StubVerdict(False, "tg_admin_only", int(RankLevel.USER))
    )
    assert (allowed, rank) == (False, int(RankLevel.USER))


@pytest.mark.asyncio
async def test_staff_authority_uses_the_actors_own_bot_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The effective rank the target guards compare against is the
    # actor's real global rank, nothing synthesised.
    _, rank = await _authority(
        monkeypatch, verdict=_StubVerdict(True, "rank", int(RankLevel.OWNER))
    )
    assert rank == int(RankLevel.OWNER)


@pytest.mark.asyncio
async def test_staff_authority_passes_a_refusal_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed, rank = await _authority(
        monkeypatch, verdict=_StubVerdict(False, "rank_too_low", int(RankLevel.MODERATOR))
    )
    assert (allowed, rank) == (False, int(RankLevel.MODERATOR))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123 2", ("123", 2)),
        ("  @Аня   4  ", ("@Аня", 4)),
        ("123 -1", ("123", -1)),  # bounds are the caller's job
        (None, None),
        ("", None),
        ("123", None),
        ("123 2 3", None),
        ("123 два", None),
        ("123 2.5", None),
    ],
)
def test_parse_staff_grant(raw: str | None, expected: tuple[str, int] | None) -> None:
    assert parse_staff_grant(raw) == expected


def test_panel_grant_range_cannot_mint_an_owner() -> None:
    # Legacy offered 1..4 and so do we: the panel structurally cannot
    # hand out OWNER (5) or DEVELOPER (6), whoever is tapping.
    assert int(RankLevel.JUNIOR_MOD) == PANEL_GRANT_MIN
    assert int(RankLevel.ADMIN) == PANEL_GRANT_MAX
    assert int(RankLevel.OWNER) > PANEL_GRANT_MAX


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["-100123", "0", "abc", "@", "@  ", "12 3", ""])
async def test_resolve_staff_target_rejects_non_users(raw: str) -> None:
    # A negative value is a CHAT id; writing it into ``users.rank``
    # would create a rank-holding row that is not a person.
    assert await _resolve_staff_target(cast("Any", None), raw) is None


@pytest.mark.asyncio
async def test_resolve_staff_target_accepts_a_numeric_id() -> None:
    assert await _resolve_staff_target(cast("Any", None), "123456") == 123456


def test_staff_markup_hides_write_buttons_without_permission() -> None:
    managed = build_staff_markup("ru", can_manage=True)
    assert [len(row) for row in managed.inline_keyboard] == [2, 2]
    labels = [b.text for row in managed.inline_keyboard for b in row]
    assert t("h_ga_staff_btn_add", "ru") in labels
    assert t("h_ga_staff_btn_drop", "ru") in labels

    plain = build_staff_markup("ru", can_manage=False)
    assert [len(row) for row in plain.inline_keyboard] == [2]
    plain_labels = [b.text for row in plain.inline_keyboard for b in row]
    assert t("h_ga_staff_btn_add", "ru") not in plain_labels
    assert t("h_ga_staff_btn_drop", "ru") not in plain_labels
    # Refresh still points at the staff page, not the overview.
    back = plain.inline_keyboard[0]
    assert GroupAdminRefresh.unpack(cast("str", back[0].callback_data)).section == PAGE_STAFF
    assert GroupAdminRefresh.unpack(cast("str", back[1].callback_data)).section == PAGE_HOME


def test_staff_drop_markup_one_person_per_row_and_round_trips() -> None:
    markup = build_staff_drop_markup([(7, "➖ A"), (8, "➖ B")], "ru")
    rows = markup.inline_keyboard
    # One per row: a mis-tap here strips somebody's rank.
    assert [len(row) for row in rows] == [1, 1, 1]
    assert GroupAdminStaffDrop.unpack(cast("str", rows[0][0].callback_data)).user_id == 7
    assert GroupAdminStaffDrop.unpack(cast("str", rows[1][0].callback_data)).user_id == 8
    assert GroupAdminRefresh.unpack(cast("str", rows[2][0].callback_data)).section == PAGE_STAFF


def test_staff_drop_markup_empty_still_offers_the_way_back() -> None:
    markup = build_staff_drop_markup([], "ru")
    assert [len(row) for row in markup.inline_keyboard] == [1]


def test_staff_callback_prefixes_stay_isolated() -> None:
    # ``gadm`` / ``gadmsa`` / ``gadmsd`` share a stem; aiogram matches on
    # the first ':'-delimited segment, so they must not cross-unpack.
    add = GroupAdminStaffAdd().pack()
    drop = GroupAdminStaffDrop(user_id=9).pack()
    # The add button carries no fields, so its payload is the bare
    # prefix — nothing a tapper can rewrite.
    assert add == "gadmsa"
    assert drop.startswith("gadmsd:")
    with pytest.raises(ValueError, match="prefix"):
        GroupAdminRefresh.unpack(drop)
    # ``add`` has no fields at all, so aiogram rejects it on arity
    # before it even gets to the prefix — either way it never becomes a
    # demote payload.
    with pytest.raises((ValueError, TypeError)):
        GroupAdminStaffDrop.unpack(add)
    assert GroupAdminStaffDrop.unpack(drop).user_id == 9


def test_staff_pages_are_known_tokens() -> None:
    assert resolve_page(PAGE_STAFF) == PAGE_STAFF
    assert resolve_page(PAGE_STAFF_DROP) == PAGE_STAFF_DROP
