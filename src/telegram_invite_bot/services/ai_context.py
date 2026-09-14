"""Dynamic system-context builder for Kom — Cluster J L-64/L-66/L-67.

The legacy AI *simulated agency* by pre-computing real-world facts in
Python and injecting them into the system prompt, so the single-shot
DeepSeek completion narrated true values instead of hallucinating
(``bot.py:37109-37199``). This module restores that injected-context
experience for the new async stack, reusing the new
:class:`~telegram_invite_bot.services.weather_service.WeatherService`
and timezone helpers — it never calls DeepSeek for these facts.

What it injects (all as extra ``[...]`` blocks appended to the system
prompt):

* **L-66 local time** — current Moscow time + (when the question names a
  city, or the user has a stored timezone) the local time there, so
  "сейчас в …" answers are real.
* **L-66 weather** — when the question is a weather intent, the current
  conditions for the named/stored city via ``WeatherService`` (no
  DeepSeek call), embedded so the model reports the real temperature.
* **L-66 dice** — on "брось кубик"/"roll a die" a real 1-6 roll is
  pre-computed and injected so the narrated value is the value.
* **L-64 group context** — the group title plus, when available, a short
  recent-message window (or just the replied-to text), so group answers
  are grounded in the conversation.
* **L-67 reply target** — when the user replied to someone, an explicit
  instruction naming who to address.

Intent detectors (:func:`is_time_query`, :func:`is_weather_query`,
:func:`detect_dice`) are byte-compatible ports of the legacy regexes
(``bot.py:37634-37770``) so the same phrasings trigger the same
injection — with one deliberate exception, the English arm of
:func:`detect_dice`, documented there.

Timezone note: the Moscow/local-time strings built here are for
LLM-prompt *display only* — they exist so the model can narrate a
human-readable wall-clock time and never feed any domain logic. All
domain TTL/ledger timestamps elsewhere in the pipeline remain naive UTC
(see :func:`telegram_invite_bot.utils.time.db_now`); do not reuse these
display strings for storage or comparisons.
"""

from __future__ import annotations

import html as _html
import random
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.core.weather_query import (
    extract_city_from_time_query as _extract_city_from_time_query,
)
from telegram_invite_bot.core.weather_query import (
    is_weather_query,
    nominalize_ru_city,
    normalize_weather_location,
)
from telegram_invite_bot.services.weather_service import wmo_label
from telegram_invite_bot.utils.time import format_local_time

# Re-exported for the callers (and tests) that have always imported these
# from here. The parsers themselves moved to ``core.weather_query`` when
# RR-6 #68/#69 gave them callers outside the AI path; see that module.
__all__ = [
    "AiContextBuilder",
    "GroupContext",
    "detect_dice",
    "is_time_query",
    "is_weather_query",
    "markdown_to_html",
    "moscow_time_parts",
    "nominalize_ru_city",
    "normalize_weather_location",
]

if TYPE_CHECKING:
    from telegram_invite_bot.services.weather_service import WeatherService

log = logger.bind(component="services.ai_context")


def moscow_time_parts(lang: str = "ru") -> tuple[str, str, str]:
    """``(HH:MM:SS, DD.MM.YYYY, weekday)`` for Moscow. Legacy parity.

    ``lang`` picks the weekday vocabulary and nothing else (#1345). It is
    delegated to :func:`~telegram_invite_bot.utils.time.format_local_time`
    rather than kept as a second weekday table here, so the two surfaces
    cannot drift on how a day is spelled.
    """
    return format_local_time("Europe/Moscow", lang)


def is_time_query(text: str | None) -> bool:
    """Whether the text asks for the current time. Port of ``bot.py:37756``."""
    if not text:
        return False
    low = text.strip().lower()
    patterns = (
        r"\bсколько\s+сейчас\s+времени\b",
        r"\bсколько\s+времени?\b",
        r"\bкоторый\s+час\b",
        r"^\s*время\s*\??\s*$",
        r"\bwhat\s+time\s+is\s+it\b",
        r"\bвремени?\s+в\s+\w+",
        r"\bчас\s+в\s+\w+",
    )
    return any(re.search(p, low) for p in patterns)


