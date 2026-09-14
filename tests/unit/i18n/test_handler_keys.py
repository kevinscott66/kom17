"""Stage 20: contract test for the ``h_send_*`` / ``h_daily_*`` keys.

Stage 20 moved /send and /daily off hardcoded ``_USAGE_RU`` /
``_SUCCESS_RU`` constants and onto :func:`t` lookups. The e2e suite
exercises a handful of substrings through dispatcher → middleware →
handler, but a typo in a placeholder name or a YAML key removal would
only surface as a wrong-looking message at runtime — the e2e assertions
intentionally check a small piece of each card and would let a botched
formatter ship.

These unit tests pin the keys directly:

* every handler key is present in BOTH languages (no "EN forgot the
  fallback" surprises)
* the ``{streak}`` / ``{amount}`` / ``{wait}`` / ``{to_id}`` etc.
  placeholders are exactly the ones the handlers pass — drift in either
  direction (renamed kwarg, dropped placeholder) is a CI failure right
  here instead of a "{name}" literal in the user-facing card
* the cooldown templates round-trip with realistic kwargs, so the YAML
  block-scalar newlines are byte-stable (the legacy-parity test only
  covers legacy keys, leaving these otherwise unguarded)
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from telegram_invite_bot.i18n import _load, t
from telegram_invite_bot.services.inventory_use_planner import VIP_NAME_DURATIONS
from telegram_invite_bot.utils.render import TELEGRAM_CALLBACK_ANSWER_LIMIT, utf16_length

# Root of the real pipeline (``src/telegram_invite_bot``). The exhaustive
# scan below walks every module here looking for ``t("literal", ...)``.
_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "telegram_invite_bot"


@pytest.fixture(autouse=True)
def _clear_load_cache() -> None:
    """Loader is ``@lru_cache``-d; same reset as the translator tests."""
    _load.cache_clear()


# (key, expected_placeholders) — placeholders match the handler call
# sites verbatim. A future handler refactor that renames a kwarg MUST
# update both sides; this table is the linter.
_HANDLER_KEYS: list[tuple[str, frozenset[str]]] = [
    ("h_send_anonymous", frozenset()),
    ("h_send_insufficient", frozenset()),
    ("h_send_invalid_amount", frozenset()),
    ("h_send_no_recipient", frozenset()),
    ("h_send_no_sender", frozenset()),
    ("h_send_self", frozenset()),
    (
        "h_send_success",
        frozenset({"recipient", "amount", "received", "tax", "balance", "comment_line"}),
    ),
    # #16 receipt comment line + recipient DM courtesy notification.
    ("h_send_comment_line", frozenset({"comment"})),
    ("h_send_dm", frozenset({"sender", "amount"})),
    ("h_send_dm_comment", frozenset({"comment"})),
    ("h_send_usage", frozenset()),
    ("h_daily_anonymous", frozenset()),
    ("h_daily_cooldown", frozenset({"streak", "wait", "pmin", "pmax"})),
    ("h_daily_no_wallet", frozenset()),
    ("h_daily_success", frozenset({"breakdown", "amount", "streak", "balance"})),
    # #11 payout breakdown line fragments composed by the handler.
    ("h_daily_line_base", frozenset({"base", "min", "max"})),
    ("h_daily_line_streak", frozenset({"bonus"})),
    ("h_daily_line_vip", frozenset({"percent", "bonus"})),
    ("h_daily_line_double", frozenset({"bonus"})),
    ("h_daily_wait_lt_minute", frozenset()),
    ("h_daily_wait_short", frozenset({"hours", "minutes"})),
    # #25 VIP cosmetic emoji badge (handlers/emoji.py).
    ("h_emoji_list", frozenset({"badges", "current"})),
    ("h_emoji_vip_only", frozenset()),
    ("h_emoji_set_ok", frozenset({"emoji"})),
    ("h_emoji_cleared", frozenset()),
    ("h_emoji_not_in_set", frozenset({"badges"})),
    ("h_emoji_preview", frozenset({"preview"})),
    ("h_emoji_preview_none", frozenset({"badges"})),
    ("h_emoji_buy_info", frozenset({"badges"})),
    # RR-1 #5 per-user /stats card (handlers/stats.py). The card title and
    # the caption header take the *same* display name; the breakdown's
    # best-day line is the only two-placeholder template here.
    ("h_stats_user_header", frozenset({"name"})),
    ("h_stats_card_title", frozenset({"name"})),
    ("h_stats_card_subtitle", frozenset()),
    ("h_stats_breakdown_title", frozenset()),
    ("h_stats_best_day", frozenset({"date", "count"})),
    ("h_stats_no_data", frozenset()),
    ("h_stats_bot_excluded", frozenset()),
    ("h_stats_anonymous", frozenset()),
    ("h_stats_group_only", frozenset()),
    # RR-1 #6 full /chatstats card (handlers/chatstats.py). The three
    # enrichment blocks are one template each; the members block is
    # composed from bare labels instead, because its "Всего" segment
    # disappears when Telegram won't report the member count.
    ("h_chatstats_title", frozenset()),
    ("h_chatstats_card_subtitle", frozenset()),
    ("h_chatstats_period_today", frozenset()),
    ("h_chatstats_period_days", frozenset({"days"})),
    ("h_chatstats_members", frozenset()),
    ("h_chatstats_members_total", frozenset()),
    ("h_chatstats_members_week", frozenset()),
    ("h_chatstats_members_new", frozenset()),
    ("h_chatstats_games", frozenset()),
    ("h_chatstats_games_line", frozenset({"today", "total"})),
    ("h_chatstats_economy", frozenset()),
    ("h_chatstats_economy_line", frozenset({"coins", "avg", "max"})),
    ("h_chatstats_donations", frozenset()),
    ("h_chatstats_donations_line", frozenset({"total", "position"})),
    # A-08 keys the enriched card kept. They were never pinned here, so
    # a rename inside the YAML would have gone unnoticed until a group
    # actually ran the command — the exhaustive AST scan below only
    # checks that a key *exists* in both languages, never its
    # placeholders.
    ("h_chatstats_activity", frozenset()),
    ("h_chatstats_activity_line", frozenset({"today", "week", "avg"})),
    ("h_chatstats_top_title", frozenset()),
    ("h_chatstats_top_empty", frozenset()),
    ("h_chatstats_messages_unit", frozenset()),
    ("h_chatstats_group_only", frozenset()),
    ("h_topactive_title", frozenset({"days"})),
    ("h_topactive_empty", frozenset({"days"})),
    # RR-1 #3 — the restored /profile group-caption blocks. The action
    # labels are looked up through a dict in ``handlers/profile.py``
    # (``_LOG_ACTION_LABELS``), so the AST scan below can't see them at
    # all: they are only reachable from here.
    ("h_profile_msg_since_join", frozenset()),
    ("h_profile_last_actions", frozenset()),
    ("h_profile_log_action_warn", frozenset()),
    ("h_profile_log_action_unwarn", frozenset()),
    ("h_profile_log_action_ban", frozenset()),
    ("h_profile_log_action_kick", frozenset()),
    ("h_profile_log_action_mute", frozenset()),
    ("h_profile_log_action_unmute", frozenset()),
    ("h_profile_log_action_unban", frozenset()),
    ("h_profile_log_action_fine", frozenset()),
    ("h_profile_log_action_other", frozenset()),
    ("h_profile_vip", frozenset()),
    ("h_profile_vip_active", frozenset({"until", "days"})),
]


_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _extract_placeholders(template: str) -> frozenset[str]:
    return frozenset(_PLACEHOLDER_RE.findall(template))


@pytest.mark.parametrize(("key", "expected"), _HANDLER_KEYS)
def test_handler_key_present_in_both_langs(key: str, expected: frozenset[str]) -> None:
    """Both ``ru.yaml`` and ``en.yaml`` carry the key with the same
    placeholders. Falling back via :func:`t` would mask an EN omission;
    we hit the loader directly so the gap is visible.
    """
    ru, en = _load("ru"), _load("en")
    assert key in ru, f"{key} missing from ru.yaml"
    assert key in en, f"{key} missing from en.yaml"
    assert _extract_placeholders(ru[key]) == expected, key
    assert _extract_placeholders(en[key]) == expected, key


def test_send_success_renders_with_handler_kwargs() -> None:
    """End-to-end render with the exact kwargs ``handlers/send.py`` passes."""
    out = t(
        "h_send_success",
        "ru",
        recipient="<a href='tg://user?id=12345'>Alice</a>",
        amount="100",
        received="90",
        tax="10",
        balance="500",
        comment_line="\n📝 Комментарий: «спасибо»",
    )
    assert "tg://user?id=12345" in out
    assert "Alice" in out
    assert "спасибо" in out
    assert "Перевод выполнен" in out
    assert "{" not in out, "unresolved placeholder leaked into render"


def test_daily_cooldown_renders_with_handler_kwargs() -> None:
    out = t("h_daily_cooldown", "ru", streak=3, wait="1ч 30м", pmin="11", pmax="60")
    assert "Бонус уже получен" in out
    assert "<b>3</b>" in out
    assert "1ч 30м" in out
    assert "11" in out
    assert "60" in out
    assert "{" not in out


def test_daily_wait_short_lang_split() -> None:
    """Russian uses ``ч`` / ``м``, English uses ``h`` / ``m`` — the
    handler's ``_format_wait`` relies on this divergence to render the
    right glyph without a Python-side branch on lang.
    """
    assert t("h_daily_wait_short", "ru", hours=2, minutes=15) == "2ч 15м"
    assert t("h_daily_wait_short", "en", hours=2, minutes=15) == "2h 15m"


def test_handler_keys_disjoint_from_legacy_prefixes() -> None:
    """No collision with the ``send_*`` / ``daily_*`` / ``transfer_*``
    keys imported from translations.py — the handler keys live behind a
    fresh ``h_`` prefix on purpose so an operator hotfix to legacy copy
    can never silently bleed into the new aiogram pipeline.
    """
    ru = _load("ru")
    for key, _ in _HANDLER_KEYS:
        assert key.startswith("h_"), f"{key} missing h_ prefix"
        # The legacy companion (with the prefix stripped) should still
        # be a distinct entry — e.g. ``send_usage_username`` exists in
        # legacy and is unrelated to ``h_send_usage``.
        legacy_form = key.removeprefix("h_")
        if legacy_form in ru:
            assert ru[key] != ru[legacy_form], (
                f"{key} and {legacy_form} share a value — namespace collapse"
            )


# ---------------------------------------------------------------------------
# Exhaustive ``t("literal", ...)`` key scan (AUD-6 hardening)
# ---------------------------------------------------------------------------
#
# The ``_HANDLER_KEYS`` table above is a *placeholder contract* — it pins the
# exact ``{...}`` kwargs a handful of hand-picked handler cards pass. It is NOT
# a completeness guard: it only knows about the ~24 keys someone remembered to
# list, which is precisely how ``h_mod_mute_protected`` /
# ``h_trights_confirm_btn`` / ``h_trights_cancel_btn`` shipped missing from the
# YAML and slipped past CI (AUD-6).
#
# This scan closes that hole. It statically collects EVERY ``t("literal", ...)``
# call across ``src/telegram_invite_bot/**/*.py`` and asserts each literal key
# exists in BOTH ``ru.yaml`` and ``en.yaml``. A new ``t("h_brand_new", ...)``
# with no YAML entry fails here the moment it lands — no allowlist to update.
#
# Dynamically-built keys (``t(f"h_top_{mode}_header", ...)``, ``t(outcome_key,
# ...)``, ``t(MAP[x], ...)``) CANNOT be resolved statically, so the scan skips
# any call whose first arg is not a plain string ``Constant``. Those families
# are covered by their own handler/e2e tests; listing the legit f-string
# call-sites here documents *why* they are exempt and keeps the scan
# exhaustive-yet-precise (a brand-new literal key can never hide behind the
# "it's dynamic" excuse because literals are never skipped).

# Modules/prefixes known to legitimately build i18n keys dynamically (first arg
# is an f-string / Name / Subscript / Call, never a bare literal). Documented
# for the reader; the scan already skips non-literal first args structurally —
# this list is the human-readable audit trail, not a functional allowlist.
_KNOWN_DYNAMIC_KEY_SITES = (
    "handlers/top.py",  # f"h_top_{mode}_header" / _empty / row keys
    "handlers/moderation.py",  # t(mod.reason, ...) — CanModerate.reason key
    "core/ranks.py",  # f"rank_{...}" rank-name keys
    "handlers/rp.py",  # f-string roleplay action keys
    "handlers/rps.py",  # outcome→key maps (t(KEY_FOR_OUTCOME[...]))
    "handlers/games.py",  # game-state Name keys
    "handlers/marriage.py",  # status Name keys
    "handlers/duel.py",  # outcome Name keys
    "handlers/p2p_trade.py",  # trade-state Name keys
    "handlers/checks.py",  # subscripted lookup keys
    "handlers/vip.py",  # f"h_vip_plan_name_{days}" per-plan copy
    "handlers/voice_settings.py",  # f-string / IfExp voice keys
    "keyboards/builders/voice_settings.py",
    "keyboards/builders/pagination.py",
)


def _iter_literal_t_keys() -> list[tuple[str, str, int]]:
    """Statically collect every ``t("literal", ...)`` call in the real pipeline.

    Returns ``(key, "rel/path.py", lineno)`` triples. Only ``t`` called as a
    bare name with a string-``Constant`` first arg is collected — attribute
    calls (``obj.t(...)``) and dynamic first args (f-string ``JoinedStr``,
    ``Name``, ``BinOp``, ``Subscript``, ``Call``, ``IfExp`` …) are skipped
    because they cannot be resolved without running the handler.
    """
    found: list[tuple[str, str, int]] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            # i18n ``t`` is always imported and called as a bare name; there
            # are no ``something.t("literal")`` call-sites in the tree (the
            # only ``.t`` attribute calls take dynamic args). Restricting to
            # ``Name`` avoids false positives from unrelated ``.t`` methods.
            if not (isinstance(func, ast.Name) and func.id == "t"):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((first.value, rel, first.lineno))
            # else: dynamically-built key — unresolvable, skip (see
            # _KNOWN_DYNAMIC_KEY_SITES for the audit rationale).
    return found


# ---------------------------------------------------------------------------
# Telegram-HTML validity of every handler-reachable string
# ---------------------------------------------------------------------------
#
# ``di/providers.py`` builds the ``Bot`` with
# ``DefaultBotProperties(parse_mode=ParseMode.HTML)``, so EVERY string that
# reaches ``message.answer`` / ``edit_text`` is parsed as HTML by Telegram —
# there is no per-call opt-out in the handlers. Telegram's parser is strict:
# an unknown tag, an unbalanced tag, or a bare ``<`` that isn't a tag makes
# the API reject the whole send with ``400 Bad Request: can't parse
# entities``, i.e. the user gets NOTHING, not a slightly-wrong card.
#
# This bit for real: ``h_daily_wait_lt_minute`` shipped as ``"< 1 мин"`` /
# ``"<1 min"``. Telegram reads the ``<`` as the start of a tag and errors with
# ``Unsupported start tag`` — so the /daily cooldown card and the /start card
# both 400'd during the ~1 minute a day the sub-minute branch renders. The
# fix is ``&lt;`` in the YAML; this test is the guard.
#
# Scope: keys the NEW pipeline can reach — the ``_HANDLER_KEYS`` contract
# table, every literal ``t("...")`` collected by the AST scan above, AND every
# ``h_``-prefixed YAML key. The prefix sweep is what makes this exhaustive:
# the dynamic-key families (``f"h_top_{mode}_header"``, ``f"h_vip_plan_name_
# {days}"``, the outcome→key maps — see ``_KNOWN_DYNAMIC_KEY_SITES``) are
# unreachable for the AST scan but are all inside the ``h_`` namespace that
# ``test_handler_keys_disjoint_from_legacy_prefixes`` pins.
#
# Legacy-only keys are deliberately excluded: several carry usage placeholders
# like ``<сумма>`` and the telebot process that rendered them no longer runs,
# so failing on them would be noise. The moment such a key is wired into an
# ``h_``-era handler via ``t("literal", ...)`` it enters this scan through the
# AST branch and must be escaped first.

# Tags Telegram's HTML parser accepts (Bot API "HTML style" + the aliases
# tdlib recognises). Anything else is an "Unsupported start tag" 400.
_TELEGRAM_HTML_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "code",
        "del",
        "em",
        "i",
        "ins",
        "pre",
        "s",
        "span",
        "strike",
        "strong",
        "tg-emoji",
        "tg-spoiler",
        "u",
    }
)

# ``<name attrs...>`` / ``</name>``. The name group is ``*`` (not ``+``) on
# purpose: ``"< 1 мин"`` must match with an EMPTY name so it is reported as an
# unsupported tag — which is exactly how Telegram treats it — instead of
# slipping through as plain text.
_HTML_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9-]*)[^>]*>")


def _html_errors(value: str) -> list[str]:
    """Return human-readable Telegram-HTML violations in ``value`` (empty = ok)."""
    errors: list[str] = []
    stack: list[str] = []
    for match in _HTML_TAG_RE.finditer(value):
        closing, name = match.group(1), match.group(2).lower()
        if name not in _TELEGRAM_HTML_TAGS:
            errors.append(f"unsupported tag {match.group(0)!r}")
            continue
        if not closing:
            stack.append(name)
        elif not stack or stack[-1] != name:
            errors.append(f"unbalanced {match.group(0)!r}")
        else:
            stack.pop()
    if stack:
        errors.append("unclosed " + ", ".join(f"<{name}>" for name in stack))
    # A ``<`` left over once the tags are removed is a bare less-than; Telegram
    # tries to parse it as a tag and 400s. It must be written ``&lt;``.
    leftover = _HTML_TAG_RE.sub("", value)
    if "<" in leftover:
        errors.append("bare '<' (write it as &lt;)")
    return errors


def test_handler_strings_are_valid_telegram_html() -> None:
    """Every key the aiogram pipeline renders must survive Telegram's parser."""
    keys = {key for key, _ in _HANDLER_KEYS}
    keys.update(key for key, _rel, _lineno in _iter_literal_t_keys())

    failures: list[str] = []
    for lang in ("ru", "en"):
        data = _load(lang)
        # ``h_`` is the new pipeline's namespace — sweeping it covers the
        # dynamically-built keys the AST scan structurally cannot resolve.
        lang_keys = keys | {key for key in data if key.startswith("h_")}
        for key in sorted(lang_keys):
            value = data.get(key)
            if not isinstance(value, str):
                continue  # missing keys are the other tests' job
            for error in _html_errors(value):
                failures.append(f"{lang}:{key}: {error} — in {value!r}")
    assert not failures, "invalid Telegram HTML in i18n copy:\n" + "\n".join(failures)


