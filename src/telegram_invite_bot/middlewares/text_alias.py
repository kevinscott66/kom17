"""Plain-text command-alias router (legacy ``process_text_shortcut_command``).

The telebot monolith answered a large set of bare/prefixed Russian words
(``меню``, ``баланс``, ``топ``, ``профиль`` …) as shortcuts for the
corresponding ``/slash`` commands. Deleting the legacy bridge dropped
that whole surface silently. This outer middleware restores it WITHOUT
duplicating any handler logic: when a message matches the legacy alias
gating it rewrites ``message.text`` to the canonical ``/command args``
form and lets the normal command routers (with all their own
middlewares and dependency injection) dispatch it.

Gating mirrors legacy exactly to avoid stealing ordinary group chatter:

* **Private chat** — any bare alias word at the start of the message
  resolves (a DM to the bot is unambiguously a command).
* **Group / supergroup** — an alias only resolves when the message is
  prefixed with one of :data:`GROUP_PREFIXES` OR is one of the small
  bare whitelist phrases legacy allowed un-prefixed,
  :data:`BARE_GROUP_PHRASES`. Everything else in a group is left
  untouched.

Both of those constants are public because the website's command index
prints them (see
:func:`telegram_invite_bot.cms.guide_site.command_index.render_plain_note_html`);
listing them in prose here as well would be a third copy of the same
facts, and prose is the copy that goes stale.

Only aliases whose target command is already PORTED are mapped — a word
pointing at an unported command resolves to ``None`` and the message
passes through unchanged.

One surface is bigger than a word list: a weather question is a whole
sentence («какая погода завтра в Казани»), so when no alias word
matches the phrase is tested with :func:`is_weather_query` and, on a
hit, forwarded to ``/weather`` intact (RR-6 #68). The same gating
applies — in a group it still takes a prefix.

The ``ком ``/``ии `` AI prefixes are deliberately NOT claimed here: they
are owned by :func:`telegram_invite_bot.handlers.ai.extract_ai_direct_question`
(the A-05 plain-text AI trigger), so this middleware leaves them for the
AI handler downstream.

Registered as the first *rewriting* root message middleware — after
:class:`LanguageMiddleware` (mounted in ``build_main_router``), which
only resolves ``lang`` and rewrites nothing, and before
``MessageActivityMiddleware`` — so a rewritten alias is treated as the
command it stands for, i.e. excluded from passive message counting /
coin rewards, matching legacy where an intercepted shortcut was never
counted as chatter.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from aiogram import BaseMiddleware
from aiogram.types import Message

from telegram_invite_bot.core.chat_types import GROUP_TYPE_NAMES
from telegram_invite_bot.core.weather_query import is_weather_query

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject


# Trigger word (lowercased) → canonical command name (no leading slash).
# ONLY commands that are already ported in the new pipeline appear here;
# an unmapped word resolves to ``None`` and the message is untouched.
_ALIAS_MAP: dict[str, str] = {
    # help / menu
    "меню": "help",
    "menu": "help",
    "команды": "help",
    "команда": "help",
    "commands": "help",
    "help": "help",
    # profile (self card) + the «кто я» whitelist token
    "профиль": "profile",
    "проф": "profile",
    "profile": "profile",
    "карточка": "profile",
    "ктоя": "profile",
    "whoami": "profile",
    # heartbeat
    "пинг": "ping",
    "ping": "ping",
    "бот": "botcheck",
    "bot": "botcheck",
    "ботпроверка": "botcheck",
    # economy
    "баланс": "balance",
    "бал": "balance",
    "деньги": "balance",
    "монеты": "balance",
    "коины": "balance",
    "кошелёк": "balance",
    "кошелек": "balance",
    "balance": "balance",
    "wallet": "balance",
    # leaderboard
    "топ": "top",
    "top": "top",
    "лидеры": "top",
    "лидерборд": "top",
    "leaderboard": "top",
    # daily bonus
    "дейли": "daily",
    "бонус": "daily",
    "daily": "daily",
    "ежедневный": "daily",
    # weather (rest of payload = city)
    "погода": "weather",
    "погоду": "weather",
    "weather": "weather",
    # saved city (rest of payload = city name), RR-6 #74. Legacy mapped
    # the same word at bot.py:43336. Only the single-word form is
    # listed because ``_resolve`` keys on ``parts[0]`` — legacy's
    # multi-word "мой город" / "из какого города" entries could never
    # match here, so adding them would be decoration.
    "город": "city",
    "city": "city",
    # time
    "время": "time",
    "time": "time",
    "мск": "time",
    # jokes 18+
    "шутка18": "joke18",
    "joke18": "joke18",
    "анекдот18": "joke18",
    # games
    "кубик": "roll",
    "roll": "roll",
    "монетка": "flip",
    "flip": "flip",
    "кости": "dice",
    "dice": "dice",
    "дуэль": "duel",
    "duel": "duel",
    "поединок": "duel",
    # calculator (rest = expression)
    "калькулятор": "calc",
    "calc": "calc",
    "посчитай": "calc",
    # stats
    "стата": "stats",
    "статистика": "stats",
    "stats": "stats",
    # shop / inventory
    "магазин": "shop",
    "магаз": "shop",
    "shop": "shop",
    "инвентарь": "inventory",
    "инв": "inventory",
    "inventory": "inventory",
    "рюкзак": "inventory",
    # marriage / relations
    "брак": "marry",
    "жениться": "marry",
    "marry": "marry",
    "развод": "divorce",
    "divorce": "divorce",
    "расстаться": "breakup",
    "breakup": "breakup",
    "браки": "marriages",
    "пары": "marriages",
    "marriages": "marriages",
    "отношения": "relationship",
    "relationship": "relationship",
    # support / faq
    "поддержка": "support",
    "support": "support",
    "тикет": "support",
    "саппорт": "support",
    "faq": "faq",
    "чаво": "faq",
    "вопросы": "faq",
    # vouchers
    "чек": "check",
    "check": "check",
    # group info (CMD-2): /chatinfo == /chatstats. ``_fold_phrase``
    # normalises «чат инфа» / «чат инфо» / «chat info» → ``чатинфа``.
    # The bare latin spelling is listed as its own key so the public
    # index prints an English trigger under this command too — the
    # folder alone would leave the site's /chatinfo row Cyrillic-only.
    "чатинфа": "chatinfo",
    "chatinfo": "chatinfo",
    # ── I18N-3: bilingual triggers for the commands ported this session
    # (CMD-1..4). Every entry has a ru AND an en form so the plain-text
    # surface works in both languages, same as the rest of this map.
    # quote
    "цитата": "quote",
    "quote": "quote",
    # SFW joke — distinct from шутка18 → joke18 above
    "шутка": "joke",
    "анекдот": "joke",
    "joke": "joke",
    # crypto rates
    "крипта": "crypto",
    "криптовалюта": "crypto",
    "crypto": "crypto",
    # currency listing
    "валюта": "currency",
    "currency": "currency",
    # COM↔currency rate (rest of payload = currency code)
    "курс": "rate",
    "rate": "rate",
    # convert (rest = amount + code)
    "конвертация": "convert",
    "конверт": "convert",
    "convert": "convert",
    # multi-day weather forecast (rest = city)
    "прогноз": "forecast",
    "forecast": "forecast",
    # top active members (rest = days)
    "активность": "topactive",
    "topactive": "topactive",
    # russian roulette (rest = bet) — group-only, gated in-handler
    "рулетка": "roulette",
    "roulette": "roulette",
    # achievements card
    "достижения": "achievements",
    "achievements": "achievements",
}

#: Prefixes that make ANY alias fire, in any chat type. In a group this
#: is the only way to reach an alias that is not on
#: :data:`BARE_GROUP_PHRASES`. ``"бот "`` keeps its trailing space —
#: that space is what separates «бот баланс» (a command) from a message
#: that merely starts with the letters «бот». ``"bot "`` is the same
#: door for an English speaker: every other trigger in this module has
#: had a Latin spelling since I18N-3, and this prefix was the last one
#: that only opened for a Russian keyboard (#176).
GROUP_PREFIXES: Final[tuple[str, ...]] = (".", "?", "!", "бот ", "bot ")

#: The phrases a group accepts with no prefix at all, in the order the
#: public guide lists them. Kept beside the matcher below so the two are
#: read and changed together, and pinned from both sides by
#: ``tests/e2e/handlers/test_text_alias``: every entry here really does
#: resolve un-prefixed in a group, so the page carries no fiction, and
#: every literal the group branch below accepts is either listed here
#: or an explicitly excused spelling variant of something that is, so
#: the bot honours no trigger the page keeps quiet about. «команда»
#: sat on the wrong side of that second rule until it was written.
BARE_GROUP_PHRASES: Final[tuple[str, ...]] = (
    "меню",
    "menu",
    "команды",
    "команда",
    "commands",
    "кто я",
    "whoami",
    "me",
    "чат инфа",
    "chat info",
    "пинг",
    "ping",
    "бот",
    "bot",
)

#: How a trigger is spelled for a reader, where that differs from the
#: :data:`_ALIAS_MAP` key. The map is keyed on the *normalised* token
#: (``_extract_payload`` folds «кто я» to ``ктоя`` before the lookup),
#: so the site has to un-fold it or it advertises a word nobody types.
_DISPLAY_FORM: Final[dict[str, str]] = {
    "ктоя": "кто я",
    "чатинфа": "чат инфа",
}


def plain_triggers_by_command() -> dict[str, tuple[str, ...]]:
    """Canonical command name → the plain-text words that reach it.

    The public command index prints these under each ``/command`` so a
    reader learns that «баланс» and ``/balance`` are the same thing.
    Inverted from :data:`_ALIAS_MAP` on demand rather than kept as a
    second, hand-written table — for exactly the reason the index
    itself is generated: two lists of the same facts drift apart, and
    it is always the copy on the website that goes stale.

    ``ё``-less spellings are folded away (``кошелёк``/``кошелек`` are
    one trigger to a reader, two keys to the matcher).
    """
    out: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for word, command in _ALIAS_MAP.items():
        display = _DISPLAY_FORM.get(word, word)
        key = display.replace("ё", "е")
        if key in seen.setdefault(command, set()):
            continue
        seen[command].add(key)
        out.setdefault(command, []).append(display)
    return {command: tuple(words) for command, words in out.items()}


def _normalize(text: str) -> str:
    """Collapse punctuation/emoji to spaces — legacy ``normalized`` form."""
    return re.sub(r"[^\wа-яё]+", " ", text, flags=re.IGNORECASE).strip()


def _fold_phrase(text: str) -> str | None:
    """Fold a whole multi-word trigger phrase into its single map key.

    ``_resolve`` looks the first *word* up in :data:`_ALIAS_MAP`, so a
    two-word trigger can never match there — «кто я» has to become
    ``ктоя`` before the lookup. Only an exact whole-phrase match folds:
    «кто я вообще такой» is a sentence, not a command, and falls
    through untouched.

    Kept as its own function because the fold has to happen on BOTH
    paths into :func:`_extract_payload`. It used to run only on the
    un-prefixed one, which made «бот кто я» and «!чат инфа» resolve to
    nothing — the two spellings the site advertises most (a prefix
    makes any trigger fire; these phrases need no prefix) did not work
    when a reader combined them.
    """
    normalized = _normalize(text.lower())
    if normalized in ("кто я", "ктоя", "whoami", "me"):
        return "ктоя"
    if normalized in ("чат инфа", "чатинфа", "чат инфо", "чатинфо", "chat info", "chatinfo"):
        return "чатинфа"  # → /chatinfo, ported in CMD-2
    return None


def _extract_payload(raw: str, *, is_group: bool) -> str | None:
    """Apply legacy gating and return the un-prefixed payload, or ``None``.

    ``raw`` is the stripped original text (leading ``/`` already excluded
    by the caller). The ``ком ``/``ии `` AI prefixes are intentionally not
    handled here.
    """
    lower = raw.lower()

    # Prefixed invocation — allowed in any chat. The trailing ``.strip()``
    # is what lets one branch cover both «!баланс» and «! баланс».
    for prefix in GROUP_PREFIXES:
        if lower.startswith(prefix):
            payload = raw[len(prefix) :].strip()
            return _fold_phrase(payload) or payload

    # Whole-phrase fold, un-prefixed, in any chat. Not a multi-word
    # rule despite the shape of the examples: six of the ten spellings
    # ``_fold_phrase`` accepts are single words (``ктоя``, ``whoami``,
    # ``me``, ``чатинфа``, ``чатинфо``, ``chatinfo``). Nor is this the
    # only place a phrase folds — the prefixed branch above folds its
    # own payload on the way out, which is what makes «бот кто я» work.
    folded = _fold_phrase(raw)
    if folded is not None:
        return folded

    normalized = _normalize(lower)

    if is_group:
        # Bare (un-prefixed) words only fire for the tiny legacy whitelist.
        if lower in ("меню", "команды", "команда", "commands", "menu"):
            return lower
        if normalized in ("пинг", "ping"):
            return "пинг"
        if normalized in ("бот", "bot"):
            return "ботпроверка"
        return None

    # Private DM — any bare alias word resolves.
    return raw


# The AI prefixes, matched against a payload rather than the raw text
# so a prefixed «.ком какая погода» is left to the AI handler too.
_AI_PREFIX_RE = re.compile(r"^\s*(ком|ии|ai)\b", re.IGNORECASE)


def _resolve_weather_phrase(payload: str) -> str | None:
    """Free-form weather question → ``/weather <phrase>`` (RR-6 #68).

    Legacy's ``maybe_send_weather_reply`` answered whole sentences —
    «какая погода завтра в Казани», «что по погоде» — not just the bare
    ``погода <город>`` form the alias table covers. The phrase is handed
    to ``/weather`` verbatim; the handler owns both the city and the
    period parsing, so there is exactly one place where "what did the
    user actually ask for" is decided.

    ``ком ``/``ии `` questions stay with the AI handler even when they
    mention weather: Kom answers those in character (and gets the live
    forecast injected into its prompt anyway), so intercepting them here
    would replace a conversation with a card.
    """
    # A payload that is already a slash command (``.  /weather``) has
    # nothing to gain from a second rewrite; let it dispatch as typed.
    if payload.startswith("/") or _AI_PREFIX_RE.match(payload):
        return None
    if not is_weather_query(payload):
        return None
    return f"/weather {payload}".strip()


def _resolve(raw: str, chat_type: str | None) -> str | None:
    """Return the canonical ``/command args`` string, or ``None``."""
    payload = _extract_payload(raw, is_group=chat_type in GROUP_TYPE_NAMES)
    if not payload:
        return None
    parts = payload.split(maxsplit=1)
    word = parts[0].lower()
    command = _ALIAS_MAP.get(word)
    if command is None:
        # No alias word — the phrase may still be a weather question.
        # Note this inherits ``_extract_payload``'s gating, so in a group
        # it only fires for a prefixed message: legacy hijacked *any*
        # group message containing «погода», which is how a bot ends up
        # answering a conversation it wasn't part of.
        return _resolve_weather_phrase(payload)
    rest = parts[1].strip() if len(parts) > 1 else ""
    return f"/{command} {rest}".strip()


class TextAliasMiddleware(BaseMiddleware):
    """Rewrite legacy plain-text shortcuts into canonical ``/command`` form."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            rewritten = await self._maybe_rewrite(event, data)
            if rewritten is not None:
                return await handler(rewritten, data)
        return await handler(event, data)

    async def _maybe_rewrite(self, message: Message, data: dict[str, Any]) -> Message | None:
        if message.from_user is None or message.from_user.is_bot:
            return None
        text = message.text
        if not text:
            return None
        raw = text.strip()
        if not raw or raw.startswith("/"):
            return None

        # Never hijack an in-progress FSM text step (support message,
        # withdraw amount, …) — those only run while a state is set.
        #
        # #1039: the answer is normally already in ``data``. aiogram's
        # ``FSMContextMiddleware`` is an update-level OUTER middleware and
        # resolves ``raw_state`` before any router middleware runs
        # (``aiogram/fsm/middleware.py:42``), so the old unconditional
        # ``await state.get_state()`` paid a storage round-trip on every
        # plain text message — and under ``FSM_BACKEND=sqlite``, which is
        # what production runs, that is a database read per message. The
        # ``state`` path stays as the fallback: a hand-built ``data``
        # (tests, a bare dispatcher) can carry the context without the
        # resolved key, and silently skipping the guard there would let an
        # alias hijack an FSM step.
        if "raw_state" in data:
            in_fsm_step = data["raw_state"] is not None
        else:
            state = data.get("state")
            in_fsm_step = state is not None and await state.get_state() is not None
        if in_fsm_step:
            return None

        canonical = _resolve(raw, message.chat.type)
        if canonical is None:
            return None
        return message.model_copy(update={"text": canonical, "entities": None})
