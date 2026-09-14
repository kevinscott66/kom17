"""Unit tests for the in-process passive-earning anti-spam gate (A-03).

These pin the legacy ``should_reward_message`` heuristic that
:class:`_RewardTracker` ports: min-length, alnum gate, all-same-char
rejection, per-user cooldown, trailing-60s rate cap, and the duplicate
text window. State is process-local, so a fresh tracker per case keeps
the assertions independent.

They also cover the one gate legacy did NOT have: the per-day coin
ceiling added by T-019 (``docs/ECONOMY_RATE_AUDIT.md`` §2.1). The legacy
cases below run with ``daily_cap=0`` (disabled) so they keep pinning the
legacy heuristic exactly.
"""

from __future__ import annotations

from telegram_invite_bot.middlewares.message_activity import _RewardTracker

_DAY = "2026-08-09"
_NEXT_DAY = "2026-08-10"


def _tracker(
    *,
    min_chars: int = 8,
    cooldown_sec: int = 20,
    max_per_minute: int = 3,
    duplicate_window_sec: int = 120,
    daily_cap: int = 0,
) -> _RewardTracker:
    return _RewardTracker(
        min_chars=min_chars,
        cooldown_sec=cooldown_sec,
        max_per_minute=max_per_minute,
        duplicate_window_sec=duplicate_window_sec,
        daily_cap=daily_cap,
    )


def test_rewards_a_qualifying_message() -> None:
    tracker = _tracker()
    assert tracker.should_reward(1, "Hello there friend", now=100.0, today=_DAY) is True


def test_command_message_never_rewards() -> None:
    tracker = _tracker()
    assert tracker.should_reward(1, "/balance please", now=100.0, today=_DAY) is False


def test_empty_text_never_rewards() -> None:
    tracker = _tracker()
    assert tracker.should_reward(1, "", now=100.0, today=_DAY) is False


def test_too_short_message_rejected() -> None:
    tracker = _tracker(min_chars=8)
    # 7 non-space chars < 8.
    assert tracker.should_reward(1, "abcdefg", now=100.0, today=_DAY) is False


def test_pure_punctuation_rejected() -> None:
    tracker = _tracker()
    # Long enough but no letter/digit.
    assert tracker.should_reward(1, "!!!!!!!!!!!", now=100.0, today=_DAY) is False


def test_all_same_char_rejected() -> None:
    tracker = _tracker()
    assert tracker.should_reward(1, "ааааааааааа", now=100.0, today=_DAY) is False


def test_cooldown_blocks_second_immediate_reward() -> None:
    tracker = _tracker(cooldown_sec=20)
    assert tracker.should_reward(1, "first message here", now=100.0, today=_DAY) is True
    # 5s later — still inside the 20s cooldown.
    assert tracker.should_reward(1, "second message here", now=105.0, today=_DAY) is False
    # 20s later — cooldown elapsed.
    assert tracker.should_reward(1, "third message here", now=120.0, today=_DAY) is True


def test_rate_cap_over_trailing_minute() -> None:
    tracker = _tracker(cooldown_sec=1, max_per_minute=3)
    assert tracker.should_reward(1, "message number one", now=100.0, today=_DAY) is True
    assert tracker.should_reward(1, "message number two", now=102.0, today=_DAY) is True
    assert tracker.should_reward(1, "message number three", now=104.0, today=_DAY) is True
    # Fourth within the same 60s window — capped.
    assert tracker.should_reward(1, "message number four", now=106.0, today=_DAY) is False
    # Past the window of the first grant — allowed again.
    assert tracker.should_reward(1, "message number five", now=161.0, today=_DAY) is True


def test_duplicate_text_within_window_rejected() -> None:
    tracker = _tracker(cooldown_sec=1, duplicate_window_sec=120)
    assert tracker.should_reward(1, "Repeated Phrase Here", now=100.0, today=_DAY) is True
    # Same text (case/space-normalised) inside the dup window.
    assert tracker.should_reward(1, "repeated   phrase here", now=110.0, today=_DAY) is False
    # Same text after the dup window — allowed.
    assert tracker.should_reward(1, "repeated phrase here", now=225.0, today=_DAY) is True