def test_html_error_detector_catches_the_regressions_it_guards() -> None:
    """Meta-test: the detector above is only worth having if it actually
    fires. Pin the three shapes that reach production as a 400 — a bare
    ``<``, an unknown tag, and an unclosed one — plus the escaped form that
    must stay legal.
    """
    assert _html_errors("< 1 мин")
    assert _html_errors("<1 min")
    assert _html_errors("/fine <amount>")
    assert _html_errors("<b>bold")
    assert _html_errors("<b>bold</i>")
    assert _html_errors("&lt; 1 мин") == []
    assert _html_errors("<b>bold</b> and <code>x</code>") == []


# ---------------------------------------------------------------------------
# Callback-alert copy must be plain text
# ---------------------------------------------------------------------------
#
# ``answerCallbackQuery`` takes NO ``parse_mode`` — Telegram shows the text
# verbatim. So the global HTML default that makes ``<b>…</b>`` render in a
# message card does nothing here: a card key reused as a toast/alert puts a
# literal ``<b>500.00 RUB</b>`` in front of the user.
#
# That is exactly what ``h_p2p_buy_above_max`` did: the typed-amount branch
# replies with it as an HTML message, and the "buy all" branch showed the same
# key as an alert. The fix is ``utils.html.plain_text`` at the popup call-site
# (one i18n key, two surfaces); this scan makes the next one fail in CI.