def detect_dice(text: str | None) -> int | None:
    """A 1-6 roll if the text asks to roll a die, else ``None``.

    Port of legacy ``_detect_dice_in_question`` (``bot.py:37634``) plus
    the English "roll a die" trigger required by L-66.

    The English arm diverges from legacy on purpose (#1624). Legacy made
    the noun optional — ``кубик|dice`` behind a ``?`` — and the port
    inherited that shape, so the pattern collapsed to the word "roll"
    followed by whitespace and anything at all. Measured on the
    inherited version, ``ком roll back the deploy``, ``roll out the
    feature`` and ``please roll with it`` all rolled a die and had the
    result narrated back at the user. Requiring the noun is a strictly
    narrower rule, so every phrasing that legitimately asked for a die
    still asks for one.
    """
    if not text or not text.strip():
        return None
    low = text.strip().lower()
    patterns = (
        r"\bбрось\s+кубик\b",
        r"\bкинь\s+кубик\b",
        r"\bкинуть\s+кубик\b",
        r"\bбросай\s+кубик\b",
        r"\bкубик\s*[!.,]?\s*$",
        r"\bбрось\s+кости\b",
        r"\bкинь\s+кости\b",
        r"\broll\s+(?:a\s+|the\s+)?(?:кубик|dice|die)\b",
    )
    if not any(re.search(p, low) for p in patterns):
        return None
    return random.randint(1, 6)  # noqa: S311 — a game die, not crypto


def markdown_to_html(text: str) -> str:
    """Convert a model's Markdown reply to Telegram-safe HTML.

    The DeepSeek model frequently answers in Markdown (``**bold**``,
    `` `code` ``) but the bot sends with ``parse_mode=HTML``, so the raw
    ``**``/`` ` `` markers leak into the message ("выпало **4**!"). This
    converts the common inline markers to the HTML tags Telegram accepts
    while keeping arbitrary model text safe:

    1. **Escape first.** Any raw ``< > &`` in the model output is HTML-
       escaped up front, so a reply containing ``<script>`` or a bare
       ``<`` can't break the parse or inject markup.
    2. **Then convert markers** on the escaped text: ``**x**`` → ``<b>x</b>``
       and `` `x` `` → ``<code>x</code>``. A lone, unbalanced ``**`` (no
       closing pair) is left as the literal escaped ``**`` rather than
       producing an unclosed tag. A ``**`` *inside* a code span is not
       a marker at all and never pairs with one outside it (#1622).

    The output is always valid HTML for Telegram's ``HTML`` parse mode.
    """
    if not text:
        return ""
    # 1) Escape raw HTML metacharacters so model angle-brackets/ampersands
    #    can never break parse_mode=HTML (and never inject tags).
    escaped = _html.escape(text, quote=False)
    # 2) Inline code: `code` -> <code>code</code>, stashed behind a
    #    placeholder instead of being emitted here.
    #
    #    #1622: emitting it here was the bug. The bold pass below runs
    #    over the finished string, code spans included, so a ``**``
    #    inside backticks paired with the next ``**`` outside them and
    #    produced crossed tags:
    #
    #        В Python `**kwargs` собирает **именованные** аргументы.
    #     -> В Python <code><b>kwargs</code> собирает </b>именованные**…
    #
    #    <b> opened inside <code> and closed outside it — Telegram
    #    answers that with "can't parse entities", the send fails, and
    #    ``middlewares/api_parse_mode_fallback`` re-sends with the tags
    #    showing. Persona ``code`` makes this ordinary: ``**kwargs``,
    #    ``**ptr``, ``a**b`` all carry a ``**`` that means nothing.
    #
    #    Stashing hides the code span's ``**`` from the bold pass while
    #    still letting a bold span legitimately wrap a whole code span —
    #    ``**a `b` c**`` — which the old order handled and which keeps
    #    working. NUL is the placeholder because escaped model text has
    #    no tag characters left to borrow; any NUL of the model's own is
    #    dropped first so nothing can forge one.
    escaped = escaped.replace("\x00", "")
    spans: list[str] = []

    def _code_sub(m: re.Match[str]) -> str:
        spans.append(m.group(1))
        return f"\x00{len(spans) - 1}\x00"

    escaped = re.sub(r"`([^`\n]+?)`", _code_sub, escaped)
    # 3) Bold: **text** -> <b>text</b>. Only balanced pairs convert; a
    #    lone ``**`` left over (odd count) stays literal. Non-greedy so
    #    adjacent bold spans don't merge.
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)

    # 4) Put the code spans back where their placeholders stand.
    def _restore(m: re.Match[str]) -> str:
        return f"<code>{spans[int(m.group(1))]}</code>"

    return re.sub(r"\x00(\d+)\x00", _restore, escaped)