def test_per_user_state_is_independent() -> None:
    tracker = _tracker(cooldown_sec=20)
    assert tracker.should_reward(1, "user one message", now=100.0, today=_DAY) is True
    # A different user is not affected by user 1's cooldown.
    assert tracker.should_reward(2, "user two message", now=100.0, today=_DAY) is True


# --- T-019: the per-day coin ceiling ---------------------------------


def test_take_is_unbounded_when_the_cap_is_disabled() -> None:
    """``daily_cap=0`` restores legacy behaviour — no ceiling at all."""
    tracker = _tracker(daily_cap=0)
    assert tracker.take(1, _DAY, 4_320) == 4_320
    assert tracker.take(1, _DAY, 4_320) == 4_320


def test_take_clamps_the_last_grant_to_the_remainder() -> None:
    """The reward that crosses the line pays the remainder, not zero.

    Dropping it whole would make the effective cap depend on the boost
    multiplier (a ×3 user would stop at 148 instead of 150); clamping
    lands every user on exactly the cap.
    """
    tracker = _tracker(daily_cap=150)
    assert tracker.take(1, _DAY, 100) == 100
    assert tracker.take(1, _DAY, 90) == 50
    assert tracker.take(1, _DAY, 1) == 0


def test_should_reward_refuses_once_the_allowance_is_spent() -> None:
    """An exhausted user is turned away before any DB work is attempted."""
    tracker = _tracker(cooldown_sec=0, daily_cap=10)
    assert tracker.should_reward(1, "message number one", now=100.0, today=_DAY) is True
    assert tracker.take(1, _DAY, 10) == 10
    assert tracker.should_reward(1, "message number two", now=200.0, today=_DAY) is False


def test_allowance_resets_on_the_next_calendar_day() -> None:
    tracker = _tracker(cooldown_sec=0, daily_cap=10)
    assert tracker.take(1, _DAY, 10) == 10
    assert tracker.should_reward(1, "message number one", now=100.0, today=_DAY) is False
    assert tracker.should_reward(1, "message number two", now=200.0, today=_NEXT_DAY) is True
    assert tracker.take(1, _NEXT_DAY, 10) == 10


def test_the_cap_is_per_user() -> None:
    tracker = _tracker(daily_cap=10)
    assert tracker.take(1, _DAY, 10) == 10
    assert tracker.take(2, _DAY, 10) == 10


def test_an_active_capped_user_is_not_handed_a_fresh_allowance() -> None:
    """LRU eviction must not become a cap reset — the loud case.

    Kept as filed even though the counter no longer lives in the evicted
    value (#224): it is the cheapest end-to-end statement of the property,
    and it must not start passing for a new reason without anyone noticing
    that the silent twin below is the one doing the real work.
    """
    tracker = _tracker(cooldown_sec=0, daily_cap=10)
    assert tracker.take(1, _DAY, 10) == 10

    # Push far more than _MAX_TRACKED_USERS distinct users through, while
    # the capped user keeps messaging (and keeps being refused).
    for other in range(2, 500):
        tracker.should_reward(other, f"filler message {other}", now=100.0, today=_DAY)
        assert tracker.should_reward(1, f"grinding away {other}", now=100.0, today=_DAY) is False

    assert tracker.take(1, _DAY, 10) == 0


def test_a_silent_capped_user_is_not_handed_a_fresh_allowance() -> None:
    """#224: the case the LRU refresh could never cover.

    ``_state`` moves a user to the end of the map on every message, which
    protects a grinder who keeps typing. It cannot protect one who stops:
    exhaust the cap, say nothing while 200 other people talk, come back.
    Until #224 the day's earnings were a field on the evicted value, so
    that walk away and return was worth a whole second daily allowance —
    and unlike a restart, it is something the grinder can arrange.

    Prod exposure was nil (23 lifetime users), which is why this shipped
    as a correctness fix rather than an incident. The number of strangers
    needed is a deployment detail, not a design boundary.
    """
    tracker = _tracker(cooldown_sec=0, daily_cap=10)
    assert tracker.take(1, _DAY, 10) == 10

    # The victim says nothing at all while the map turns over twice.
    for other in range(2, 500):
        tracker.should_reward(other, f"filler message {other}", now=100.0, today=_DAY)

    assert tracker.take(1, _DAY, 10) == 0
    assert tracker.should_reward(1, "back after a long silence", now=100.0, today=_DAY) is False