# Receiver names that denote a ``CallbackQuery`` in this codebase. Restricting
# by name keeps the scan precise — ``message.answer`` is an HTML surface and
# must NOT be swept.
_CALLBACK_RECEIVERS = frozenset({"callback", "call", "cb", "query"})

_MARKUP_RE = re.compile(
    r"</?(?:" + "|".join(sorted(_TELEGRAM_HTML_TAGS)) + r")\b[^>]*>", re.IGNORECASE
)


def _iter_callback_alert_keys() -> list[tuple[str, str, int, bool]]:
    """Collect ``callback.answer(t("literal", …))`` sites (raw, unstripped).

    A first arg wrapped in ``plain_text(...)`` is skipped — that IS the fix,
    so a site that applies it is compliant no matter what the copy holds. A
    ``t(...)[:200]`` slice is unwrapped and still checked: truncation solves
    the length cap, not the markup leak.

    The fourth tuple element records whether that slice was present, because
    the two pins below want opposite things from it: the markup scan checks
    a truncated site anyway, the length scan skips it.
    """
    found: list[tuple[str, str, int, bool]] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "answer"):
                continue
            receiver = func.value
            name = receiver.id if isinstance(receiver, ast.Name) else ""
            if name not in _CALLBACK_RECEIVERS:
                continue
            arg = node.args[0]
            capped = isinstance(arg, ast.Subscript)
            if capped:  # the ``[:200]`` alert-cap slice
                arg = arg.value  # type: ignore[attr-defined]
            if not (isinstance(arg, ast.Call) and arg.args):
                continue
            inner = arg.func
            if not isinstance(inner, ast.Name):
                continue
            if inner.id == "plain_text":  # already stripped — compliant
                continue
            if inner.id != "t":
                continue
            key = arg.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found.append((key.value, rel, node.lineno, capped))
    return found


