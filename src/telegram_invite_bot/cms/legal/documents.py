"""The three public documents, as Markdown, in RU and EN.

Why the text lives in a Python module and not in a database, a CMS or a
telegra.ph page: the acquiring bank's requirement is not "publish some
documents", it is "the documentation must be permanently available to
users". A page on somebody else's site can be edited by whoever holds
that account and disappears when that service does; a row in the bot's
database can be changed at runtime with no review and no history. Text
in the repository is reviewed in a diff, deployed with the code, and
served by the same process that serves the bot — if the bot is up, the
documents are up, and that is exactly the property the bank is asking
about.

What is *not* baked in is anything that legitimately differs per
operator: who they are, and how to reach them. Those arrive as tokens
(:data:`SLOT_OPERATOR` and friends) filled at render time from
:class:`~telegram_invite_bot.config.settings.LegalConfig`.

Numbers deliberately absent from the text: withdrawal minimums, daily
caps, the coin/USDT rate and the rouble price table. All of them are env
knobs that an operator can change in a minute, and a legal document that
quotes a stale number is worse than one that points at the live screen —
so the offer says "the limits shown in ``/withdraw``" and means it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Date of the current revision, printed on every page. Bump it in the
#: same commit that changes any text below — the reader's only way to
#: tell two versions apart, and the first thing a compliance reviewer
#: looks for.
REVISION: Final[str] = "2026-08-14"

SLOT_SERVICE: Final[str] = "[[SERVICE]]"
SLOT_OPERATOR: Final[str] = "[[OPERATOR]]"
SLOT_UPDATED: Final[str] = "[[UPDATED]]"
SLOT_CONTACTS: Final[str] = "[[CONTACTS]]"


@dataclass(frozen=True, slots=True)
class LegalDoc:
    """One document in both languages.

    ``slug`` is the URL segment *and* the i18n key suffix, so a new
    document is one entry here plus two translation keys — the router
    iterates, it does not enumerate.
    """

    slug: str
    title_ru: str
    title_en: str
    body_ru: str
    body_en: str

    def title(self, lang: str) -> str:
        return self.title_en if lang == "en" else self.title_ru

    def body(self, lang: str) -> str:
        return self.body_en if lang == "en" else self.body_ru


# ---------------------------------------------------------------------------
# Privacy policy
# ---------------------------------------------------------------------------

_PRIVACY_RU = """
# 1. Общие положения

Настоящая Политика конфиденциальности описывает, какие данные Telegram-бот
[[SERVICE]] (далее — «Сервис») получает от пользователя, для чего и на каком
основании их обрабатывает, кому передаёт и сколько хранит.

Оператор обработки данных — [[OPERATOR]]. Начиная пользоваться Сервисом,
пользователь подтверждает, что ознакомился с настоящей Политикой и с
Пользовательским соглашением. Если вы не согласны с Политикой — прекратите
использование Сервиса; удалить свои данные можно в порядке раздела 8.

Редакция от [[UPDATED]].

# 2. Какие данные обрабатываются

Сервис работает внутри Telegram и **не запрашивает** документы, удостоверяющие
личность, адрес проживания, реквизиты банковских карт и коды подтверждения.
Обрабатываются:

- **Идентификаторы Telegram**: числовой ID, username, отображаемое имя, код
  языка клиента. Их передаёт сам Telegram вместе с каждым сообщением.
- **Данные о пользовании Сервисом**: баланс внутренних монет и история
  операций (пополнения, покупки, ставки в игровых функциях, переводы между
  пользователями, заявки на вывод), достижения, дата регистрации.
- **Настройки, заданные пользователем**: язык интерфейса, город для прогноза
  погоды, часовой пояс, валюта отображения.
- **Статистика активности в группах**, куда Сервис добавлен администратором:
  факт и время сообщений в объёме, необходимом для рейтингов, антифлуда и
  начисления монет за активность. Содержимое сообщений в базу не сохраняется,
  за исключением случаев, прямо названных в двух пунктах ниже.
- **Обращения в поддержку**: текст обращения и переписка по нему, а также
  контакт для ответа, если пользователь указал его сам — например при
  обращении через форму на сайте. Форма на сайте не сохраняет и не передаёт
  IP-адрес и данные браузера: оператор получает только то, что написано в
  полях формы.