def test_the_allowance_map_is_emptied_on_a_new_day_not_grown() -> None:
    """The day counter needs no eviction of its own — it needs a date.

    Moving the counter out of the LRU (#224) would be a memory leak if the
    map only ever grew, and adding a second eviction policy would
    reintroduce the bug in a new place. Instead the whole map is dropped
    the first time a later day is seen.
    """
    tracker = _tracker(cooldown_sec=0, daily_cap=10)
    for user in range(1, 400):
        assert tracker.take(user, _DAY, 10) == 10
    assert len(tracker._earned) == 399

    # First call on the new day empties it wholesale.
    assert tracker.take(1, _NEXT_DAY, 10) == 10
    assert len(tracker._earned) == 1


def test_earning_coins_does_not_by_itself_consume_an_lru_slot() -> None:
    """``take`` no longer touches ``_users`` (#224).

    It used to call ``_state``, so crediting a reward created an entry in
    the 200-slot map as a side effect — the tightest LRU in the tree,
    pressured by the one operation with no need of it.
    """
    tracker = _tracker(daily_cap=100)
    assert tracker.take(4242, _DAY, 5) == 5
    assert 4242 not in tracker._users


def test_the_all_same_char_gate_folds_case_like_legacy() -> None:
    """#758: legacy lowercased before it counted distinct characters.

    ``bot.py:43725`` normalises with ``.lower()`` and ``bot.py:43737``
    builds the set from that string, so "АаАаАаАа" was one character
    there. The port built the set from the raw text, making it two —
    the whole gate defeated by holding shift every other keystroke.
    """
    tracker = _tracker()
    assert tracker.should_reward(1, "аааааааааа", now=100.0, today=_DAY) is False
    assert tracker.should_reward(2, "АаАаАаАаАа", now=100.0, today=_DAY) is False
    assert tracker.should_reward(3, "AaAaAaAaAa", now=100.0, today=_DAY) is False
    # Genuinely mixed content is still rewarded: the fold must not widen
    # the gate into a two-distinct-letters rejection.
    assert tracker.should_reward(4, "Aa but with words", now=100.0, today=_DAY) is True


def test_the_allowance_map_stays_empty_while_the_cap_is_disabled() -> None:
    """#756: with the cap off nothing meters ``_earned`` and no day-roll
    ever clears it.

    ``_remaining_today`` returns ``None`` *before* it reaches
    ``_roll_day``, so the map is never emptied; ``take`` used to write to
    it anyway, and it sits outside the LRU by design. One entry per
    rewarded user, for the life of the process. Unreachable on the
    shipped default (cap 150) — it is the operator who sets 0 "to
    disable the limit" who pays.
    """
    tracker = _tracker(daily_cap=0)
    for user in range(1, 400):
        assert tracker.take(user, _DAY, 10) == 10
    assert tracker._earned == {}


def test_give_back_returns_an_allowance_whose_credit_never_landed() -> None:
    """#757: ``take`` books on the assumption the credit will land.

    When the wallet refuses it (balance cap) nothing is minted, so the
    cap — which exists to bound minting — must not have moved either.
    """
    tracker = _tracker(daily_cap=10)
    assert tracker.take(1, _DAY, 4) == 4
    tracker.give_back(1, _DAY, 4)
    # The full allowance is available again, not 6.
    assert tracker.take(1, _DAY, 10) == 10


def test_give_back_does_not_refund_across_a_day_boundary() -> None:
    """A grant taken just before midnight must not be refunded out of the
    new day's allowance — that would hand the grinder a free top-up for
    every failed credit straddling the roll."""
    tracker = _tracker(daily_cap=10)
    assert tracker.take(1, _DAY, 10) == 10
    # New day: the map rolls, and yesterday's booking is already gone.
    assert tracker.take(1, _NEXT_DAY, 4) == 4
    tracker.give_back(1, _DAY, 10)
    # Today's 4 still stands; the stale refund bought nothing.
    assert tracker.take(1, _NEXT_DAY, 10) == 6


def test_give_back_is_inert_while_the_cap_is_disabled() -> None:
    """The exact inverse of ``take``, which books nothing with the cap
    off (#756) — so the refund must not conjure an entry either."""
    tracker = _tracker(daily_cap=0)
    assert tracker.take(1, _DAY, 5) == 5
    tracker.give_back(1, _DAY, 5)
    assert tracker._earned == {}


