"""Copy for the site's front page, in RU and EN.

Same rule as :mod:`telegram_invite_bot.cms.legal.documents`, and partly
for the same reason: text held in the repository is reviewed in a diff
and deployed with the code. The other reason is narrower — this page is
what an acquiring bank's reviewer sees when they trim a legal URL down
to the bare domain, so "what is this service, and where are its
documents" has to be answered there, without them opening Telegram.

Deliberately *not* routed through :func:`~telegram_invite_bot.i18n.t`:
that helper renders its values as **Telegram** markup, which would turn
the ``**bold**`` and ``[label](url)`` this page needs into literal
asterisks and brackets on the page. The short chrome labels do come from
i18n — see :mod:`telegram_invite_bot.cms.home.router`.

The body is stored as separate pieces rather than one blob because two
of them are conditional: ``commands_md`` advertises ``/commands``, which
only exists when ``GUIDE_SITE_ENABLED`` is on, and ``contact_md`` is the
one line of the documents list that points at ``/contact``, which only
exists when ``SITE_CONTACT_ENABLED`` is. A section — or a bullet —
promising a page that 404s is worse than no section.

Section headings are written with a single ``#``, which the renderer
maps to ``<h2>`` — the page's one ``<h1>`` is the shell's hero title, so
``##`` here would publish an ``<h3>`` with no ``<h2>`` above it and skip
a level for anyone reading by headings.

Line-wrapping rule (inherited from the guide's markdown dialect, see
:func:`~telegram_invite_bot.cms.legal.documents.unwrap_paragraphs`):
prose may be soft-wrapped and is rejoined on the way out, but a list
item or a numbered step must stay on one source line — a wrapped one
would publish its tail as a stray paragraph between the bullets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Substituted at render time. ``[[SERVICE]]`` is the same token the
#: legal documents use; the URL tokens are this page's own, because it
#: is the only page that links at every other one — one token per page
#: the site serves, which is the property that keeps that claim true.
SLOT_SERVICE: Final[str] = "[[SERVICE]]"
SLOT_COMMANDS: Final[str] = "[[COMMANDS]]"
SLOT_PRIVACY: Final[str] = "[[PRIVACY]]"
SLOT_TERMS: Final[str] = "[[TERMS]]"
SLOT_SUPPORT: Final[str] = "[[SUPPORT]]"
SLOT_CONTACT: Final[str] = "[[CONTACT]]"


@dataclass(frozen=True, slots=True)
class HomeCopy:
    """The front page in one language.

    ``title`` and ``lede`` are plain text — the shell escapes and prints
    them as-is. The ``*_md`` fields are Markdown for
    :func:`~telegram_invite_bot.cms.guide_site.markdown.md_to_html`.

    ``contact_md`` is a single list item, not a section: it belongs to
    the ``docs_md`` list and is appended to it when the contact form is
    mounted. Stored apart only because that append is conditional.
    """

    title: str
    lede: str
    intro_md: str
    start_md: str
    commands_md: str
    docs_md: str
    contact_md: str


_RU = HomeCopy(
    title="Монеты, приглашения и игры в Telegram",
    lede=(
        "[[SERVICE]] — телеграм-бот с внутренней монетой: её зарабатывают в чате, "
        "пополняют картой или криптовалютой и тратят внутри бота."
    ),
    intro_md="""
# Что это

Бот работает и в личных сообщениях, и в группах. Внутренняя монета — DLAB: её начисляют
за активность, приглашения и ежедневный бонус, а можно и купить — за рубли или
криптовалюту. Тратится она только внутри бота, вывод идёт обратно в криптовалюту.

- **Профиль и баланс.** Ежедневный бонус, статистика активности, переводы монет.
- **Приглашения.** Монеты за приведённых в группу друзей и комиссия с их пополнений.
- **Магазин и VIP.** Покупки за монеты, подписки, размещение объявлений.
- **Игры.** Кубик, монетка, дуэль, камень-ножницы-бумага, рулетка — ставка со своего счёта.
- **Группы.** Модерация, приветствие новичков, фильтр слов, статистика чата, ранги.
- **Ассистент.** Вопросы к ИИ, погода, время, курсы валют — обычными словами, без слэша.
""",
    start_md="""
# Как начать

1. Откройте бота в Telegram и нажмите «Начать» — кошелёк создаётся сразу.
2. Отправьте `/help`: команды разложены по разделам, там же ежедневный бонус.
3. Добавьте бота в свою группу, чтобы включить приглашения, модерацию и статистику.
""",
    commands_md="""
# Все команды

Полный список с описаниями — и словами, которыми команды вызываются без слэша, — на
[странице справочника]([[COMMANDS]]). Там же подробный гайд по экономике: монеты,
пополнение, вывод, игры и покупки.
""",
    docs_md="""
# Документы

- [Политика конфиденциальности]([[PRIVACY]]) — какие данные бот получает и сколько хранит.
- [Пользовательское соглашение]([[TERMS]]) — правила пополнения, покупок, игр и вывода.
- [Поддержка]([[SUPPORT]]) — как связаться и в какой срок мы отвечаем.
""",
    contact_md=(
        "- [Связаться с оператором]([[CONTACT]]) — форма для банка-эквайера, "
        "регулятора и запросов о данных."
    ),
)


_EN = HomeCopy(
    title="Coins, invites and games in Telegram",
    lede=(
        "[[SERVICE]] is a Telegram bot with an in-app coin: you earn it by being active, "
        "top it up by card or crypto, and spend it inside the bot."
    ),
    intro_md="""
# What this is

The bot works both in a private chat and inside groups. The in-app coin is DLAB: it is
granted for activity, for invites and as a daily bonus, and it can also be bought — for
roubles or for crypto. It is spendable only inside the bot; withdrawals go back out as
crypto.

- **Profile and balance.** Daily bonus, activity stats, coin transfers to other members.
- **Invites.** Coins for friends you bring into a group, plus a cut of their top-ups.
- **Shop and VIP.** Purchases for coins, subscriptions, paid listings.
- **Games.** Dice, coin flip, duel, rock-paper-scissors, roulette — staked from your own balance.
- **Groups.** Moderation, greetings, a word filter, chat statistics, ranks and permissions.
- **Assistant.** AI questions, weather, time, exchange rates — in plain words, no slash.
""",
    start_md="""
# Getting started

1. Open the bot in Telegram and press Start — the wallet is created right away.
2. Send `/help`: the commands are grouped by area, and the daily bonus is there too.
3. Add the bot to your group to switch on invites, moderation and statistics.
""",
    commands_md="""
# Every command

The full list with descriptions — and the words that call each command without a slash —
is on [the reference page]([[COMMANDS]]), together with the long guide to the economy:
coins, top-ups, withdrawals, games and purchases.
""",
    docs_md="""
# Documents

- [Privacy policy]([[PRIVACY]]) — what data the bot receives and how long it is kept.
- [Terms of use]([[TERMS]]) — the rules for top-ups, purchases, games and withdrawals.
- [Support]([[SUPPORT]]) — how to reach us, and how quickly we answer.
""",
    contact_md=(
        "- [Contact the operator]([[CONTACT]]) — for the acquiring bank, "
        "regulators and data requests."
    ),
)


def copy_for(lang: str) -> HomeCopy:
    """The front page in ``lang``; anything but ``en`` reads Russian."""
    return _EN if lang == "en" else _RU