- **Запросы к AI-функциям**: текст, который пользователь сам адресовал боту в
  режиме AI-ответа или голосовой расшифровки.
- **Технические сведения о платеже**: идентификатор платежа у провайдера,
  сумма, валюта, статус и внутренний номер заказа.

# 3. Цели обработки

Предоставление функций Сервиса; начисление и списание внутренних монет;
работа игровых, групповых и справочных функций; поддержка пользователей и
разбор спорных ситуаций по платежам; защита от мошенничества, мультиаккаунтов
и автоматизированной накрутки; исполнение требований законодательства.

# 4. Основания обработки

Согласие пользователя, выраженное началом использования Сервиса; исполнение
договора (Пользовательского соглашения, заключаемого при использовании);
законные интересы оператора в части безопасности и предотвращения
злоупотреблений; требования применимого законодательства.

# 5. Передача третьим лицам

Оператор **не продаёт** данные пользователей и не передаёт их для рекламных
целей. Передача происходит только в объёме, необходимом для работы Сервиса:

- **Telegram** — платформа, через которую идёт всё взаимодействие; на неё
  распространяется политика конфиденциальности Telegram;
- **платёжные провайдеры** — для приёма платежей и подтверждения зачисления;
- **поставщики AI-функций** — только текст запроса, который пользователь сам
  отправил боту в AI-режиме;
- **внешние справочные сервисы** (погода, курсы валют) — только сам запрос
  (город, пара валют), без идентификаторов пользователя;
- **хостинг-провайдер** сервера, на котором работает Сервис;
- **уполномоченные государственные органы** — по законному запросу.

# 6. Платёжные данные

Оплата производится на стороне платёжного провайдера, на его странице или в
банковском приложении. Номер карты, срок действия, CVV, коды подтверждения и
данные банковского приложения **в Сервис не передаются и не хранятся**. Сервис
получает от провайдера только идентификатор платежа, сумму, валюту, статус и
внутренний номер заказа — того минимума достаточно, чтобы зачислить монеты и
разобрать спорную ситуацию.

# 7. Хранение и защита

Данные хранятся на сервере оператора; доступ к ним имеет оператор и
уполномоченные им лица. Взаимодействие с Telegram и с платёжными провайдерами
идёт по защищённому соединению (HTTPS); уведомления о платежах принимаются
только с корректной криптографической подписью провайдера.

Сроки хранения: данные аккаунта — пока аккаунт используется и далее в течение
срока, необходимого для разрешения возможных споров; сведения о финансовых
операциях — не менее трёх лет с даты операции; обращения в поддержку — один
год с момента закрытия обращения.

# 8. Права пользователя

Пользователь вправе: запросить сведения о том, какие его данные хранятся;
потребовать их исправления; потребовать удаления аккаунта и связанных с ним
данных; отозвать согласие на обработку. Запрос направляется через команду
`/support` в боте или по контактам из раздела 10; ответ предоставляется в срок
до 30 дней.

Удаление аккаунта прекращает доступ к внутренним монетам и не является
основанием для их выплаты. Сведения о совершённых финансовых операциях
сохраняются в объёме и на срок, предписанные законом, — их удаление
невозможно.

# 9. Возрастное ограничение

Сервис не предназначен для лиц младше 18 лет. Оператор не собирает данные о
несовершеннолетних осознанно; при обнаружении такого аккаунта он блокируется,
а связанные с ним данные удаляются.

# 10. Контакты

[[CONTACTS]]

# 11. Изменение Политики

Оператор вправе изменять Политику. Действующая редакция всегда доступна на
этой странице и датирована. Продолжение использования Сервиса после
публикации новой редакции означает согласие с ней.
""".strip()

_PRIVACY_EN = """
# 1. General provisions

This Privacy Policy describes what data the [[SERVICE]] Telegram bot (the
"Service") receives from a user, why and on what basis it is processed, who it
is shared with, and how long it is kept.

The data controller is [[OPERATOR]]. By starting to use the Service the user
confirms that they have read this Policy and the Terms of Use. If you do not
agree with the Policy, stop using the Service; your data can be deleted as
described in section 8.