def test_a_fresh_process_asks_for_a_seed_exactly_once_per_user_per_day() -> None:
    """#1789: the ledger read is a per-user-per-day cost, not a per-message one.

    ``needs_seed`` is the whole hot-path budget of the fix: the very
    first qualifying message a user sends in a process pays one indexed
    query, and every message after it is the dict lookup it always was.
    """
    tracker = _tracker(daily_cap=150)
    assert tracker.needs_seed(1, _DAY) is True
    tracker.seed(1, _DAY, 40)
    assert tracker.needs_seed(1, _DAY) is False
    # A different user is a different question.
    assert tracker.needs_seed(2, _DAY) is True
    # A new day is too — the ledger has to be re-read against new bounds.
    assert tracker.needs_seed(1, _NEXT_DAY) is True


def test_a_seeded_allowance_survives_what_a_restart_used_to_reset() -> None:
    """The defect itself: a fresh process handed back the whole cap.

    The second tracker stands in for the process that came up after a
    deploy. Before #1789 it knew nothing and granted 150 again; seeded
    from the ledger it grants only the 30 that were genuinely left.
    """
    before = _tracker(daily_cap=150)
    assert before.needs_seed(1, _DAY) is True
    before.seed(1, _DAY, 0)
    assert before.take(1, _DAY, 120) == 120

    after_restart = _tracker(daily_cap=150)
    assert after_restart.needs_seed(1, _DAY) is True
    after_restart.seed(1, _DAY, 120)
    assert after_restart.take(1, _DAY, 150) == 30
    assert after_restart.take(1, _DAY, 1) == 0


def test_seeding_never_lowers_a_booking_the_process_already_made() -> None:
    """A grant in flight has not reached the ledger yet.

    ``take`` books before the credit and the commit, so a seed that
    landed after it would read a smaller number. Taking the smaller one
    would hand that allowance straight back out, which is the bug this
    method exists to prevent — hence the ``max``.
    """
    tracker = _tracker(daily_cap=150)
    assert tracker.take(1, _DAY, 100) == 100
    tracker.seed(1, _DAY, 10)
    assert tracker.take(1, _DAY, 100) == 50


def test_seeding_is_idempotent_within_a_day() -> None:
    """A second seed is refused outright, not merely min/maxed.

    Two messages from the same user can be in flight at once; the
    loser's read is stale by then and must not be applied on top.
    """
    tracker = _tracker(daily_cap=150)
    tracker.seed(1, _DAY, 100)
    tracker.seed(1, _DAY, 140)
    assert tracker.take(1, _DAY, 150) == 50


def test_a_zero_seed_leaves_the_allowance_map_empty() -> None:
    """Most users have earned nothing yet; that must cost no memory.

    ``_earned`` means "users who actually earned today" and is bounded
    by that. Writing a zero for every user who merely sent a qualifying
    message would widen it to "users who talked today" for no gain —
    ``_remaining_today`` already reads a missing key as zero.
    """
    tracker = _tracker(daily_cap=150)
    tracker.seed(1, _DAY, 0)
    assert tracker._earned == {}
    assert tracker.take(1, _DAY, 150) == 150


def test_seeding_is_inert_while_the_cap_is_disabled() -> None:
    """#756 again: with the cap off nothing meters these maps.

    ``needs_seed`` must answer False so the call site never spends a
    query on a bound that does not exist, and ``seed`` must write
    nothing — either map would otherwise grow one entry per user for
    the life of the process.
    """
    tracker = _tracker(daily_cap=0)
    assert tracker.needs_seed(1, _DAY) is False
    tracker.seed(1, _DAY, 999)
    assert tracker._earned == {}
    assert tracker._seeded == set()
    assert tracker.take(1, _DAY, 4_320) == 4_320


def test_the_seed_marker_is_emptied_on_a_new_day_not_grown() -> None:
    """``_seeded`` has the same bound and the same lifetime as ``_earned``."""
    tracker = _tracker(daily_cap=150)
    for uid in range(400):
        assert tracker.needs_seed(uid, _DAY) is True
        tracker.seed(uid, _DAY, 1)
    assert len(tracker._seeded) == 400
    assert tracker.needs_seed(1, _NEXT_DAY) is True
    tracker.seed(1, _NEXT_DAY, 1)
    assert len(tracker._seeded) == 1
    assert len(tracker._earned) == 1