def test_callback_alert_copy_carries_no_markup() -> None:
    """Alerts render verbatim — an unstripped card key leaks its tags."""
    sites = _iter_callback_alert_keys()
    # Sanity: the scan must actually be finding call-sites, or it would pass
    # vacuously after any refactor that renames the receiver variable.
    assert len(sites) > 50, f"alert scan found only {len(sites)} sites — is it still wired?"

    failures: list[str] = []
    for lang in ("ru", "en"):
        data = _load(lang)
        for key, rel, lineno, _capped in sites:
            value = data.get(key)
            if not isinstance(value, str):
                continue
            markup = _MARKUP_RE.findall(value)
            if markup:
                failures.append(
                    f"{rel}:{lineno} shows {lang}:{key} as an alert, but the copy "
                    f"carries HTML ({value!r}) — wrap it in plain_text()"
                )
    assert not failures, "markup leaking into a callback alert:\n" + "\n".join(failures)


def test_callback_alert_copy_fits_the_toast_cap() -> None:
    """A toast is 200 characters, and copy is the half that CI can check.

    Same call-sites as the markup pin, different failure: Telegram
    rejects an over-long ``answerCallbackQuery`` outright, so the user
    taps a button and nothing happens at all. ``h_groupstats_boost_hint``
    is the reason this is a test and not a comment — it is marketing
    copy shown as an alert, it reached 181 UTF-16 units in EN, and
    nothing in the repo would have said a word if the next edit had
    pushed it over.

    Two deliberate limits on what this pin claims:

    * Sites that already slice ``[:200]`` are skipped — truncation is
      the cap, and re-checking the template there would demand a fix
      that changes nothing.
    * The *template* is measured, not a rendered string. A key with
      ``{amount}`` can still overflow once a real number lands in it;
      that is a runtime shape no static scan sees, and it is exactly
      what ``LengthGuardMiddleware`` was extended to catch (it now
      measures ``AnswerCallbackQuery.text`` against 200, not 4096).
      Static pin here, dynamic guard there — neither alone is enough.
    """
    sites = _iter_callback_alert_keys()
    assert len(sites) > 50, f"alert scan found only {len(sites)} sites — is it still wired?"

    failures: list[str] = []
    for lang in ("ru", "en"):
        data = _load(lang)
        for key, rel, lineno, capped in sites:
            if capped:
                continue
            value = data.get(key)
            if not isinstance(value, str):
                continue
            length = utf16_length(value)
            if length > TELEGRAM_CALLBACK_ANSWER_LIMIT:
                failures.append(
                    f"{rel}:{lineno} shows {lang}:{key} as an alert, but the copy "
                    f"is {length} UTF-16 units against a "
                    f"{TELEGRAM_CALLBACK_ANSWER_LIMIT} cap — Telegram rejects the "
                    f"whole call and the tap does nothing"
                )
    assert not failures, "callback alert copy over the toast cap:\n" + "\n".join(failures)