Revision of [[UPDATED]].

# 2. Data processed

The Service runs inside Telegram and **does not request** identity documents,
a home address, card credentials or confirmation codes. It processes:

- **Telegram identifiers**: numeric ID, username, display name, client
  language code — sent by Telegram itself with every message.
- **Usage data**: the internal coin balance and operation history (top-ups,
  purchases, wagers in the game features, transfers between users, withdrawal
  requests), achievements, registration date.
- **User-set preferences**: interface language, city for the weather forecast,
  time zone, display currency.
- **Group activity statistics** for groups where the Service was added by an
  administrator: the fact and time of messages, to the extent needed for
  ratings, flood protection and activity rewards. Message content is not
  stored, except in the two cases named below.
- **Support requests**: the text of the request and the correspondence about
  it, plus the reply contact where the user supplied one themselves — for
  example when writing through the web form. The web form neither stores nor
  forwards an IP address or browser details: the operator receives only what
  was typed into its fields.
- **AI requests**: the text a user has themselves addressed to the bot in AI
  reply or voice transcription mode.
- **Technical payment details**: the provider's payment identifier, amount,
  currency, status and the internal order number.

# 3. Purposes

Providing the Service's features; crediting and debiting internal coins;
operating the game, group and reference features; user support and the
resolution of payment disputes; protection against fraud, multi-accounting and
automated farming; compliance with applicable law.

# 4. Legal basis

The user's consent, given by starting to use the Service; performance of the
contract (the Terms of Use entered into by using the Service); the operator's
legitimate interest in security and the prevention of abuse; requirements of
applicable law.

# 5. Sharing with third parties

The operator **does not sell** user data and does not share it for advertising
purposes. Data is shared only to the extent the Service needs to work:

- **Telegram** — the platform all interaction runs through; Telegram's own
  privacy policy applies to it;
- **payment providers** — to accept payments and confirm crediting;
- **AI providers** — only the text of a request the user has themselves sent
  to the bot in AI mode;
- **external reference services** (weather, exchange rates) — only the query
  itself (a city, a currency pair), with no user identifiers;
- **the hosting provider** of the server the Service runs on;
- **competent public authorities** — upon a lawful request.

# 6. Payment data

Payment happens on the payment provider's side, on their page or in a banking
application. Card number, expiry date, CVV, confirmation codes and banking
application data are **never passed to the Service and never stored by it**.
The Service receives only the payment identifier, amount, currency, status and
internal order number — the minimum needed to credit coins and to investigate
a dispute.

# 7. Storage and protection

Data is stored on the operator's server; access is limited to the operator and
persons authorised by them. Communication with Telegram and with payment
providers uses a secure connection (HTTPS); payment notifications are accepted
only with a valid cryptographic signature from the provider.

Retention: account data — while the account is in use and thereafter for as
long as needed to resolve possible disputes; records of financial operations —
at least three years from the date of the operation; support requests — one
year after the request is closed.

# 8. User rights

A user may: request what data about them is stored; ask for it to be
corrected; ask for the account and associated data to be deleted; withdraw
consent to processing. Requests go through the `/support` command in the bot
or the contacts in section 10; an answer is provided within 30 days.

Deleting an account ends access to the internal coins and is not a ground for
paying them out. Records of financial operations already carried out are
retained to the extent and for the period prescribed by law and cannot be
deleted.

# 9. Age restriction

The Service is not intended for persons under 18. The operator does not
knowingly collect data about minors; an account found to belong to one is
blocked and the associated data deleted.

# 10. Contacts

[[CONTACTS]]

# 11. Changes to this Policy

The operator may amend this Policy. The current revision is always available
on this page and is dated. Continuing to use the Service after a new revision
is published means acceptance of it.
""".strip()


# ---------------------------------------------------------------------------
# Terms of use / public offer
# ---------------------------------------------------------------------------

_TERMS_RU = """
# 1. Общие положения

Настоящий документ является публичной офертой [[OPERATOR]] (далее —
«Оператор») и определяет условия использования Telegram-бота [[SERVICE]]
(далее — «Сервис») любым лицом (далее — «Пользователь»).

