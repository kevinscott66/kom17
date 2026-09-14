"""Kom (DeepSeek) persona/role modes — Cluster J L-62.

The legacy monolith (``bot.py:36953``) gave the assistant seven
selectable *modes*, each swapping the system-prompt preamble so the
same single-shot completion narrates in a different voice
(default/chat/party/help/creative/code/expert). Non-VIP users were
pinned to ``default``; VIP (and developers) could switch.

This module restores that behaviour as a small, dependency-free
value layer:

* :data:`SYSTEM_PROMPTS` — the seven preambles, byte-identical to
  legacy so answers in a given mode read the same as the old bot.
* :data:`SYSTEM_PROMPTS_EN` — English twins of the same seven (#1345).
  Legacy had no such thing: it briefed the model in Russian and then
  appended one English line asking for an English reply, so an English
  speaker got a Russian persona description with an English postscript.
* :data:`MODE_TITLES` — the user-facing labels (RU) used by the
  ``/ai`` help card and mode-switch confirmation.
* :class:`AiModeStore` — an in-process per-user mode map. Matches the
  legacy ``DeepSeekAI.user_modes`` dict, which was in-memory and reset
  on restart; keeping it in-process avoids a migration for a
  preference that is cheap to re-select. The tradeoff (mode resets on
  deploy) is identical to legacy and documented here.

VIP gating is *policy*, not stored here: :meth:`AiModeStore.resolve`
collapses a non-VIP user's selection to ``default`` at read time, so a
user who was VIP when they picked ``expert`` and later lapsed silently
falls back — exactly the legacy ``get_system_prompt`` behaviour
(``bot.py:36981``).
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock

# Verbatim from ``bot.py:36953-36961``. Every preamble is reproduced
# byte-identically so a given mode's voice matches the legacy bot.
SYSTEM_PROMPTS: dict[str, str] = {
    "default": (
        "Ты — умный помощник в Telegram-боте (Ком). Отвечай на вопросы "
        "пользователей, помогай с настройкой бота, объясняй команды, давай "
        "советы. Отвечай кратко, но информативно; используй эмодзи для "
        "дружелюбности. Если не знаешь ответа — честно скажи об этом."
    ),
    "chat": (
        "Ты Ком, свой человек в чате. Общайся естественно и неформально, с "
        "лёгким современным сленгом, поддерживай разговор и вайб тусовки. Не "
        "будь грубым, не провоцируй конфликты."
    ),
    "party": (
        "Ты Ком в режиме тусовки: общаешься живо, энергично, по-молодёжному, "
        "с аккуратным сленгом 2026 и мемным вайбом, но без оскорблений и "
        "перегибов. Поддерживай диалог как друг, предлагай темы для общения и "
        "активности в чате."
    ),
    "help": ("Ты помощник, объясняющий функции бота. Отвечай чётко, структурированно, по делу."),
    "creative": (
        "Ты креативный помощник. Генерируй идеи, пиши стихи, рассказы, помогай с "
        "творческими задачами."
    ),
    "code": ("Ты эксперт по программированию. Помогай с кодом, объясняй алгоритмы, давай примеры."),
    "expert": (
        "Ты эксперт. Дай развёрнутый, детальный ответ с примерами и пояснениями. "
        "Структурируй ответ (списки, абзацы), не упускай важные детали."
    ),
}

# #1345: the English half of the same seven personas. These are NOT ports
# of anything — legacy only ever had the Russian table above and leaned on
# a trailing "Reply in English only" line to bend the answer. That works
# most of the time and fails in the two ways a mixed-language prompt
# always fails: the model drifts back into the briefing language on long
# answers, and any wording it is told to reuse ("mode: тусовка") comes out
# in the wrong language. Describing the persona in the reader's language
# removes both.
#
# The Russian table stays byte-identical to legacy on purpose, so the
# parity claim in this module's docstring keeps meaning what it says. Keep
# the two tables keyed alike: ``test_en_prompts_cover_every_mode`` fails
# if a mode is added to one and not the other.
SYSTEM_PROMPTS_EN: dict[str, str] = {
    "default": (
        "You are a smart assistant inside a Telegram bot (Kom). Answer "
        "users' questions, help with bot setup, explain commands, give "
        "advice. Be brief but informative; use emoji to sound friendly. "
        "If you do not know the answer, say so honestly."
    ),
    "chat": (
        "You are Kom, one of the regulars in this chat. Talk naturally and "
        "informally, with light modern slang, keep the conversation and the "
        "vibe going. Do not be rude, do not pick fights."
    ),
    "party": (
        "You are Kom in party mode: lively, energetic, youthful, with "
        "careful 2026 slang and a meme vibe, but no insults and nothing "
        "over the top. Keep the conversation going like a friend, suggest "
        "topics and things to do in the chat."
    ),
    "help": (
        "You are an assistant explaining what the bot can do. Answer "
        "clearly, in a structured way, and to the point."
    ),
    "creative": (
        "You are a creative assistant. Generate ideas, write poems and "
        "stories, help with creative tasks."
    ),
    "code": (
        "You are a programming expert. Help with code, explain algorithms, "
        "and give runnable examples."
    ),
    "expert": (
        "You are an expert. Give a thorough, detailed answer with examples "
        "and explanations. Structure it (lists, paragraphs) and do not skip "
        "the important details."
    ),
}

# Default mode every user starts in (and the only one non-VIP users get).
DEFAULT_MODE = "default"

# User-facing labels (RU) — byte-identical to legacy ``AI_MODE_TITLES``
# (``bot.py:38399``). The mode-switch confirmation and the ``/ai`` help
# card render these.
MODE_TITLES: dict[str, str] = {
    "default": "💬 Обычный",
    "chat": "🤝 Дружеский",
    "party": "🔥 Тусовка 17",
    "help": "❓ Помощник",
    "creative": "🎨 Креативный",
    "code": "💻 Код",
    "expert": "📚 Эксперт",
}

# Short hint word per mode, embedded into the dynamic context block so
# the model keeps the chosen voice even on «ком»/«ии»-prefixed prompts.
# Byte-identical to legacy ``mode_hint`` map (``bot.py:37113``).
MODE_HINTS: dict[str, str] = {
    "party": "тусовка",
    "expert": "эксперт",
    "chat": "дружеский",
    "help": "помощник",
    "creative": "креативный",
    "code": "код",
}

# #1345: the same hint words for an English reader. Each value is the
# canonical mode key, which is deliberate rather than lazy: every key is
# already an entry of ``_MODE_ALIASES``, so the ``mode_hint`` ->
# ``resolve_mode_token`` round-trip that ``MODE_HINTS`` has to satisfy
# (#1074) is closed in English for free — whatever the prompt shows the
# user, the user can type back after ``/mode``.
MODE_HINTS_EN: dict[str, str] = {
    "party": "party",
    "expert": "expert",
    "chat": "chat",
    "help": "help",
    "creative": "creative",
    "code": "code",
}


def mode_hint(mode: str, lang: str = "ru") -> str:
    """Hint word for the dynamic context block; defaults to «обычный».

    ``lang`` defaults to Russian because that is what every call site
    meant before #1345, and because the hint words are half of the
    ``/mode`` alias contract pinned in the unit tests.
    """
    if lang == "ru":
        return MODE_HINTS.get(mode, "обычный")
    return MODE_HINTS_EN.get(mode, "default")


def is_valid_mode(mode: str) -> bool:
    """Whether ``mode`` is one of the seven known personas."""
    return mode in SYSTEM_PROMPTS


def system_prompt_for(mode: str, lang: str) -> str:
    """Persona preamble for ``mode``, in the reader's language (#1345).

    Anything other than ``ru`` gets the English table. An unknown mode
    falls back to :data:`DEFAULT_MODE` in the same language rather than
    raising: this runs inside prompt assembly, where a stale stored key
    must degrade to the plain persona, not lose the answer.
    """
    table = SYSTEM_PROMPTS if lang == "ru" else SYSTEM_PROMPTS_EN
    return table.get(mode, table[DEFAULT_MODE])


# Localised display tokens a user is likely to type after ``/mode``. The
# ``/mode`` list shows ``MODE_TITLES`` (e.g. "📚 Эксперт"), so a user
# naturally types the Russian label ("эксперт") rather than the canonical
# English key ("expert") — but the parser previously only accepted the
# English keys, so "/mode эксперт" answered "Unknown style" even though
# the list advertised "Эксперт". This map accepts the Russian display
# words (emoji/case stripped), the canonical English keys, and the hint
# words already used in the dynamic context. Keys are lowercased, emoji-
# and space-trimmed; values are canonical mode keys in ``SYSTEM_PROMPTS``.
_MODE_ALIASES: dict[str, str] = {
    # Canonical English keys (identity) — kept explicit so the resolver is
    # a single lookup and doesn't special-case ``is_valid_mode``.
    "default": "default",
    "chat": "chat",
    "party": "party",
    "help": "help",
    "creative": "creative",
    "code": "code",
    "expert": "expert",
    # Russian display words from MODE_TITLES (without emoji).
    "обычный": "default",
    "дружеский": "chat",
    "тусовка": "party",
    "тусовка 17": "party",
    "помощник": "help",
    "креативный": "creative",
    "код": "code",
    "эксперт": "expert",
    # A couple of natural synonyms users reach for.
    "обычная": "default",
    "дружелюбный": "chat",
    "english": "default",
}


def _strip_mode_token(raw: str) -> str:
    """Lowercase, drop emoji/punctuation, collapse spaces for alias lookup."""
    low = raw.strip().lower()
    # Keep Cyrillic/Latin letters, digits and spaces; everything else
    # (emoji, ``📚``, punctuation) is dropped so "📚 эксперт" → "эксперт".
    cleaned = "".join(c for c in low if c.isalnum() or c.isspace())
    return " ".join(cleaned.split())


def resolve_mode_token(raw: str) -> str | None:
    """Resolve a user-typed ``/mode`` argument to a canonical mode key.

    Accepts the canonical English keys (``expert``), the localised display
    names shown in the ``/mode`` list (``эксперт``, ``креативный``,
    emoji-and-case-insensitive), and the dynamic-context hint words. Returns
    the canonical key (a member of :data:`SYSTEM_PROMPTS`) or ``None`` when
    the token matches no known mode.
    """
    token = _strip_mode_token(raw)
    if not token:
        return None
    canonical = _MODE_ALIASES.get(token)
    if canonical is not None:
        return canonical
    # #1074: this loop is currently unreachable, and the comment that used
    # to justify it argued from a typo — it compared "дружеский" to
    # "дружеский" and called them different. Every value in
    # :data:`MODE_HINTS` is already a key of ``_MODE_ALIASES`` resolving to
    # the same mode, so the lookup above always wins; that redundancy is
    # pinned by ``test_every_mode_hint_is_already_an_alias``.
    #
    # Kept anyway, as a backstop rather than a path: a hint word added to
    # :data:`MODE_HINTS` without a matching alias would otherwise be a mode
    # the bot advertises in its own prompt and then refuses to accept from
    # the user. The test fails first and names the omission; this catches
    # it if the test is ever relaxed.
    for mode, hint in MODE_HINTS.items():
        if hint == token:
            return mode
    return None


#: LRU ceiling on :class:`AiModeStore`. The map used to be an unbounded
#: ``dict``: one entry per user who ever switched mode, never removed, in
#: a process that runs for months. Small per entry, but monotone — the
#: same shape as the middleware caches that were capped earlier.
#:
#: Eviction is safe *because* of the VIP gate: a lapsed selection reads
#: back as ``default``, which is exactly what ``resolve`` already returns
#: for a non-VIP user and what a restart already produces. 10 000 mode
#: switchers is far more than the VIP population, so in practice nothing
#: is ever evicted — the cap is the guarantee, not the mechanism.
_MAX_TRACKED_USERS = 10_000


class AiModeStore:
    """In-process per-user Kom mode map (legacy ``DeepSeekAI.user_modes``).

    LRU-bounded at :data:`_MAX_TRACKED_USERS`; a process restart clears
    it, matching legacy. Thread-safe because the webhook dispatcher may
    touch the same user from concurrent updates.
    """

    def __init__(self) -> None:
        self._modes: OrderedDict[int, str] = OrderedDict()
        self._lock = Lock()

    def get(self, user_id: int) -> str:
        """Raw stored mode (ignores VIP gating). Defaults to ``default``.

        Reads count as use: a user who keeps talking to Kom in ``expert``
        must not be evicted in favour of someone who switched mode once
        and left. ``resolve`` runs on every AI reply, so this is the
        signal that keeps the LRU order meaningful.
        """
        with self._lock:
            mode = self._modes.get(user_id)
            if mode is None:
                return DEFAULT_MODE
            self._modes.move_to_end(user_id)
            return mode

    def set(self, user_id: int, mode: str) -> bool:
        """Store ``mode`` for ``user_id``; ``False`` if mode is unknown."""
        if not is_valid_mode(mode):
            return False
        with self._lock:
            self._modes[user_id] = mode
            self._modes.move_to_end(user_id)
            while len(self._modes) > _MAX_TRACKED_USERS:
                self._modes.popitem(last=False)
        return True

    def resolve(self, user_id: int, *, is_vip: bool) -> str:
        """Effective mode after VIP gating.

        Non-VIP users are collapsed to ``default`` exactly like legacy
        ``get_system_prompt`` (``bot.py:36984``). ``is_vip`` should be
        ``True`` for developers too (the caller folds dev-ids into the
        VIP test, matching ``_is_vip_for_ai``).
        """
        mode = self.get(user_id)
        if not is_vip:
            return DEFAULT_MODE
        return mode

    def system_prompt(self, user_id: int, *, is_vip: bool, lang: str = "ru") -> str:
        """System-prompt preamble for the user's effective mode.

        ``lang`` selects the preamble table (#1345) and defaults to
        Russian so the pre-existing call shape keeps its meaning.
        """
        return system_prompt_for(self.resolve(user_id, is_vip=is_vip), lang)