def test_known_dynamic_key_sites_still_use_dynamic_keys() -> None:
    """Tripwire: if a documented dynamic-key module starts using a *literal*
    ``t()`` key, that's fine (the exhaustive scan will check it) — but if one
    *stops* building keys dynamically entirely we want to notice, because the
    ``_KNOWN_DYNAMIC_KEY_SITES`` rationale would be stale. We assert the listed
    modules all still exist so the audit trail can't silently rot.
    """
    for rel in _KNOWN_DYNAMIC_KEY_SITES:
        assert (_SRC_ROOT / rel).is_file(), (
            f"{rel} is listed in _KNOWN_DYNAMIC_KEY_SITES but no longer exists "
            "— update the audit list."
        )


def test_every_literal_t_key_exists_in_both_langs() -> None:
    """EXHAUSTIVE guard: every ``t("literal", ...)`` key in the real pipeline
    resolves in BOTH ``ru.yaml`` and ``en.yaml``.

    This is the regression guard AUD-6 wanted: a missing key fails CI here
    with a precise ``file:line`` list, instead of rendering the raw key string
    to a user at runtime (the i18n fallback chain ends at "return the key").
    """
    ru, en = _load("ru"), _load("en")
    missing: list[str] = []
    for key, rel, lineno in _iter_literal_t_keys():
        absent_in = [lang for lang, table in (("ru", ru), ("en", en)) if key not in table]
        if absent_in:
            missing.append(f"{key!r} (missing from {'+'.join(absent_in)}) — {rel}:{lineno}")
    assert not missing, (
        "Literal t() keys absent from one or both language files "
        f"({len(missing)}):\n  " + "\n  ".join(sorted(missing)) + "\n"
        "Add the key to BOTH src/telegram_invite_bot/i18n/data/ru.yaml and "
        "en.yaml (or, if it is genuinely built dynamically, ensure the t() "
        "first arg is not a string literal)."
    )