Акцептом оферты — то есть полным и безоговорочным принятием её условий —
является начало использования Сервиса, а для платных функций дополнительно
факт оплаты. Соглашение вступает в силу с момента акцепта.

Редакция от [[UPDATED]].

# 2. Термины

- **Монеты** — внутренняя учётная единица Сервиса, отражающая объём
  оплаченных и заработанных Пользователем прав на использование платных
  функций. Монеты **не являются** денежными средствами, электронными
  денежными средствами, ценными бумагами или криптовалютой и не могут
  использоваться как средство платежа за пределами Сервиса.
- **Пополнение** — приобретение монет за деньги через платёжного провайдера.
- **Вывод** — обмен монет обратно на цифровой актив по заявке Пользователя, на
  условиях раздела 7.

# 3. Предмет соглашения

Оператор предоставляет Пользователю доступ к функциям Сервиса: справочным
(погода, курсы валют, время), развлекательным (игровые механики, ролевые
команды), социальным (профиль, рейтинги, переводы монет), а также к
инструментам администрирования групп. Часть функций доступна бесплатно, часть
оплачивается монетами.

Сервис является программным продуктом развлекательного и вспомогательного
назначения. Он не является банковской, платёжной, инвестиционной, кредитной,
страховой или брокерской услугой и не предоставляет финансовых консультаций.

# 4. Пополнение, цена и момент оказания услуги

Стоимость монет отображается в боте по команде `/topup` на момент покупки.
Цена привязана к курсу доллара США и может изменяться; количество монет,
указанное на кнопке, является ориентировочным на момент показа, а фактическое
зачисление рассчитывается от суммы, подтверждённой платёжным провайдером.

Монеты зачисляются автоматически после подтверждения платежа провайдером —
как правило, в течение нескольких секунд, при задержках на стороне провайдера
до 30 минут. **Услуга считается оказанной в полном объёме в момент зачисления
монет на баланс Пользователя.**

# 5. Возврат

Монеты являются цифровым продуктом, передаваемым в момент оплаты. После
зачисления монет на баланс возврат уплаченных денежных средств не
производится, в том числе если монеты израсходованы на игровые функции,
покупки или переводы.

Исключение — техническая ошибка: если денежные средства списаны, а монеты не
зачислены. В этом случае Пользователь обращается в поддержку (раздел 12),
указав идентификатор платежа, дату, сумму и способ оплаты. Обращение
рассматривается в срок до 3 (трёх) рабочих дней. При подтверждении ошибки
Оператор по выбору Пользователя зачисляет монеты либо возвращает денежные
средства тем же способом, которым была произведена оплата.

# 6. Игровые функции

Игровые функции Сервиса носят развлекательный характер, оплачиваются
исключительно внутренними монетами и предназначены для лиц **старше 18 лет**.

Исход игровых функций определяется случайным образом; распределение
настроено так, что математическое ожидание для Пользователя отрицательное, —
то есть на длинной дистанции ставки приводят к уменьшению баланса. Монеты,
поставленные в игровой функции, могут быть потеряны полностью. Игровые функции
не являются источником дохода, инвестицией или способом заработка.

# 7. Вывод монет

Пользователь вправе подать заявку на обмен монет на цифровой актив. Актуальные
условия — минимальная и максимальная сумма, курс, суточный и месячный
лимиты — отображаются в боте по команде `/withdraw` и могут изменяться
Оператором.

Существенные условия вывода:

- заявка рассматривается Оператором **вручную**, срок рассмотрения — до 3
  (трёх) рабочих дней;
- **совокупная сумма всех выплат Пользователю не может превышать совокупную
  сумму его пополнений.** Сервис не является источником дохода: вывести можно
  не больше, чем было внесено;
- вывод доступен только Пользователям, совершившим хотя бы одно пополнение;
- Оператор вправе отказать в выводе при обоснованных подозрениях в
  мошенничестве, использовании нескольких аккаунтов, автоматизированной
  накрутке активности или ином нарушении настоящего соглашения, с указанием
  причины отказа.

# 8. Обязанности Пользователя

