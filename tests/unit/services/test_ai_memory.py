"""Unit tests for :mod:`telegram_invite_bot.services.ai_memory` (L-63/L-76)."""

from __future__ import annotations

from telegram_invite_bot.services.ai_memory import (
    _MAX_TURN_CHARS,
    AiMemoryStore,
    KomModeStore,
    recent_for_prompt,
    render_history_text,
)


def test_record_and_read_window() -> None:
    store = AiMemoryStore()
    store.record_exchange(1, "hi", "hello", chat_id=1)
    history = store.history(1, 1)
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_window_trims_to_max_turns() -> None:
    store = AiMemoryStore(max_turns=4)
    for i in range(5):
        store.record_exchange(1, f"q{i}", f"a{i}", chat_id=1)
    history = store.history(1, 1)
    # Only the last 4 turns survive (2 exchanges).
    assert len(history) == 4
    assert history[0]["content"] == "q3"
    assert history[-1]["content"] == "a4"


def test_per_chat_isolation() -> None:
    store = AiMemoryStore()
    store.record_exchange(1, "in-group", "g", chat_id=-100)
    store.record_exchange(1, "in-pm", "p", chat_id=1)
    assert store.size(1, -100) == 2
    assert store.size(1, 1) == 2
    assert store.history(1, -100)[0]["content"] == "in-group"
    assert store.history(1, 1)[0]["content"] == "in-pm"


def test_private_chat_id_folds_onto_user() -> None:
    store = AiMemoryStore()
    # chat_id=None in a private context maps to the user id.
    store.add(42, "user", "x")
    assert store.size(42, 42) == 1
    assert store.size(42, None) == 1


def test_clear_forgets_window() -> None:
    store = AiMemoryStore()
    store.record_exchange(1, "q", "a", chat_id=1)
    store.clear(1, 1)
    assert store.history(1, 1) == []


def test_a_long_turn_is_cut_to_the_char_ceiling() -> None:
    """#1623: the key cap on its own never bounded the store.

    ``add`` stored the model's answer verbatim, and the answer is
    capped in tokens, not characters — so one turn could weigh
    thousands of characters and a full store far more than the
    megabyte the old comment claimed.
    """
    store = AiMemoryStore(max_chars=10)
    store.add(1, "assistant", "a" * 100, 1)
    stored = store.history(1, 1)[0]["content"]
    # Exactly the ceiling, ellipsis included: the bound is arithmetic.
    assert len(stored) == 10
    assert stored == "aaaaaaaaa\u2026"


def test_a_turn_inside_the_ceiling_is_stored_verbatim() -> None:
    """The cut is a ceiling, not a reformat — nothing else is touched."""
    store = AiMemoryStore(max_chars=10)
    store.add(1, "user", "abcdefghij", 1)
    assert store.history(1, 1)[0]["content"] == "abcdefghij"


def test_the_ceiling_counts_characters_not_bytes() -> None:
    """Cyrillic is the audience, and it is the expensive case.

    The measured worst case quoted next to the constant is Cyrillic,
    so the cut has to be the one that was measured: characters. A
    byte-based cut would keep about half as much text per turn.
    """
    store = AiMemoryStore(max_chars=10)
    store.add(1, "assistant", "\u044f" * 100, 1)
    stored = store.history(1, 1)[0]["content"]
    assert len(stored) == 10
    assert stored == "\u044f" * 9 + "\u2026"


def test_record_exchange_cuts_both_halves() -> None:
    """The question is user-supplied and is bounded by the same ceiling."""
    store = AiMemoryStore(max_chars=5)
    store.record_exchange(1, "q" * 50, "a" * 50, chat_id=1)
    contents = [turn["content"] for turn in store.history(1, 1)]
    assert contents == ["qqqq\u2026", "aaaa\u2026"]


def test_the_shipped_ceiling_is_the_measured_one() -> None:
    """The MiB figures in the comment are true only at this value."""
    store = AiMemoryStore()
    store.add(1, "assistant", "a" * 5000, 1)
    assert len(store.history(1, 1)[0]["content"]) == _MAX_TURN_CHARS


