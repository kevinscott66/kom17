"""In-process conversation memory for Kom — Cluster J L-63.

The legacy assistant kept a per-(user, chat) rolling window of the last
10 messages on the assistant instance (``AIAssistant.histories``,
``bot.py:36949``, guarded by ``self.history_lock`` at ``bot.py:36950``)
so the single-shot DeepSeek completion could carry a short multi-turn
context. ``/reset`` cleared it; a process restart wiped everything.

This module restores that experience as a **bounded, thread-safe,
in-process store**:

* Keyed by ``(user_id, chat_id)`` — in a private chat ``chat_id`` is the
  user's own id, in a group it is the group id, so the same user has
  independent histories per group (legacy ``_history_key``).
* Each key holds at most :data:`_MAX_TURNS` role/content pairs; appending
  past the cap drops the oldest (legacy kept the last 10).
* A global cap on the number of live keys (:data:`_MAX_KEYS`) and a
  ceiling on the length of one stored turn (:data:`_MAX_TURN_CHARS`)
  together bound memory in a long-running webhook process — the legacy
  dict grew without bound in both directions, which we fix here.

Tradeoff (documented, matches legacy intent): memory is **not
persisted**. A deploy or crash clears every conversation. We choose this
over a new DB table because (a) it matches the legacy in-memory
``context_manager`` semantics users already expect, (b) it avoids a
migration on the ``users``/``activity`` DBs, and (c) conversation
context is low-value to persist — a dropped window just means the next
question starts fresh, which is the same as typing ``/reset``.

:class:`KomModeStore` (L-76) lives here too: a per-(user, chat) flag
marking that the user pressed «Войти в Ком», so every subsequent plain
private message routes to the model until they exit. Same in-process,
restart-clearing posture as the history store.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Legacy kept the last 10 messages per (user, chat) (``bot.py:37028``).
_MAX_TURNS = 10

# Hard cap on live conversation keys. Oldest-touched key is evicted
# first (LRU) — a conversation nobody has touched in a while is the
# cheapest to forget.
#
# #1623: this cap on its own does NOT bound the store, and the comment
# that used to stand here claimed it held it "well under a megabyte".
# Measured with tracemalloc on a full store (2000 keys x 10 turns):
# even the 50-character turns that claim assumed cost 6.1 MiB, and a
# realistic expert answer — _EXPERT_MAX_TOKENS is 2000 in
# ``handlers/ai.py``, so roughly 6000 characters — costs 65.2 MiB in
# ASCII and 125.7 MiB in Cyrillic, which is the audience this bot
# actually has. Prod runs under a tight per-process memory ceiling.
# This cap bounds how many windows exist; _MAX_TURN_CHARS bounds what
# one turn may weigh. Only the two together bound the store.
_MAX_KEYS = 2000

# Longest stored turn. :meth:`AiMemoryStore.add` cuts past this before
# appending, so the whole store is bounded by _MAX_KEYS x _MAX_TURNS x
# this. Measured the same way, the cut takes the Cyrillic worst case
# above from 125.7 MiB to 30.2 MiB (17.4 MiB in ASCII).
#
# The cut shows in two places and is acceptable in both: the export
# button renders the stored window verbatim, and :func:`recent_for_prompt`
# feeds the last turns back to the model. A trailing ellipsis stands in
# for what was dropped, so neither reads as a complete answer that
# simply stops.
_MAX_TURN_CHARS = 1000

# A single stored turn: ``role`` is ``"user"`` or ``"assistant"``.
Turn = dict[str, str]


def _key(user_id: int, chat_id: int | None) -> tuple[int, int]:
    """History key — private chat folds ``chat_id`` onto ``user_id``."""
    cid = chat_id if chat_id is not None else user_id
    return (user_id, cid)


def _clip(content: str, limit: int) -> str:
    """One turn's text, cut to ``limit`` characters (#1623).

    The ellipsis replaces the last kept character instead of being
    appended to it, so the result is never longer than ``limit``: the
    bound quoted next to :data:`_MAX_TURN_CHARS` has to be arithmetic,
    not approximate. ``limit`` is a positive character count — the
    store's own ceiling or a smaller one a test passes in.
    """
    if len(content) <= limit:
        return content
    return content[: limit - 1] + "\u2026"


class AiMemoryStore:
    """Bounded, thread-safe per-(user, chat) rolling message window."""

    def __init__(
        self,
        *,
        max_turns: int = _MAX_TURNS,
        max_keys: int = _MAX_KEYS,
        max_chars: int = _MAX_TURN_CHARS,
    ) -> None:
        self._max_turns = max_turns
        self._max_keys = max_keys
        self._max_chars = max_chars
        # OrderedDict gives O(1) LRU eviction: move_to_end on touch,
        # popitem(last=False) to evict the least-recently-used key.
        self._store: OrderedDict[tuple[int, int], list[Turn]] = OrderedDict()
        self._lock = Lock()

    def history(self, user_id: int, chat_id: int | None = None) -> list[Turn]:
        """Copy of the current window for this (user, chat). Newest last."""
        k = _key(user_id, chat_id)
        with self._lock:
            turns = self._store.get(k)
            if turns is None:
                return []
            self._store.move_to_end(k)
            # #1540: copy the dicts too. ``list(turns)`` handed callers
            # the STORED turn objects, so a caller that edited one (a
            # prompt builder trimming ``content``, say) silently rewrote
            # the window this store exists to protect.
            return [dict(turn) for turn in turns]

    def add(self, user_id: int, role: str, content: str, chat_id: int | None = None) -> None:
        """Append one turn, trimming it, the window and the live keys."""
        k = _key(user_id, chat_id)
        with self._lock:
            turns = self._store.get(k)
            if turns is None:
                turns = []
                self._store[k] = turns
            turns.append({"role": role, "content": _clip(content, self._max_chars)})
            if len(turns) > self._max_turns:
                del turns[: len(turns) - self._max_turns]
            self._store.move_to_end(k)
            while len(self._store) > self._max_keys:
                self._store.popitem(last=False)

    def record_exchange(
        self,
        user_id: int,
        question: str,
        answer: str,
        chat_id: int | None = None,
    ) -> None:
        """Append a user→assistant pair (the common post-answer path)."""
        self.add(user_id, "user", question, chat_id)
        self.add(user_id, "assistant", answer, chat_id)

    def clear(self, user_id: int, chat_id: int | None = None) -> None:
        """Forget the window for this (user, chat) — backs ``/reset``."""
        k = _key(user_id, chat_id)
        with self._lock:
            self._store.pop(k, None)

    def size(self, user_id: int, chat_id: int | None = None) -> int:
        """Number of stored turns for this (user, chat)."""
        k = _key(user_id, chat_id)
        with self._lock:
            turns = self._store.get(k)
            return len(turns) if turns else 0


def recent_for_prompt(history: Sequence[Turn], *, limit: int = 5) -> list[Turn]:
    """Last ``limit`` turns as clean ``{role, content}`` dicts for the API.

    Legacy fed the last 5 turns into the messages array
    (``bot.py:37205``); we keep that bound so the prompt stays small even
    though the store retains 10.
    """
    tail = list(history)[-limit:]
    return [{"role": t["role"], "content": t["content"]} for t in tail]


def render_history_text(
    history: Sequence[Turn],
    *,
    header: str,
    you_label: str,
    bot_label: str,
) -> str:
    """The conversation window as a plain-text transcript (RR-6 #64).

    Backs the 📤 Export button. Deliberately **plain text**, not HTML: the
    result is written into a ``.txt`` attachment, where markup would be
    read literally, and model output routinely contains raw ``<`` — the
    one thing that must not need escaping here is the payload itself.

    Labels are passed in rather than looked up so this stays a pure
    function the unit tests can drive in either locale.
    """
    lines = [header, ""]
    for turn in history:
        label = you_label if turn.get("role") == "user" else bot_label
        lines.append(f"{label}: {turn.get('content', '')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class KomModeStore:
    """Per-(user, chat) «Войти в Ком» flag — Cluster J L-76.

    When set, every plain private message from the user routes to the
    model until they exit. In-process and restart-clearing, like the
    history store — pressing «Войти в Ком» again after a deploy is cheap.
    Bounded by an LRU cap so an abandoned session set can't grow without
    limit.
    """

    def __init__(self, *, max_keys: int = _MAX_KEYS) -> None:
        self._active: OrderedDict[tuple[int, int], bool] = OrderedDict()
        self._lock = Lock()
        self._max_keys = max_keys

    def enter(self, user_id: int, chat_id: int | None = None) -> None:
        k = _key(user_id, chat_id)
        with self._lock:
            self._active[k] = True
            self._active.move_to_end(k)
            while len(self._active) > self._max_keys:
                self._active.popitem(last=False)

    def exit(self, user_id: int, chat_id: int | None = None) -> None:
        k = _key(user_id, chat_id)
        with self._lock:
            self._active.pop(k, None)

    def is_active(self, user_id: int, chat_id: int | None = None) -> bool:
        k = _key(user_id, chat_id)
        with self._lock:
            if k not in self._active:
                return False
            # #1533: touch on READ, not only in :meth:`enter`. Without
            # this the LRU order froze at entry time, so a user who had
            # been in Ком mode all day counted as older than 2000 newer
            # entrants and got silently dropped mid-conversation. The
            # cap exists to shed ABANDONED sessions, and only a read
            # touch tells an abandoned session from an old live one —
            # :meth:`AiMemoryStore.history` already does exactly this.
            self._active.move_to_end(k)
            return True