Пользователь обязуется: быть совершеннолетним; использовать Сервис лично и не
создавать нескольких аккаунтов; не использовать автоматизацию, скрипты и
эмуляторы для накрутки активности или обхода лимитов; не пытаться получить
доступ к чужим аккаунтам, балансам или к серверной части Сервиса; не
использовать Сервис для противоправных действий, оскорблений, распространения
запрещённого контента и мошенничества; самостоятельно обеспечивать сохранность
своего аккаунта Telegram.

Действия, совершённые из аккаунта Telegram Пользователя, считаются
совершёнными Пользователем.

# 9. Права Оператора

Оператор вправе: изменять набор функций, цены, курсы и лимиты; приостанавливать
работу Сервиса для технического обслуживания; ограничивать или блокировать
доступ Пользователя при нарушении настоящего соглашения; аннулировать монеты,
начисленные в результате ошибки, сбоя или недобросовестных действий.

# 10. Ответственность

Сервис предоставляется «как есть». Оператор прилагает разумные усилия для
бесперебойной работы, но не гарантирует отсутствие сбоев и не несёт
ответственности за перерывы, вызванные работой Telegram, платёжных
провайдеров, хостинг-провайдера, каналов связи и иных третьих лиц.

Оператор не несёт ответственности за упущенную выгоду и за решения, принятые
Пользователем на основании справочной информации Сервиса (курсы валют, погода,
ответы AI-функций) — такая информация носит ознакомительный характер.

Совокупная ответственность Оператора ограничена суммой, фактически уплаченной
Пользователем за 30 календарных дней, предшествующих событию.

# 11. Интеллектуальная собственность

Программный код, интерфейсы, тексты и оформление Сервиса принадлежат Оператору.
Использование Сервиса не влечёт передачи каких-либо исключительных прав.

# 12. Персональные данные и поддержка

Обработка персональных данных описана в Политике конфиденциальности, которая
является неотъемлемой частью настоящего соглашения.

Обращения принимаются через встроенную систему тикетов (`/support` в боте) и
по контактам ниже. Претензионный порядок обязателен: срок ответа на претензию
— 30 календарных дней с момента её получения.

[[CONTACTS]]

# 13. Заключительные положения

Оператор вправе изменять настоящее соглашение. Действующая редакция
публикуется на этой странице и датирована. Продолжение использования Сервиса
после публикации новой редакции означает согласие с ней. Если отдельное
положение соглашения окажется недействительным, остальные положения сохраняют
силу.
""".strip()

_TERMS_EN = """
# 1. General provisions

This document is a public offer by [[OPERATOR]] (the "Operator") and sets out
the terms on which any person (the "User") may use the [[SERVICE]] Telegram bot
(the "Service").

The offer is accepted — fully and unconditionally — by starting to use the
Service and, for paid features, additionally by making a payment. The agreement
takes effect upon acceptance.

Revision of [[UPDATED]].

# 2. Definitions

- **Coins** — the Service's internal unit of account, reflecting the volume of
  paid and earned rights to use paid features. Coins are **not** money,
  electronic money, securities or cryptocurrency, and cannot be used as a means
  of payment outside the Service.
- **Top-up** — acquiring coins for money through a payment provider.
- **Withdrawal** — exchanging coins back into a digital asset upon the User's
  request, on the terms of section 7.

# 3. Subject matter

The Operator gives the User access to the Service's features: reference
(weather, exchange rates, time), entertainment (game mechanics, role-play
commands), social (profile, ratings, coin transfers), and group administration
tools. Some features are free; others are paid for with coins.

The Service is a software product of an entertainment and auxiliary nature. It
is not a banking, payment, investment, credit, insurance or brokerage service
and provides no financial advice.

# 4. Top-ups, price and when the service is rendered

The price of coins is shown in the bot under the `/topup` command at the moment
of purchase. It is anchored to the US dollar rate and may change; the coin
figure on a button is indicative at the time it is displayed, and the actual
credit is computed from the amount confirmed by the payment provider.

Coins are credited automatically once the provider confirms the payment —
normally within seconds, and within 30 minutes where the provider is delayed.
**The service is deemed rendered in full at the moment the coins are credited
to the User's balance.**

# 5. Refunds

Coins are a digital product delivered at the moment of payment. Once coins have
been credited, the money paid is not refunded, including where the coins have
been spent on game features, purchases or transfers.

