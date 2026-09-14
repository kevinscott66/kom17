"""Copy for the contact page, in RU and EN.

Held here rather than in :mod:`telegram_invite_bot.i18n` for the same
reason :mod:`telegram_invite_bot.cms.home.content` is: ``t()`` renders
its values as *Telegram* markup, and this page is HTML. The short chrome
labels (the nav entry, the page kind) do come from i18n — see
:mod:`telegram_invite_bot.cms.contact.router`.

Every string in :class:`ContactCopy` is plain text. The renderer escapes
at the HTML boundary, so a copy edit can never open an injection; the
one exception is ``intro_md``, which is Markdown for the same renderer
the rest of the site uses.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContactCopy:
    """The contact page in one language.

    The ``err_*`` strings are the complete set of outcomes the form can
    report. They are deliberately specific — "too long" names the limit,
    "throttled" says to come back later — because the sender has no
    other channel to ask what went wrong, which is the whole premise of
    the page.
    """

    title: str
    lede: str
    intro_md: str
    label_message: str
    hint_message: str
    label_reply_to: str
    hint_reply_to: str
    submit_label: str
    privacy_note: str
    ok_title: str
    ok_body: str
    #: Heading of the rejection panel. Every ``err_*`` sentence below
    #: appears under it, so it must read as a verdict on the submission
    #: rather than as the page's own title.
    err_title: str
    err_empty: str
    err_too_long: str
    err_reply_too_long: str
    err_throttled: str
    err_undeliverable: str


_RU = ContactCopy(
    title="Связаться с оператором",
    lede=(
        "Форма для тех, кому нужно обратиться к оператору Сервиса напрямую — "
        "без установки бота и без аккаунта в Telegram."
    ),
    intro_md="""
# Когда писать сюда

Если вы пользуетесь ботом, быстрее и надёжнее команда `/support`: обращение
получает номер, и по нему видно статус. Эта форма — для всех остальных случаев:
запрос от банка-эквайера или регулятора, вопрос о персональных данных,
досудебная претензия, сообщение об уязвимости.

Сообщение уходит оператору в Telegram сразу после отправки. Ответ придёт на тот
контакт, который вы укажете ниже.
""",
    label_message="Сообщение",
    hint_message="До 2000 символов. Опишите вопрос целиком — переписываться через форму нельзя.",
    label_reply_to="Куда ответить",
    hint_reply_to="E-mail, телефон или @username в Telegram — без него ответить не получится.",
    submit_label="Отправить",
    privacy_note=(
        "Отправляя форму, вы соглашаетесь на обработку указанных в ней данных "
        "для ответа на обращение. Текст и контакт передаются оператору в Telegram; "
        "IP-адрес и данные браузера не сохраняются и не передаются."
    ),
    ok_title="Обращение отправлено",
    ok_body=(
        "Оператор получил сообщение. Ответ придёт на указанный контакт — "
        "обычно в течение 3 рабочих дней."
    ),
    err_title="Обращение не отправлено",
    err_empty="Заполните оба поля: и сообщение, и контакт для ответа.",
    err_too_long="Сообщение длиннее 2000 символов. Сократите его и отправьте снова.",
    err_reply_too_long="Контакт для ответа длиннее 200 символов — похоже, это опечатка.",
    err_throttled=(
        "Слишком много обращений подряд. Подождите несколько минут и попробуйте ещё раз."
    ),
    err_undeliverable=(
        "Не удалось доставить обращение — сбой на нашей стороне. "
        "Попробуйте ещё раз через несколько минут."
    ),
)


_EN = ContactCopy(
    title="Contact the operator",
    lede=(
        "A form for anyone who needs to reach the Service's operator directly — "
        "without installing the bot and without a Telegram account."
    ),
    intro_md="""
# When to use this

If you already use the bot, the `/support` command is faster and safer: the
request gets a number and you can track its status. This form is for everything
else — a request from an acquiring bank or a regulator, a personal-data
question, a formal claim, a vulnerability report.

The message reaches the operator on Telegram the moment you send it. The reply
goes to whatever contact you leave below.
""",
    label_message="Message",
    hint_message="Up to 2000 characters. Say the whole thing — the form is not a conversation.",
    label_reply_to="Where to reply",
    hint_reply_to=(
        "An e-mail, a phone number or a Telegram @username — without one there is no reply."
    ),
    submit_label="Send",
    privacy_note=(
        "By sending this form you consent to the processing of the data in it for "
        "the purpose of answering you. The text and the contact are passed to the "
        "operator on Telegram; your IP address and browser details are neither "
        "stored nor forwarded."
    ),
    ok_title="Message sent",
    ok_body=(
        "The operator has received it. The reply goes to the contact you left — "
        "usually within 3 business days."
    ),
    err_title="Message not sent",
    err_empty="Both fields are required: the message and a contact to reply to.",
    err_too_long="The message is longer than 2000 characters. Shorten it and send again.",
    err_reply_too_long="The reply contact is longer than 200 characters — that looks like a typo.",
    err_throttled="Too many messages in a row. Wait a few minutes and try again.",
    err_undeliverable=(
        "The message could not be delivered — a fault on our side. "
        "Please try again in a few minutes."
    ),
)


def copy_for(lang: str) -> ContactCopy:
    """The contact page in ``lang``; anything but ``en`` reads Russian."""
    return _EN if lang == "en" else _RU
