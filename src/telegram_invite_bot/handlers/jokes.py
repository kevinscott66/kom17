"""``/joke`` (online + pool) and ``/joke18`` (pool only) — RR-6 #72.

Legacy ``cmd_joke`` (bot.py:17277) fetches a **fresh** joke from
JokeAPI (reserve: icanhazdadjoke), translating EN→RU through Lingva for
Russian users, and only falls back to a static list when the network is
down. The port kept the fallback and dropped the fetch, so ``/joke``
served the same finite pool forever — a list is exhausted by any user
who runs the command a dozen times, and then the command is just a
slower way of re-reading it.

Both halves are restored. The network side lives in
:class:`~telegram_invite_bot.services.joke_service.JokeService`, whose
whole contract is "a joke or ``None``" — so an outage of a free humour
API degrades to the offline pool instead of an error, and ``/joke``
answers every single time it is called. ``/joke18`` (bot.py:17296) is
offline by design in legacy too and stays a pure pool pick.

The pick itself is no longer a bare ``random.choice``: a
:class:`~telegram_invite_bot.core.recent_picker.RecentPicker` keyed by
chat keeps the pool from repeating itself within a short window. On the
seven-entry pools that legacy shipped, plain uniform choice collides
often enough to read as a bug.

Chat types: **any**, restoring legacy. The private-only gate the port
carried was justified by legacy's
``require_group_feature(message, "ai", ...)``, but that call can never
deny: ``is_feature_enabled_for_chat`` (bot.py:7992) returns ``True`` for
``features_mode == "full"`` AND, in ``restricted`` mode, for exactly the
``"ai"`` feature — the two modes ``set_group_features_mode`` can write.
So the check the deferral was protecting is a no-op in legacy, and the
group half of these commands was lost for nothing. Group admins do have
a real off-switch in the new pipeline —
:class:`~telegram_invite_bot.handlers.command_access.CommandAccessMiddleware`
(``/cmdcfg``, min-rank 6 = disabled) — which is a stronger and
per-command gate than the phantom one.

Other parity notes:

* Language is taken from ``users.language`` (the field the
  ``UserService.touch`` upsert already maintains) — same source as
  legacy's ``get_user_language``.
* Offline pool contents are copied **verbatim** from legacy
  (bot.py:16976/17046 SFW, bot.py:17094/17111 adult).
* Legacy issues ``bot.send_chat_action(..., "typing")`` before picking.
  Still skipped: the e2e harness treats any outbound method other than
  a send as a regression, and buying a progress hint at the cost of an
  untested Telegram call is the wrong trade for a one-line command.
* Legacy deletes the user's command message via ``try_delete_message``
  (clean-chat aesthetic). aiogram's ``message.delete()`` requires
  either bot admin in the chat or the message being our own; in private
  chats neither holds, so attempting it would just 400.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.core.recent_picker import RecentPicker
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.ai_rate_limit import ContentRateLimitMiddleware
from telegram_invite_bot.services.joke_service import JokeService
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.language import pick_by_language

log = logger.bind(component="handlers.jokes")

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.services.user_service import UserService


# Verbatim copy of bot.py:17094 / 17111 — keep in sync if legacy edits.
# Long-line linter rule (E501) is suppressed per-line: the jokes are
# one-liners by design and wrapping them with implicit concatenation
# would either change whitespace inside the punchline or muddy the
# diff against the legacy source we copied from.
_JOKES_RU_ADULT: tuple[str, ...] = (
    "— Доктор, после вчерашнего у меня всё болит. — Так не повторяйте вчерашнее каждый день.",
    "Муж возвращается поздно. Жена: «Где был?» — «У друзей». — «А почему ты весь в блёстках?» — «Там был… тимбилдинг».",  # noqa: E501
    "— Ты меня любишь? — Безумно. — Тогда почему в телефоне пароль не мой день рождения? — Потому что ты меняешь настроение чаще, чем пароль.",  # noqa: E501
    "В баре: «Что будешь?» — «Как вчера». — «Ты вчера был в другом баре». — «Значит, как позавчера».",  # noqa: E501
    "— Почему ты такой весёлый утром? — Потому что вчера вечером я был умнее.",
    "Коллега: «У меня сегодня всё горит». — «Я думал, это только у меня после пятницы».",
    "— Ты один? — Да. — Тогда кто храпит в твоей спальне? — Кот. — У тебя нет кота. — Вот он и пришёл.",  # noqa: E501
    "Психолог: «Расскажите о своих фантазиях». — «Чтобы выключили будильник и включили деньги».",
    "На свидании: «Ты красивая». — «Спасибо». — «Я про пиццу». — «…ладно, тоже спасибо».",
    "— Дорогой, ты меня хочешь? — Хочу. — Тогда почему ты уже спишь? — Это был быстрый спринт, не марафон.",  # noqa: E501
    "Подруга: «У меня новый парень». — «Какой?» — «Высокий». — «Это рост или характер?» — «Пока только рост».",  # noqa: E501
    "Врач: «Берегите спину». — «Она сама себя бережёт — я её почти не нагружаю».",
    "— Почему ты не пишешь? — Пишу. Только не тебе.",
    "На корпоративе начальник: «Мы — семья!» — «Тогда где выходные и алименты?»",
)

_JOKES_EN_ADULT: tuple[str, ...] = (
    "Therapist: 'Tell me your fantasies.' Me: 'Sleeping through the alarm and waking up rich.'",
    "Date: 'You're funny.' Me: 'Thanks, I practiced in group chats.' Date: 'I meant the pizza.' Me: 'Still thanks.'",  # noqa: E501
    "My love language is sarcasm and paying for Wi‑Fi.",
    "They said 'Netflix and chill.' We chilled. Netflix asked if we're still watching — emotionally, no.",  # noqa: E501
    "I'm not flirting. I'm just buffering eye contact.",
    "My ex said I never listen. Funny, I don't remember that.",
    "Adulting is just googling 'how long can chicken sit out' at 2 a.m.",
    "My dating app says 'new people nearby.' My bank app laughs in overdraft.",
    "Romance isn't dead — it's just in airplane mode.",
    "If my bed had a review section, it would be 5 stars: 'Great support, toxic relationship with alarms.'",  # noqa: E501
    "I'm a snack — expired, but still technically edible.",
)


# /joke (SFW). Verbatim copy of bot.py:16976 (RU) / 17046 (EN) — keep in
# sync if legacy edits. Same offline-only scope as the adult pools above:
# legacy ``cmd_joke`` (bot.py:17277) tries JokeAPI + icanhazdadjoke + Lingva
# first and falls back to these static lists only when the network is down.
# We port ONLY that offline fallback — the three HTTP integrations aren't an
# atomic move (see the module docstring's rationale for /joke18). The
# E501 suppression is per-line for the same reason as the adult pools.
_JOKES_RU_SFW: tuple[str, ...] = (
    "— Что сказал нуль единице? — Красивая у тебя фигура!",
    "Оптимист верит, что стакан наполовину полон. Пессимист — что наполовину пуст. Инженер — что стакан в два раза больше нужного.",  # noqa: E501
    "Жизнь как зебра: то белая полоса, то чёрная. А потом приходит пони и говорит: «Я не зебра».",  # noqa: E501
    "Почему роботы не боятся? У них нервы стальные.",
    "— Папа, а мы бедные? — Нет, сынок, мы просто экономные. — А почему тогда мама говорит, что мы бедные? — Потому что мама не экономная.",  # noqa: E501
    "Учительница: — Петя, назови глагол. — Котёнок! — Почему? — Потому что он мурлычет.",
    "— Доктор, я всё забываю! — А давно? — Что давно?",
    "Почему кошки ловят мышей? Потому что доставка еды не работает.",
    "— Ты меня любишь? — Конечно! — А почему тогда не звонишь? — Потому что люблю тишину.",
    "В магазине: — Это свежее? — Конечно, вчера только испортилось.",
    "Мама: — Убери комнату! — Я уже убрал! — Где? — В корзине для белья.",
    "— Почему ты опоздал? — Трафик. — В воскресенье? — Да, люди тоже решили в воскресенье ехать.",  # noqa: E501
    "Бабушка: — Съешь ещё! — Я не могу! — Можешь, ты просто не стараешься.",
    "— Как называется страх перед понедельником? — Работа.",
    "Почему зебра в полосках? Чтобы хищник не нашёл начало и конец.",
    "— Ты умеешь плавать? — Нет. — А если упадёшь в воду? — Тогда умею кричать.",
    "В лифте надпись: «Не разговаривать с незнакомцами». Лифт молчит — молодец.",
    "— Дорогой, ты купил хлеб? — Купил. — Где? — В списке дел: «купить хлеб».",
    "Почему снеговик улыбается? Потому что видит, как люди мёрзнут.",
    "— Что общего у тумана и начальника? — Оба неясные и мешают видеть дорогу.",
    "Ученик: — Можно не писать сочинение? — Почему? — Потому что я уже всё знаю о лени.",
    "Почему попугай повторяет? Потому что собственных мыслей мало, зато много эха.",
    "— Ты спишь? — Нет. — А храп откуда? — Это кот. — У нас нет кота. — Вот он и пришёл.",
    "Врач: — Больше гуляйте! — А если дождь? — Тогда зонт. — А если ветер? — Тогда быстрее гуляйте.",  # noqa: E501
    "Почему часы тикают? Чтобы мы не забывали, что время уходит, пока мы читаем шутки.",
    "— Пап, а откуда дети? — С капусты. — А я почему не зелёный? — Потому что ты особенный.",
    "На экзамене: — Опишите воду. — Мокрая. — Подробнее. — Очень мокрая.",
    "Почему пингвины не летают? Потому что авиакомпании берут за багаж.",
    "— Ты меня обманул! — Нет, я тебя удивил неожиданным поворотом.",
    "В автобусе: — Уступите место беременной! — Я не беременная, я просто поела.",
    "Почему дверь скрипит? Чтобы соседи знали: ты дома и открываешь холодильник.",
    "— Как дела? — Как у кота: сплю, ем, иногда думаю о смысле жизни. — И что? — Смысл — это корм.",  # noqa: E501
    "Почему программист выходит из душа? На бутылочке было написано: «Намылить, смыть, повторить».",  # noqa: E501
    "— Как программисты выбирают вино? — По номеру версии.",
    "Два бита в байте сидят. Один другому: «Ты чётный?» — «Нет, я нечётный».",
    "Почему у разработчика всегда холодно? Потому что он держит окно открытым — смотрит на консоль.",  # noqa: E501
    "— Почему код не работает? — Работает. — А почему тогда баг? — Потому что ты смотришь не на тот экран.",  # noqa: E501
    "Говорят, в 2026 году ИИ уже не пишет код за тебя — он только объясняет, почему ты сам не понял свой код.",  # noqa: E501
    "Мой алгоритм ленты: покажи то, что я уже видел, но с другой обложкой.",
    "Лайфхак: если положить телефон на зарядку, он всё равно разрядится, пока ты ищешь короткое видео.",  # noqa: E501
    "Я не прокрастинирую — я жду, пока дедлайн станет достаточно близким, чтобы превратиться в адреналин.",  # noqa: E501
    "Самый страшный хоррор — уведомление от банка в 22:00.",
    "Скриншот — это не память, это коллекция «я потом посмотрю».",
    "В тренде: не «привет», а «я тут, но не на чтение».",
    "Спорт — это когда ты идёшь за кофе, а шаги считаются сами.",
    "Мой toxic trait — зайти в чат за одним сообщением и выпасть из жизни на три часа ленты.",
    "Сказали «будь собой». Я был собой. Теперь мне неловко перед собой.",
    "База: лечь пораньше. Кринж: «ещё пять минут». Реально: смотреть Shorts до рассвета.",
    "ИИ: я помогу. Я: объясни смысл. ИИ: …ладно, держи список дел.",
    "Сигма-рутина: встать в пять — лечь в пять… утра следующего дня.",
    "Алгоритм знает меня лучше, чем я сам — и это не комплимент.",
    "«Ты в порядке?» — «Я в процессе обновления». — «Сколько процентов?» — «Ноль, но вайб есть».",  # noqa: E501
    "Скинул другу мем. Он не оценил. Дружба на грани: либо ты понимаешь юмор, либо ты бумер.",
    "Чат GPT после третьего «перефразируй короче»: молчит. Я понимаю. Я тоже устал.",
    "Мой главный скилл — делать вид, что я слушаю, пока в голове саундтрек из 2016.",
    "Нейросеть нарисовала кота. Я заплакал. Не от красоты — от того, что это мой уровень рисования.",  # noqa: E501
    "«Прикоснись к траве». Я прикоснулся. Трава сказала: «иди спать».",
    "Реакция на жизнь: 👍 (иронично).",
    "Если бы прокрастинация была валютой, я бы уже купил себе выходные.",
    "Мой inner peace живёт там же, где «завтра начну».",
    "Brain rot — это когда ты смеёшься над абсурдом, а потом понимаешь, что это твоя жизнь.",
    "Вайб чата: все онлайн, никто не отвечает. Классика.",
    "Я не ленивый — я в режиме энергосбережения с человеческим лицом.",
    "Мем про понедельник не смешной. Мем про то, что я снова не выспался — документальный.",
    "Когда говорят «отстань от телефона», я открываю телефон, чтобы записать это в заметки.",
)

_JOKES_EN_SFW: tuple[str, ...] = (
    "Why don't scientists trust atoms? Because they make up everything.",
    "I told my computer I needed a break. It said: 'No problem — I'll go to sleep mode.'",
    "Parallel lines have so much in common. It's a shame they'll never meet.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "My math teacher called me average. How mean!",
    "I used to hate facial hair, but then it grew on me.",
    "What do you call a bear with no teeth? A gummy bear.",
    "I'd tell you a construction joke, but I'm still building the punchline.",
    "There are 10 kinds of people: those who understand binary and those who don't.",
    "Why did the coffee file a police report? It got mugged.",
    "I'm on a seafood diet. I see food and I eat it.",
    "Why don't eggs tell jokes? They'd crack each other up.",
    "I only know 25 letters of the alphabet. I don't know y.",
    "What's orange and sounds like a parrot? A carrot.",
    "Why was the math book sad? Because it had too many problems.",
    "I have a joke about construction — I'm still working on it.",
    "Time flies like an arrow. Fruit flies like a banana.",
    "I'd explain electricity, but it's shocking.",
    "Why did the bicycle fall over? Because it was two tired.",
    "My dog used to chase people on a bike. It got so bad, I had to take his bike away.",
    "My AI said it can't help with my homework — finally, some honesty.",
    "I don't scroll — I perform 'research' on the algorithm.",
    "My screen time says 2026 — I call it a feature, not a bug.",
    "In 2026, notifications are basically jump scares.",
    "I put my phone on the charger. It charged me emotionally instead.",
    "My vibe is 'online' but my brain is in airplane mode.",
    "I'm not lazy — I'm in power-saving mode with a human face.",
    "My brain has 47 tabs: one is a song, the rest are anxiety in HD.",
    "They said touch grass. I Googled what grass looks like. 10/10 would recommend.",
    "Main character moment: I said 'bruh' out loud in public and nobody questioned it.",
    "My algorithm knows me better than my friends — and that's not a compliment.",
    "Sigma routine: wake up at 5 — go to bed at 5… AM the next day.",
    "ChatGPT after the 5th 'make it shorter': silent. Relatable.",
    "I sent a meme. You didn't laugh. Our friendship is now in beta testing.",
    "If procrastination were crypto, I'd be mining it 24/7.",
    "My screen time isn't a bug — it's a feature with trauma DLC.",
    "NPC energy: smiling through notifications like it's a cutscene.",
    "I don't scroll — I conduct peer-reviewed research on chaos.",
    "My sleep schedule isn't broken — it's early access.",
    "The grass touched me back. We are not on speaking terms.",
    "If my life had patch notes, today's would just say 'misc bugfixes'.",
)


# One picker per pool family, shared process-wide: the anti-repeat ring
# is keyed by chat, so a module-level instance is what makes two users in
# the same group see a varied stream rather than each other's repeats.
_ADULT_PICKER = RecentPicker()
_SFW_PICKER = RecentPicker()


def _pick(lang: str, key: object) -> str:
    """Anti-repeat pick keyed off language. ``en`` → EN pool; everything
    else → RU, matching legacy's ``"en" if l == "en" else "ru"`` branch.

    Callers always pass :pyattr:`User.language` (canonical ``"ru"``/``"en"``),
    so the legacy strip-lower defence is gone — see the docstring on
    :func:`pick_by_language` for the contract. ``key`` scopes the
    recently-served ring (chat id), so one busy group cannot thin out
    another's pool.
    """
    pool = pick_by_language(lang, ru=_JOKES_RU_ADULT, en=_JOKES_EN_ADULT)
    return _ADULT_PICKER.pick((key, lang), pool)


def _pick_sfw(lang: str, key: object) -> str:
    """SFW twin of :func:`_pick` — same language-keyed pick over the
    ``/joke`` static pools, used when the online source has nothing.
    """
    pool = pick_by_language(lang, ru=_JOKES_RU_SFW, en=_JOKES_EN_SFW)
    return _SFW_PICKER.pick((key, lang), pool)


def _header(lang: str) -> str:
    return t("h_joke18_header", lang)


async def handle_joke18(message: Message, user_service: UserService) -> None:
    """Touch the user (keeps ``last_seen`` fresh, mirrors legacy's
    ``update_user_info`` at handler top) then render a pool-pick.
    """
    user = await user_service.touch(require_from_user(message))
    # The bot-wide parse_mode is HTML (app.py default). Pool entries today
    # contain no `<` / `>` / `&`, but ``html.escape`` is the cheap insurance
    # that a future joke editor can't accidentally break the envelope — the
    # legacy command sent under Markdown parse_mode where any literal `*`
    # or `_` in a punchline would have the same risk; we just close the
    # equivalent door for HTML up front.
    body = html.escape(_pick(user.language, message.chat.id))
    await message.answer(_header(user.language) + body)
    log.bind(
        uid=user.user_id,
        lang=user.language,
    ).info("/joke18 rendered")


async def handle_joke(
    message: Message,
    user_service: UserService,
    joke_service: JokeService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """SFW ``/joke``: a fresh joke from the network, else the local pool.

    Legacy ``cmd_joke`` (bot.py:17277) sends the joke text alone, with NO
    header (only ``/joke18`` prepends one) — kept, so the online and
    offline answers are indistinguishable in shape and a fallback never
    announces itself as a degradation.

    ``html.escape`` matters far more here than on the offline path: the
    text is now arbitrary third-party content under a bot-wide HTML
    parse_mode, so an upstream joke containing ``<`` would otherwise
    either break the envelope or silently swallow the punchline.
    """
    user = await user_service.touch(require_from_user(message))
    # ``touch`` opened a write transaction on ``users.db``; the fetch
    # below waits on a third-party joke API. Commit first so nobody
    # else's update queues behind that wait (:class:`db.session.Checkpoint`).
    if checkpoint is not None:
        await checkpoint()
    online = await joke_service.fetch(user.language)
    body = html.escape(online or _pick_sfw(user.language, message.chat.id))
    await message.answer(body)
    log.bind(
        uid=user.user_id,
        lang=user.language,
        online=online is not None,
    ).info("/joke rendered")


def build_router(service: JokeService | None = None) -> Router:
    """Factory — fresh ``Router`` per call so tests can re-wire dispatchers.

    ``service`` is the shared :class:`JokeService`; it defaults to a new
    instance for test convenience, and production wiring in
    :mod:`routers.main_router` passes one instance for the whole
    process. That instance carries the ``enabled`` flag and nothing
    else of consequence — no ``httpx.AsyncClient`` is injected anywhere
    today, so each online fetch opens and closes its own (#423).

    Chat types are unrestricted: legacy answered both commands in groups
    (its ``require_group_feature(..., "ai")`` gate can never deny — see
    the module docstring), and ``/cmdcfg`` is the real per-command
    off-switch for group admins in the new pipeline.
    """
    joke_service = service if service is not None else JokeService()

    async def _handle_joke(
        message: Message,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_joke(message, user_service, joke_service, checkpoint)

    router = Router(name="jokes")
    router.message.register(
        handle_joke18,
        Command(
            "joke18",
            "шутка18",
            "анекдот18",
            "funny18",
            "kom_joke18",
            ignore_case=True,
        ),
        F.from_user,
    )
    # /joke goes on a sub-router carrying the content rate limit, because
    # it is the only one of the pair that can reach the network. Gating
    # /joke18 — a pure local pick — would cost a user their punchline for
    # nothing. The two are distinct command names, so registration order
    # against the parent's own handler is immaterial here.
    online_router = Router(name="jokes.online")
    online_router.message.middleware(ContentRateLimitMiddleware())
    online_router.message.register(
        _handle_joke,
        Command(
            "joke",
            "шутка",
            "анекдот",
            "kom_joke",
            # #501: legacy alias (bot.py:42349), lost in the port.
            "funny",
            ignore_case=True,
        ),
        F.from_user,
    )
    router.include_router(online_router)
    return router