def test_every_vip_plan_has_curated_copy_in_both_langs() -> None:
    """RR-2 #21: ``handlers/vip.py`` builds its per-plan keys from the
    duration table (``f"h_vip_plan_name_{days}"``), so the exhaustive
    literal scan above cannot see them.

    Pin them here instead: every canonical plan the effect planner knows
    how to grant must have a name AND a perk blurb in both languages,
    otherwise ``/vip_shop`` renders the raw key string as the plan title.
    """
    ru, en = _load("ru"), _load("en")
    missing: list[str] = []
    for days in sorted(set(VIP_NAME_DURATIONS.values())):
        for key in (f"h_vip_plan_name_{days}", f"h_vip_plan_desc_{days}"):
            absent_in = [lang for lang, table in (("ru", ru), ("en", en)) if key not in table]
            if absent_in:
                missing.append(f"{key!r} (missing from {'+'.join(absent_in)})")
    assert not missing, "VIP plan copy missing for a canonical duration:\n  " + "\n  ".join(missing)


#: Tags Telegram's HTML parse mode accepts (core.telegram.org/bots/api
#: #html-style). Anything else makes Telegram reject the WHOLE message
#: with "Unsupported start tag", so the handler answers nothing at all.
_TELEGRAM_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "span",
        "tg-spoiler",
        "a",
        "code",
        "pre",
        "blockquote",
    }
)