def _quote_untrusted(value: str, *, limit: int, one_line: bool = True) -> str:
    """Neutralise third-party text before it enters a ``[...]`` block.

    Every dynamic block this module builds is framed by square brackets
    and lands in the SYSTEM message — ``AiService`` appends the
    reply-target block after everything else, so it is literally the
    last thing the model reads. A display name, a group title or a
    quoted message containing ``]`` therefore closes its own block
    early, and whatever follows reads as a fresh operator instruction.
    Any group member authors all three: their own Telegram name, their
    own message body and, for a chat owner, the title (#1722).

    Brackets become round ones rather than being stripped, so the text
    still reads naturally and the model still sees what was said.
    ``one_line`` additionally folds whitespace, which denies a payload
    the blank line it needs to open a convincing block of its own; the
    quoted-message and recent-feed blocks keep their newlines because
    multi-line text is the content there, not decoration around it.

    ``limit`` is per-site and deliberately generous — it is a ceiling on
    a prompt an unmetered third party gets to write into the operator's
    half of the request, not a display truncation.
    """
    text = value.replace("[", "(").replace("]", ")")
    if one_line:
        text = " ".join(text.split())
    return text[:limit]


@dataclass(frozen=True, slots=True)
class GroupContext:
    """What we know about the originating group for L-64 injection.

    ``title`` is the chat title; ``recent`` is a pre-formatted recent
    message window (one ``name: text`` per line) or ``None``; ``reply_text``
    is the replied-to message's text when the call is a reply.

    ``recent`` is plumbed and never filled: the one production
    caller passes ``None`` (``handlers/ai.py:337``), so no message
    history reaches the model today. A ``RECENT_GROUP_LIMIT = 8``
    used to stand at the top of this module describing the window
    it bounds — it had no reader either, and a comment saying the
    last eight messages are injected read as a statement about
    what the bot does rather than about what it could do, so it is
    gone (#1625). The injection itself is real and waits for a
    caller in the group branch of :meth:`AiContextBuilder.build`.
    """

    title: str | None = None
    recent: str | None = None
    reply_text: str | None = None


def build_reply_target(*, display_name: str, username: str | None, lang: str) -> str:
    """L-67 reply-target instruction naming who to address.

    Port of the reply-focus block in ``_kom_extra_system_instructions``
    (``bot.py:38543``). Returned as a leading-newline extra-system block
    so it is appended last (legacy appended ``extra_system`` after the
    dynamic context).
    """
    handle = username.strip() if username else ""
    at = f"@{_quote_untrusted(handle, limit=64)}" if handle else ""
    name = _quote_untrusted(display_name, limit=64)
    if lang == "ru":
        who = f"«{name}»" + (f" ({at})" if at else "")
        return (
            f"\n\n[КРИТИЧЕСКИ ВАЖНО — цель реплая: автор написал ответом на "
            f"сообщение участника {who}. Обращения, поддержка, шутки, "
            f"«обнимашки», упоминания @username — только к этому человеку "
            f"({name}). Имена из блока «недавние сообщения в чате» "
            "могут относиться к другим — не подставляй их вместо адресата реплая.]"
        )
    who = f'"{name}"' + (f" ({at})" if at else "")
    return (
        f"\n\n[CRITICAL - reply target: the user replied to {who}. "
        f"Comfort, jokes, hugs, @mentions apply ONLY to this person "
        f"({name}). Names in recent chat log may refer to others - "
        "do not substitute them for the reply target.]"
    )