The exception is a technical failure: money debited and coins not credited. In
that case the User contacts support (section 12) stating the payment
identifier, date, amount and payment method. The request is reviewed within 3
(three) business days. Where the failure is confirmed, the Operator — at the
User's choice — credits the coins or refunds the money by the same method it
was paid.

# 6. Game features

The Service's game features are for entertainment, are paid for exclusively
with internal coins, and are intended for persons **over 18**.

Outcomes are determined at random; the distribution is set so that the User's
mathematical expectation is negative — over a long run, wagering reduces the
balance. Coins wagered in a game feature may be lost in full. Game features are
not a source of income, an investment or a way to earn money.

# 7. Withdrawals

A User may request the exchange of coins into a digital asset. The current
terms — minimum and maximum amount, rate, daily and monthly limits — are shown
in the bot under the `/withdraw` command and may be changed by the Operator.

Material terms of withdrawal:

- requests are reviewed by the Operator **manually**, within 3 (three) business
  days;
- **the total of all payouts to a User may not exceed the total of that User's
  top-ups.** The Service is not a source of income: no more can be withdrawn
  than was paid in;
- withdrawal is available only to Users who have made at least one top-up;
- the Operator may refuse a withdrawal on reasonable suspicion of fraud, use of
  multiple accounts, automated activity farming or any other breach of this
  agreement, stating the reason for the refusal.

# 8. User obligations

The User undertakes: to be of full age; to use the Service personally and not
to create multiple accounts; not to use automation, scripts or emulators to
farm activity or bypass limits; not to attempt access to other users' accounts
or balances or to the Service's server side; not to use the Service for
unlawful acts, abuse, distribution of prohibited content or fraud; and to keep
their own Telegram account secure.

Actions performed from the User's Telegram account are deemed performed by the
User.

# 9. Operator rights

The Operator may: change the set of features, prices, rates and limits; suspend
the Service for maintenance; restrict or block a User's access upon breach of
this agreement; and cancel coins credited as a result of an error, a failure or
bad-faith conduct.

# 10. Liability

The Service is provided "as is". The Operator makes reasonable efforts to keep
it running but does not guarantee the absence of failures and is not liable for
interruptions caused by Telegram, payment providers, the hosting provider,
communication channels or other third parties.

The Operator is not liable for lost profit or for decisions the User makes on
the basis of the Service's reference information (exchange rates, weather, AI
answers) — such information is for guidance only.

The Operator's aggregate liability is limited to the amount actually paid by
the User over the 30 calendar days preceding the event.

# 11. Intellectual property

The Service's source code, interfaces, texts and design belong to the Operator.
Using the Service transfers no exclusive rights.

# 12. Personal data and support

The processing of personal data is described in the Privacy Policy, which forms
an integral part of this agreement.

Requests are accepted through the built-in ticket system (`/support` in the
bot) and through the contacts below. A pre-litigation claim procedure is
mandatory: a claim is answered within 30 calendar days of receipt.

[[CONTACTS]]

# 13. Final provisions

The Operator may amend this agreement. The current revision is published on
this page and is dated. Continuing to use the Service after a new revision is
published means acceptance of it. If any provision is held invalid, the
remaining provisions stay in force.
""".strip()


# ---------------------------------------------------------------------------
# Support
# ---------------------------------------------------------------------------

_SUPPORT_RU = """
# 1. Как обратиться

Основной канал — встроенная система тикетов. Отправьте боту команду
`/support`, опишите вопрос одним сообщением, и обращение будет сохранено с
номером. Ответ приходит в личные сообщения от того же бота. Свои обращения и
их статус можно посмотреть командой `/my_tickets`.

Дублировать обращение в других каналах не нужно — номер тикета достаточен для
любого разбора.

Если доступа к боту нет — например, вы пишете как представитель банка или
регулятора, обращаетесь по поводу персональных данных или у вас просто нет
аккаунта в Telegram, — используйте любой из контактов ниже. Ответ придёт на
тот контакт, который вы укажете.

[[CONTACTS]]

# 2. Сроки

Обращения по платежам рассматриваются в срок до 3 (трёх) рабочих дней;
обращения по персональным данным (раздел 8 Политики конфиденциальности) — до
30 дней; письменные претензии — до 30 календарных дней.