_TAG_RE = re.compile(r"<\s*/?\s*([^\s>/]+)[^>]*>")


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_handler_copy_uses_only_telegram_html_tags(lang: str) -> None:
    """Handler-owned (``h_``) copy must be parseable as Telegram HTML.

    Found live in RR-4 #45: several usage lines spelled their
    placeholders as bare ``<текст>`` / ``<rank>``. Under the bot's
    global ``parse_mode=HTML`` that is an unsupported start tag —
    ``/perm``, ``/setwelcome`` and ``/admin_withdrawals`` with no
    arguments raised a BadRequest and replied nothing. Wrap the
    placeholder in ``<code>`` without the angle brackets, or show a
    worked example.

    Scoped to ``h_`` keys on purpose: the legacy-ported keys carry the
    same defect but are frozen byte-for-byte by
    ``test_legacy_parity`` — they are dead in the new pipeline and
    fixing them there would break the parity guard.
    """
    offenders = [
        f"{key}: <{tag}>"
        for key, template in _load(lang).items()
        if key.startswith("h_")
        for tag in _TAG_RE.findall(template)
        if tag.lower() not in _TELEGRAM_TAGS
    ]
    assert not offenders, (
        f"{lang}.yaml handler copy carries non-Telegram HTML tags "
        f"({len(offenders)}):\n  " + "\n  ".join(sorted(offenders))
    )