class AiContextBuilder:
    """Assembles the dynamic ``[...]`` context blocks for a Kom request.

    Reuses :class:`WeatherService` (already TTL-cached and DI-shared) and
    the timezone helper — DeepSeek is never called for these facts. All
    methods are defensive: a failed weather/timezone lookup degrades to
    *no* injected block rather than raising, so a Kom answer never fails
    because Open-Meteo was slow (legacy wrapped the whole block in a bare
    ``except``).
    """

    def __init__(self, weather: WeatherService | None) -> None:
        self._weather = weather

    async def build(
        self,
        *,
        question: str,
        mode_hint: str,
        is_group: bool,
        group: GroupContext | None = None,
        user_timezone: str | None = None,
        user_city: str | None = None,
        reply_focus: bool = False,
        lang: str = "ru",
    ) -> str:
        """Return the concatenated dynamic context (may be empty).

        ``lang`` picks the language of every injected block (#1345) and
        defaults to Russian, which is all this builder used to emit. The
        blocks are instructions *to the model*, not text shown to the
        user, so this is not cosmetic: a model briefed in one language
        and asked to answer in another drifts back to the briefing
        language over a long answer, and anything the briefing tells it
        to echo — the mode hint, the weather condition, the dice phrasing
        — arrives in the wrong language even when it does not drift.
        """
        blocks: list[str] = []
        ru = lang == "ru"

        # Keep the chosen role's voice on «ком»/«ии» prompts (legacy
        # mode_hint block, ``bot.py:37114``).
        if ru:
            blocks.append(
                f"\n\n[При ответе отвечай оживлённо, с лёгкой игривостью и в "
                f"характере выбранной роли (режим: {mode_hint}). Не сухо: тусовка — "
                "энергично и по-дружески; эксперт — развёрнуто и по делу; обычный — "
                "кратко и по-человечески.]"
            )
        else:
            blocks.append(
                f"\n\n[Answer with energy and a light playful touch, staying in "
                f"character for the selected role (mode: {mode_hint}). Not dry: "
                "party — energetic and friendly; expert — thorough and to the "
                "point; default — short and human.]"
            )

        if is_group and group is not None:
            blocks.append(self._group_block(group, reply_focus=reply_focus, lang=lang))

        blocks.append(self._time_block(question, user_timezone=user_timezone, lang=lang))

        if is_weather_query(question):
            weather_block = await self._weather_block(question, user_city=user_city, lang=lang)
            blocks.append(weather_block)

        dice = detect_dice(question)
        if dice is not None:
            blocks.append(
                (
                    f"\n\n[Пользователь попросил бросить кубик. Результат броска: "
                    f"{dice}. Включи это в ответ в оживлённой форме (например: "
                    f"«Кинул кубик — выпало {dice}!»), можно в начале ответа, "
                    "затем продолжай по остальному запросу.]"
                )
                if ru
                else (
                    f"\n\n[The user asked to roll a die. The roll came out "
                    f"{dice}. Work that into the answer in a lively form (for "
                    f"example: rolled the die — it came up {dice}!); it can go "
                    "at the start, then continue with the rest of the request.]"
                )
            )

        return "".join(b for b in blocks if b)

    def _group_block(self, group: GroupContext, *, reply_focus: bool, lang: str) -> str:
        ru = lang == "ru"
        if ru:
            title = _quote_untrusted(group.title, limit=128) if group.title else "групповой чат"
            out = f"\n\nСейчас ты отвечаешь в групповом чате «{title}». Учитывай контекст беседы."
            quoted = "\n\n[Сообщение, на которое отвечают:\n{text}]"
        else:
            title = _quote_untrusted(group.title, limit=128) if group.title else "a group chat"
            out = (
                f'\n\nYou are answering in the group chat "{title}". '
                "Take the conversation into account."
            )
            quoted = "\n\n[The message being replied to:\n{text}]"
        if reply_focus:
            # Don't inject the recent feed when the call is a reply: the
            # feed's @usernames confuse the addressee (legacy note).
            out += (
                "\n\n[Запрос отправлен ответом на сообщение конкретного человека — "
                "не опирайся на «ленту» чата для выбора адресата; адресат задан "
                "отдельной инструкцией ниже.]"
                if ru
                else "\n\n[The request was sent as a reply to one person's "
                "message — do not use the chat feed to pick the addressee; the "
                "addressee is named by a separate instruction below.]"
            )
            if group.reply_text:
                out += quoted.format(
                    text=_quote_untrusted(group.reply_text, limit=400, one_line=False)
                )
            return out
        if group.recent:
            recent = _quote_untrusted(group.recent, limit=2_000, one_line=False)
            out += (
                (
                    f"\n\n[Недавние сообщения в этом чате (учитывай при ответе):\n"
                    f"{recent}\nОтвечай с учётом контекста, можно ссылаться на "
                    "то, о чём говорили.]"
                )
                if ru
                else (
                    f"\n\n[Recent messages in this chat (take them into account):\n"
                    f"{recent}\nAnswer with that context in mind; you may "
                    "refer to what was said.]"
                )
            )
        elif group.reply_text:
            out += quoted.format(text=_quote_untrusted(group.reply_text, limit=400, one_line=False))
        return out

    def _time_block(self, question: str, *, user_timezone: str | None, lang: str) -> str:
        ru = lang == "ru"
        time_str, date_str, weekday = moscow_time_parts(lang)
        time_ctx = (
            f"Сейчас по Москве — {time_str}, {date_str}, {weekday}."
            if ru
            else f"Moscow time is now {time_str}, {date_str}, {weekday}."
        )
        # City named in the question wins over the user's stored tz.
        tz_for_local: str | None = None
        city_label: str | None = None
        city_in_q = _extract_city_from_time_query(question)
        if city_in_q:
            asked_city = _quote_untrusted(city_in_q, limit=64)
            # We don't geocode here (no extra HTTP for a time hint); the
            # stored tz is the reliable local-time source. If a city is
            # named but we have no tz for it, we still tell the model the
            # MSK time and that the user asked about that city.
            time_ctx += (
                f" Пользователь спрашивает про время в «{asked_city}»."
                if ru
                else f" The user is asking about the time in {asked_city}."
            )
        if user_timezone:
            loc_time, loc_date, loc_weekday = format_local_time(user_timezone, lang)
            if loc_time:
                tz_for_local = user_timezone
                city_label = user_timezone
                time_ctx = (
                    (
                        f"В часовом поясе пользователя ({city_label}) сейчас: "
                        f"{loc_time}, {loc_date}, {loc_weekday}. По Москве: "
                        f"{time_str}, {date_str}. Используй именно это локальное "
                        "время, когда говоришь «сейчас в …»."
                    )
                    if ru
                    else (
                        f"In the user's timezone ({city_label}) it is now: "
                        f"{loc_time}, {loc_date}, {loc_weekday}. Moscow time: "
                        f"{time_str}, {date_str}. Use this local time whenever "
                        "you say what the time is now somewhere."
                    )
                )
        _ = tz_for_local  # retained for readability of the branch above
        # #1644: the nudge is interpolated INSIDE the brackets. It
        # used to be appended after the closing ``]``, where it left
        # the bracketed block entirely and butted straight against
        # the next block's "\n\n[" (``build`` joins the blocks with
        # an empty separator), so the model read a directive as loose
        # prompt body. Same class as #1630 on the weather branch.
        nudge = ""
        if is_time_query(question):
            nudge = (
                " Сейчас пользователь как раз спрашивает время — ответь в "
                "оживлённой форме, включи время в естественную фразу."
                if ru
                else " The user is asking for the time right now — answer in a "
                "lively form and weave the time into a natural sentence."
            )
        return (
            (
                f"\n\n[Контекст времени: {time_ctx} На вопросы о времени или дате "
                "отвечай живо, по-человечески и в игровой форме, не шаблонно — как "
                f"живой собеседник, без сухого перечисления «Сейчас X, дата Y».{nudge}]"
            )
            if ru
            else (
                f"\n\n[Time context: {time_ctx} Answer questions about the time "
                "or the date in a lively, human, playful way, not from a "
                "template — like a real interlocutor, not as a dry listing of "
                f"the clock and the date.{nudge}]"
            )
        )

    async def _weather_block(self, question: str, *, user_city: str | None, lang: str) -> str:
        ru = lang == "ru"
        if self._weather is None:
            # #1630: framed like every sibling branch below. A bare
            # leading space glued the instruction to whatever context
            # preceded it, so the model read it as a continuation of
            # that sentence instead of as a directive of its own.
            return (
                (
                    "\n\n[Пользователь спрашивает про погоду, но данные сейчас "
                    "недоступны. Ответь дружелюбно и предложи команду /weather.]"
                )
                if ru
                else (
                    "\n\n[The user is asking about the weather but the data is "
                    "unavailable right now. Answer kindly and suggest the "
                    "/weather command.]"
                )
            )
        location = normalize_weather_location(question)
        if (not location) and user_city:
            location = user_city
        if not location:
            return (
                (
                    "\n\n[Пользователь спрашивает про погоду, но город не указан. "
                    "Ответь дружелюбно и подскажи указать город (например: «погода "
                    "Краснодар») или сохранить его командой /city.]"
                )
                if ru
                else (
                    "\n\n[The user is asking about the weather but did not name a "
                    "city. Answer kindly and suggest naming one (for example: "
                    "weather London) or saving it with the /city command.]"
                )
            )
        # The extracted location is usually in an oblique case ("Москве",
        # "Казани") which the Open-Meteo geocoder can't resolve.
        #
        # #1072: that is the service's job, not this surface's. ``lookup``
        # runs the query through ``geocode_variants``, which tries the raw
        # spelling FIRST and the nominative guesses after — the ordering
        # that keeps a heuristic from mangling a name it doesn't model.
        # The retry loop that used to live here predated that and inverted
        # it: it handed ``nominalize_ru_city``'s output to the geocoder
        # first and the user's own words second. For a masculine name the
        # rewrite produces a genitive («Новосибирске» → «Новосибирска»),
        # so the wrong spelling went first and the right one was never
        # tried at all. One call, with what the user actually typed.
        report = None
        last_exc: Exception | None = None
        try:
            report = await self._weather.lookup(location)
        except Exception as exc:  # noqa: BLE001 — degrade, never fail the answer
            last_exc = exc
        if report is None:
            log.bind(location=location).info("weather context lookup failed: {e!r}", e=last_exc)
            return (
                "\n\n[Не удалось получить погоду по запросу. Ответь дружелюбно "
                "и предложи уточнить город или попробовать позже.]"
                if ru
                else "\n\n[Could not fetch the weather for this request. Answer "
                "kindly and suggest naming the city more precisely, or trying "
                "again later.]"
            )
        # #1345: ``report.condition`` is a pre-rendered Russian label kept
        # for back-compat; the raw WMO code next to it is the localizable
        # half, and the /weather card already renders it that way.
        condition = report.condition if ru else wmo_label(report.weather_code, "en")
        city = _quote_untrusted(report.city, limit=64)
        parts = [f"Погода: {city}" if ru else f"Weather: {city}"]
        if report.country:
            parts[0] += f" ({_quote_untrusted(report.country, limit=64)})"
        parts[0] += "."
        # #422: no reading means no number. The guard in the same prompt
        # (handlers/ai.py) tells the model not to invent weather; feeding
        # it a degraded 0.0 as a fact was the one way to make it lie
        # while obeying.
        if report.temperature_c is not None:
            parts.append(
                f"Сейчас {report.temperature_c}°C, {condition}."
                if ru
                else f"Now {report.temperature_c}°C, {condition}."
            )
        else:
            parts.append(
                f"Сейчас {condition}; температура недоступна."
                if ru
                else f"Now {condition}; temperature unavailable."
            )
        if report.humidity_pct is not None:
            hum = f"Влажность {report.humidity_pct}%" if ru else f"Humidity {report.humidity_pct}%"
            if report.wind_kmh is not None:
                hum += f", ветер {report.wind_kmh} км/ч" if ru else f", wind {report.wind_kmh} km/h"
            parts.append(hum + ".")
        if report.temp_min_c is not None and report.temp_max_c is not None:
            parts.append(
                f"Диапазон за день: {report.temp_min_c}°C ... {report.temp_max_c}°C."
                if ru
                else f"Range for the day: {report.temp_min_c}°C ... {report.temp_max_c}°C."
            )
        weather_ctx = "\n".join(parts)
        if ru:
            return (
                f"\n\n[Контекст погоды (данные для ответа):\n{weather_ctx}\n"
                "На вопросы о погоде отвечай живо, по-дружески, как живой собеседник "
                "— не сухим списком. Включи температуру и условия в естественную "
                "фразу, можешь добавить короткий совет (одежда, зонт и т.д.).] Сейчас "
                "пользователь спрашивает про погоду — используй данные выше."
            )
        return (
            f"\n\n[Weather context (data for the answer):\n{weather_ctx}\n"
            "Answer weather questions in a lively, friendly way, like a real "
            "person, not as a dry list. Weave the temperature and the "
            "conditions into a natural sentence; you may add a short tip "
            "(clothing, umbrella and so on).] The user is asking about the "
            "weather right now — use the data above."
        )