# 3. Что приложить к обращению по платежу

Чтобы разбор не потребовал второго круга уточнений, укажите сразу:

- дату и время платежа;
- сумму и способ оплаты (карта / СБП / криптовалюта);
- идентификатор платежа или номер заказа, если он у вас есть;
- что произошло: деньги списаны и монеты не зачислены, зачислена не та сумма,
  платёж завис в обработке.

# 4. Что поддержка не решает

Возврат средств за монеты, уже израсходованные на игровые функции, покупки или
переводы (раздел 5 Пользовательского соглашения); отмена результатов игровых
функций; восстановление доступа к аккаунту Telegram — это зона ответственности
Telegram, а не Сервиса.
""".strip()

_SUPPORT_EN = """
# 1. How to get in touch

The main channel is the built-in ticket system. Send the bot the `/support`
command, describe the issue in a single message, and the request is stored with
a number. The answer arrives as a direct message from the same bot. Your
requests and their status are available under `/my_tickets`.

There is no need to duplicate a request through other channels — the ticket
number is enough for any investigation.

If you have no access to the bot — you are writing on behalf of a bank or a
regulator, you are asking about personal data, or you simply have no Telegram
account — use any of the contacts below. The answer goes to whichever contact
you leave.

[[CONTACTS]]

# 2. Response times

Payment requests are reviewed within 3 (three) business days; personal-data
requests (section 8 of the Privacy Policy) within 30 days; written claims
within 30 calendar days.

# 3. What to include in a payment request

So that the investigation does not need a second round of questions, state up
front:

- the date and time of the payment;
- the amount and the payment method (card / SBP / crypto);
- the payment identifier or order number, if you have one;
- what happened: money debited and coins not credited, the wrong amount
  credited, or the payment stuck in processing.

# 4. What support cannot do