def test_key_eviction_bounds_store() -> None:
    store = AiMemoryStore(max_keys=2)
    store.add(1, "user", "a", 1)
    store.add(2, "user", "b", 2)
    store.add(3, "user", "c", 3)  # evicts key (1, 1) — least recent
    assert store.size(1, 1) == 0
    assert store.size(2, 2) == 1
    assert store.size(3, 3) == 1


def test_recent_for_prompt_limits_and_strips() -> None:
    history = [{"role": "user", "content": str(i)} for i in range(8)]
    out = recent_for_prompt(history, limit=3)
    assert [t["content"] for t in out] == ["5", "6", "7"]
    assert set(out[0]) == {"role", "content"}


def test_kom_mode_enter_exit() -> None:
    store = KomModeStore()
    assert store.is_active(1, 1) is False
    store.enter(1, 1)
    assert store.is_active(1, 1) is True
    store.exit(1, 1)
    assert store.is_active(1, 1) is False


def test_render_history_text_labels_each_side() -> None:
    store = AiMemoryStore()
    store.record_exchange(1, "как дела?", "отлично", chat_id=1)
    out = render_history_text(
        store.history(1, 1), header="Диалог с Комом", you_label="Ты", bot_label="Ком"
    )
    assert out.startswith("Диалог с Комом\n\n")
    assert "Ты: как дела?" in out
    assert "Ком: отлично" in out
    assert out.endswith("\n")


def test_render_history_text_is_plain_not_html() -> None:
    """RR-6 #64: the transcript ships as ``.txt``.

    Markup would be read literally there, and model output routinely
    contains raw ``<`` — escaping it would corrupt the payload the user
    asked to take home.
    """
    history = [{"role": "assistant", "content": "use <b>bold</b> & co"}]
    out = render_history_text(history, header="H", you_label="You", bot_label="Kom")
    assert "<b>bold</b> & co" in out
    assert "&lt;" not in out
    assert "&amp;" not in out


def test_render_history_text_survives_an_empty_window() -> None:
    # The handler short-circuits on empty history, but the renderer must
    # not be the thing that makes that a hard requirement.
    assert render_history_text([], header="H", you_label="You", bot_label="Kom") == "H\n"


def test_kom_mode_eviction_bounds_store() -> None:
    store = KomModeStore(max_keys=2)
    for uid in (1, 2, 3):
        store.enter(uid)
    assert not store.is_active(1)
    assert store.is_active(2)
    assert store.is_active(3)


def test_kom_mode_is_active_refreshes_recency() -> None:
    """#1533: an ACTIVE user must outlive newer entrants.

    Before the fix ``is_active`` did not touch the LRU order, so
    recency froze at ``enter`` time and a user who had been in Ком
    mode all day was evicted by newer entrants — the cap is meant to
    shed abandoned sessions, not live ones.
    """
    store = KomModeStore(max_keys=2)
    store.enter(1)
    store.enter(2)
    assert store.is_active(1)  # touch: 1 is now the newest
    store.enter(3)  # evicts the least-recently-used, which must be 2
    assert store.is_active(1)
    assert not store.is_active(2)
    assert store.is_active(3)


def test_history_hands_out_copies_of_the_turns() -> None:
    """#1540: mutating a returned turn must not rewrite the window.

    ``history`` returned ``list(turns)`` — a new list holding the SAME
    dict objects. A caller that trimmed or relabelled a turn (a prompt
    builder capping ``content``, say) silently edited the store the
    window exists to protect, and the next call served the damage.
    """
    store = AiMemoryStore()
    store.record_exchange(1, "question", "answer", chat_id=1)

    first = store.history(1, 1)
    first[0]["content"] = "MUTATED"
    first[0]["role"] = "system"

    second = store.history(1, 1)
    assert second == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    # The two reads must not share dict identity either.
    assert second[0] is not first[0]