Refund coins already spent on game features, purchases or transfers (section 5
of the Terms of Use); reverse the outcome of a game feature; or restore access
to a Telegram account — that is Telegram's responsibility, not the Service's.
""".strip()


PRIVACY: Final[LegalDoc] = LegalDoc(
    slug="privacy",
    title_ru="Политика конфиденциальности",
    title_en="Privacy Policy",
    body_ru=_PRIVACY_RU,
    body_en=_PRIVACY_EN,
)

TERMS: Final[LegalDoc] = LegalDoc(
    slug="terms",
    title_ru="Пользовательское соглашение",
    title_en="Terms of Use",
    body_ru=_TERMS_RU,
    body_en=_TERMS_EN,
)

SUPPORT: Final[LegalDoc] = LegalDoc(
    slug="support",
    title_ru="Служба поддержки",
    title_en="Support",
    body_ru=_SUPPORT_RU,
    body_en=_SUPPORT_EN,
)

#: Every document, in the order they are listed to a reader. Privacy and
#: terms first because that is the pair a compliance reviewer opens;
#: support last because it is the one a user opens.
DOCUMENTS: Final[tuple[LegalDoc, ...]] = (PRIVACY, TERMS, SUPPORT)

BY_SLUG: Final[dict[str, LegalDoc]] = {doc.slug: doc for doc in DOCUMENTS}


def build_contacts_md(
    lang: str,
    *,
    support_url: str | None,
    support_email: str | None,
    operator: str,
    operator_details: str | None,
    contact_url: str | None = None,
) -> str:
    """The contact block, as a Markdown list.

    Rendered from config rather than written into each document so the
    three pages cannot end up quoting three different handles — the
    classic way a support contact goes stale.

    The in-bot ticket line is unconditional and comes first: it is the
    one channel that exists in every deployment and it is one of the
    three forms the acquiring bank accepts ("тикет-система, @username
    или email"). The handle and the mailbox are added when configured.

    ``contact_url`` is the web form, and it is listed second on purpose:
    it is the only channel here that works for a reader with no Telegram
    account — a regulator, a bank, or someone asking about their own
    data — but a reader who *does* have the bot is better served by the
    ticket system, which gives their request a number.
    """
    is_en = lang == "en"
    lines: list[str] = []
    if is_en:
        lines.append(f"- **Operator:** {operator}")
        if operator_details:
            lines.append(f"- **Details:** {operator_details}")
        lines.append("- **Support tickets:** the `/support` command in the bot")
        if contact_url:
            lines.append(f"- **Web form:** [{contact_url}]({contact_url})")
        if support_url:
            handle = support_url.rsplit("/", 1)[-1]
            lines.append(f"- **Telegram:** [@{handle}]({support_url})")
        if support_email:
            lines.append(f"- **Email:** [{support_email}](mailto:{support_email})")
    else:
        lines.append(f"- **Оператор:** {operator}")
        if operator_details:
            lines.append(f"- **Реквизиты:** {operator_details}")
        lines.append("- **Обращения:** команда `/support` в боте")
        if contact_url:
            lines.append(f"- **Форма на сайте:** [{contact_url}]({contact_url})")
        if support_url:
            handle = support_url.rsplit("/", 1)[-1]
            lines.append(f"- **Telegram:** [@{handle}]({support_url})")
        if support_email:
            lines.append(f"- **Email:** [{support_email}](mailto:{support_email})")
    return "\n".join(lines)


# An ordered-list item, and only that: the marker is required.
#
# The clause this replaces read ``stripped.partition(" ")[0]
# .rstrip(".)").isdigit()``, which never required the ``.`` or the ``)``
# at all — ``"30".rstrip(".)")`` is ``"30"``, and that is a digit. So any
# soft-wrapped prose line beginning with a bare number counted as
# structure, ended the paragraph and shipped as its own ``<p>``. It fired
# on the live ``/support`` page (#1634), cutting the claim-deadline
# sentence in half at the preposition "до" — on the document written for
# an acquiring bank.
#
# Deliberately wider than the renderer, which honours only ``N.``
# (``guide_site.markdown._ORDERED_RE``), because the bullet markers
# beside it are wider too — ``* `` and ``> `` are not renderer structure
# either. Ending the paragraph is the right call for anything an author
# wrote as a list; whether the renderer then draws it as one is a
# separate question, and answering it here would silently glue the item
# to the prose above.
_ORDERED_ITEM_RE: Final[re.Pattern[str]] = re.compile(r"^\d+[.)]\s")


def unwrap_paragraphs(md_text: str) -> str:
    """Join soft-wrapped prose lines back into one line per paragraph.

    The site's markdown renderer follows the guide's dialect, where one
    source line is one block — the guide is authored that way and uses
    trailing hard breaks deliberately, so widening the renderer would
    reflow a page that is already correct. Legal prose is the opposite
    case: it is wrapped at 80 columns so a clause can be reviewed in a
    diff, and rendered raw that would publish every wrapped line as its
    own ``<p>`` — a ragged, obviously-broken page on exactly the document
    an acquirer reads first.

    So the unwrapping happens here, on the way out of this module, and
    the guide keeps rendering byte-for-byte what it rendered before.
    Structural lines (blank, heading, list item, rule) end a paragraph
    and pass through untouched.
    """
    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            out.append(" ".join(buffer))
            buffer.clear()

    for raw in md_text.split("\n"):
        stripped = raw.strip()
        structural = (
            not stripped
            or stripped.startswith(("#", "- ", "* ", "> ", "---"))
            or _ORDERED_ITEM_RE.match(stripped) is not None
        )
        if structural:
            flush()
            out.append(raw)
            continue
        buffer.append(stripped)

    flush()
    return "\n".join(out)


def render_body(
    doc: LegalDoc,
    lang: str,
    *,
    service: str,
    operator: str,
    contacts_md: str,
    updated: str = REVISION,
) -> str:
    """Fill the tokens in ``doc``'s body for ``lang``.

    Plain ``str.replace`` rather than ``str.format``: the documents are
    prose with numbered clauses and inline code, and a single stray brace
    in a future edit would turn a formatting call into a ``KeyError`` on
    a live page. A missed token, by contrast, is caught by the test that
    asserts no ``[[`` survives rendering.
    """
    body = doc.body(lang)
    for token, value in (
        (SLOT_SERVICE, service),
        (SLOT_OPERATOR, operator),
        (SLOT_UPDATED, updated),
        (SLOT_CONTACTS, contacts_md),
    ):
        body = body.replace(token, value)
    return unwrap_paragraphs(body)
