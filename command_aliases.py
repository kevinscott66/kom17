# -*- coding: utf-8 -*-
"""
Единая система алиасов команд.
Этап 1: только данные (COMMAND_REGISTRY) и get_canonical_command().
Не регистрирует хэндлеры, не вызывает функции бота.
"""

from typing import Optional, Dict, Any, List

# Реестр: каноническое_имя -> { handler, aliases (без слэша для сравнения), contexts, description? }
# Все алиасы храним в нижнем регистре без ведущего / для единообразного сравнения.
COMMAND_REGISTRY: Dict[str, Dict[str, Any]] = {
    # --- Универсальные точки входа ---
    'start': {
        'handler': 'cmd_start',
        'aliases': ['start'],
        'contexts': ['private', 'group'],
        'description': 'Точка входа, приветствие, deep-links',
    },
    'help': {
        'handler': 'cmd_help',
        'aliases': ['help', 'h', 'commands'],
        'contexts': ['private', 'group'],
        'description': 'Справка и список команд',
    },
    # --- Время, город, погода ---
    'time': {
        'handler': 'cmd_time',
        'aliases': ['time', 'время', 'time_msk'],
        'contexts': ['private', 'group'],
        'description': 'Время по Москве / в городе',
    },
    'city': {
        'handler': 'cmd_city',
        'aliases': ['city', 'город'],
        'contexts': ['private', 'group'],
        'description': 'Показать/установить город',
    },
    'timezone': {
        'handler': 'cmd_timezone',
        'aliases': ['timezone', 'часовой_пояс', 'tz'],
        'contexts': ['private'],
        'description': 'Часовой пояс',
    },
    'weather': {
        'handler': 'cmd_weather',
        'aliases': ['weather', 'погода'],
        'contexts': ['private', 'group'],
        'description': 'Погода',
    },
    'forecast': {
        'handler': 'cmd_forecast',
        'aliases': ['forecast', 'прогноз'],
        'contexts': ['private', 'group'],
        'description': 'Прогноз на 7 дней',
    },
    # --- AI / развлечения (команды) ---
    'joke': {
        'handler': 'cmd_joke',
        'aliases': ['joke', 'шутка', 'анекдот'],
        'contexts': ['private', 'group'],
        'description': 'Шутка от Кома',
    },
    'joke18': {
        'handler': 'cmd_joke18',
        'aliases': ['joke18', 'шутка18', 'анекдот18'],
        'contexts': ['private', 'group'],
        'description': 'Шутка 18+ (мемы/острый юмор)',
    },
    'quote': {
        'handler': 'cmd_quote',
        'aliases': ['quote', 'цитата'],
        'contexts': ['private', 'group'],
        'description': 'Цитата от Кома',
    },
    'calc': {
        'handler': 'cmd_calc',
        'aliases': ['calc', 'калькулятор'],
        'contexts': ['private', 'group'],
        'description': 'Калькулятор',
    },
    # --- Игры ---
    'roll': {
        'handler': 'cmd_roll',
        'aliases': ['roll', 'кубик'],
        'contexts': ['private', 'group'],
        'description': 'Бросок кубика',
    },
    'flip': {
        'handler': 'cmd_flip',
        'aliases': ['flip', 'монетка'],
        'contexts': ['private', 'group'],
        'description': 'Подброс монетки',
    },
    'roulette': {
        'handler': 'cmd_roulette',
        'aliases': ['roulette'],
        'contexts': ['private', 'group'],
        'description': 'Рулетка',
    },
    'dice': {
        'handler': 'cmd_dice',
        'aliases': ['dice', 'кубик'],
        'contexts': ['private', 'group'],
        'description': 'Игральный кубик',
    },
    'duel': {
        'handler': 'cmd_duel',
        'aliases': ['duel'],
        'contexts': ['private', 'group'],
        'description': 'Дуэль',
    },
    'duel_stats': {
        'handler': 'cmd_duel_stats',
        'aliases': ['duel_stats'],
        'contexts': ['private', 'group'],
        'description': 'Статистика дуэлей',
    },
    'cpc': {
        'handler': 'cmd_cpc',
        'aliases': ['cpc', 'кнб'],
        'contexts': ['group'],
        'description': 'Камень, ножницы, бумага на ставки',
    },
    'accept': {
        'handler': 'cmd_accept',
        'aliases': ['accept'],
        'contexts': ['private', 'group'],
        'description': 'Принять вызов КНБ (в ответ на сообщение бота)',
    },
    'decline': {
        'handler': 'cmd_decline',
        'aliases': ['decline'],
        'contexts': ['private', 'group'],
        'description': 'Отклонить вызов КНБ',
    },
    'cancel_game': {
        'handler': 'cmd_cancel_game',
        'aliases': ['cancel_game'],
        'contexts': ['private', 'group'],
        'description': 'Отменить текущую игру КНБ',
    },
    # --- Валюта и баланс ---
    'currency': {
        'handler': 'cmd_currency',
        'aliases': ['currency', 'валюта'],
        'contexts': ['private', 'group'],
        'description': 'Настройка валюты',
    },
    'rate': {
        'handler': 'cmd_rate',
        'aliases': ['rate', 'курс', 'курсы'],
        'contexts': ['private', 'group'],
        'description': 'Курсы валют',
    },
    'crypto': {
        'handler': 'cmd_crypto',
        'aliases': ['crypto', 'крипта', 'криптовалюта'],
        'contexts': ['private', 'group'],
        'description': 'Курсы крипты',
    },
    'convert': {
        'handler': 'cmd_convert',
        'aliases': ['convert', 'конверт', 'конвертация'],
        'contexts': ['private', 'group'],
        'description': 'Конвертация COM',
    },
    'balance': {
        'handler': 'cmd_balance',
        'aliases': ['balance', 'bal'],
        'contexts': ['private', 'group'],
        'description': 'Баланс',
    },
    'daily': {
        'handler': 'cmd_daily',
        'aliases': ['daily'],
        'contexts': ['private', 'group'],
        'description': 'Ежедневный бонус',
    },
    'buy': {
        'handler': 'cmd_buy',
        'aliases': ['buy'],
        'contexts': ['private'],
        'description': 'Покупка монет',
    },
    'send': {
        'handler': 'cmd_send',
        'aliases': ['send'],
        'contexts': ['private', 'group'],
        'description': 'Перевод монет',
    },
    'withdraw': {
        'handler': 'cmd_withdraw',
        'aliases': ['withdraw', 'вывод'],
        'contexts': ['private'],
        'description': 'Вывод монет',
    },
    'withdraw_status': {
        'handler': 'cmd_withdraw_status',
        'aliases': ['withdraw_status'],
        'contexts': ['private'],
        'description': 'Статус выводов',
    },
    # --- Топ и рейтинг ---
    'top': {
        'handler': 'cmd_top',
        'aliases': ['top'],
        'contexts': ['private', 'group'],
        'description': 'Топ игроков',
    },
    # --- Браки и отношения ---
    'marry': {
        'handler': 'cmd_marry',
        'aliases': ['marry', 'брак', 'жениться'],
        'contexts': ['private', 'group'],
        'description': 'Предложить брак',
    },
    'marry_accept': {
        'handler': 'cmd_marry_accept_decline',
        'aliases': ['marry_accept'],
        'contexts': ['private', 'group'],
        'description': 'Принять брак',
    },
    'marry_decline': {
        'handler': 'cmd_marry_accept_decline',
        'aliases': ['marry_decline'],
        'contexts': ['private', 'group'],
        'description': 'Отклонить брак',
    },
    'marriage': {
        'handler': 'cmd_marriage',
        'aliases': ['marriage', 'my_marriage', 'брак_статус'],
        'contexts': ['private', 'group'],
        'description': 'Мой брак',
    },
    'divorce': {
        'handler': 'cmd_divorce',
        'aliases': ['divorce', 'развод'],
        'contexts': ['private', 'group'],
        'description': 'Расторгнуть брак',
    },
    'marry_top_on': {
        'handler': 'cmd_marry_top_on',
        'aliases': ['marry_top_on', 'брак_рейтинг_вкл'],
        'contexts': ['private', 'group'],
        'description': 'Включить брак в рейтинг',
    },
    'marry_top_off': {
        'handler': 'cmd_marry_top_off',
        'aliases': ['marry_top_off', 'брак_рейтинг_выкл'],
        'contexts': ['private', 'group'],
        'description': 'Выключить брак из рейтинга',
    },
    'marry_extend': {
        'handler': 'cmd_marry_extend',
        'aliases': ['marry_extend', 'брак_продлить'],
        'contexts': ['private', 'group'],
        'description': 'Продлить брак',
    },
    'marry_auto_divorce': {
        'handler': 'cmd_marry_auto_divorce',
        'aliases': ['marry_auto_divorce', 'брак_режим_развода'],
        'contexts': ['private', 'group'],
        'description': 'Режим развода',
    },
    'marry_other': {
        'handler': 'cmd_marry_other',
        'aliases': ['marry_other', 'твой_брак'],
        'contexts': ['private', 'group'],
        'description': 'Брак другого пользователя',
    },
    'marriages': {
        'handler': 'cmd_marriages',
        'aliases': ['marriages', 'браки', 'пары'],
        'contexts': ['private', 'group'],
        'description': 'Список пар',
    },
    'relationship': {
        'handler': 'cmd_relationship',
        'aliases': ['relationship', 'rel', 'отношения', 'в_отношениях'],
        'contexts': ['private', 'group'],
        'description': 'Отношения',
    },
    'breakup': {
        'handler': 'cmd_breakup',
        'aliases': ['breakup', 'расстаться'],
        'contexts': ['private', 'group'],
        'description': 'Прекратить отношения',
    },
    'relations': {
        'handler': 'cmd_relations',
        'aliases': ['relations', 'отношения_список'],
        'contexts': ['private', 'group'],
        'description': 'Список отношений',
    },
    'rp_commands': {
        'handler': 'cmd_rp_commands',
        'aliases': ['rp_commands', 'рп_команды'],
        'contexts': ['private', 'group'],
        'description': 'РП-команды',
    },
    # --- Магазин и инвентарь ---
    'shop': {
        'handler': 'cmd_shop',
        'aliases': ['shop'],
        'contexts': ['private', 'group'],
        'description': 'Магазин',
    },
    'inventory': {
        'handler': 'cmd_inventory',
        'aliases': ['inventory', 'inv'],
        'contexts': ['private', 'group'],
        'description': 'Инвентарь',
    },
    'achievements': {
        'handler': 'cmd_achievements',
        'aliases': ['achievements', 'ach'],
        'contexts': ['private', 'group'],
        'description': 'Достижения',
    },
    # --- Настройки и язык ---
    'lang': {
        'handler': 'cmd_lang',
        'aliases': ['lang', 'language', 'язык'],
        'contexts': ['private', 'group'],
        'description': 'Язык бота',
    },
    'settings': {
        'handler': 'cmd_settings',
        'aliases': ['settings', 'настройки'],
        'contexts': ['private'],
        'description': 'Настройки пользователя',
    },
    'mygroups': {
        'handler': 'cmd_mygroups',
        'aliases': ['mygroups', 'мои_группы'],
        'contexts': ['private'],
        'description': 'Мои группы',
    },
    'nick': {
        'handler': 'cmd_nick',
        'aliases': ['nick', 'setnick', 'ник', 'никнейм'],
        'contexts': ['group'],
        'description': 'Локальный ник в группе',
    },
    # --- Донаты и казна ---
    'donate': {
        'handler': 'cmd_donate',
        'aliases': ['donate'],
        'contexts': ['group'],
        'description': 'Донат в группу',
    },
    'donaters': {
        'handler': 'cmd_donaters',
        'aliases': ['donaters', 'донатеры'],
        'contexts': ['group'],
        'description': 'Топ донатеров',
    },
    'rating': {
        'handler': 'cmd_rating',
        'aliases': ['rating', 'рейтинг'],
        'contexts': ['private'],
        'description': 'Рейтинг групп',
    },
    'mydonates': {
        'handler': 'cmd_mydonates',
        'aliases': ['mydonates', 'мои_донаты'],
        'contexts': ['private'],
        'description': 'Мои донаты',
    },
    'groupstats': {
        'handler': 'cmd_groupstats',
        'aliases': ['groupstats', 'статистика_группы', 'group_treasury', 'казна_группы'],
        'contexts': ['private', 'group'],
        'description': 'Статистика/казна группы',
    },
    'group_pay': {
        'handler': 'cmd_group_pay',
        'aliases': ['group_pay', 'выплата_из_казны'],
        'contexts': ['group'],
        'description': 'Выплата из казны',
    },
    # --- Рефералы ---
    'referral': {
        'handler': 'cmd_referral',
        'aliases': ['referral', 'реферал', 'реферальная_ссылка'],
        'contexts': ['private'],
        'description': 'Реферальная ссылка',
    },
    'referrals': {
        'handler': 'cmd_referrals',
        'aliases': ['referrals', 'рефералы', 'мои_рефералы'],
        'contexts': ['private'],
        'description': 'Мои рефералы',
    },
    'commission': {
        'handler': 'cmd_commission',
        'aliases': ['commission', 'комиссии', 'мои_комиссии'],
        'contexts': ['private'],
        'description': 'Мои комиссии',
    },
    # --- Разработчик / админ ---
    'dev': {
        'handler': 'cmd_dev',
        'aliases': ['dev', 'developer'],
        'contexts': ['private'],
        'description': 'Панель разработчика',
    },
    'admin': {
        'handler': 'cmd_admin',
        'aliases': ['admin'],
        'contexts': ['private'],
        'description': 'Админ-панель групп',
    },
    'admin_help': {
        'handler': 'cmd_admin_help',
        'aliases': ['admin_help', 'owner_help'],
        'contexts': ['private'],
        'description': 'Помощь владельцу',
    },
    'set_admin_password': {
        'handler': 'cmd_set_admin_password',
        'aliases': ['set_admin_password'],
        'contexts': ['private'],
        'description': 'Установить пароль админки',
    },
    'create_check': {
        'handler': 'cmd_create_check',
        'aliases': ['create_check', 'check'],
        'contexts': ['private'],
        'description': 'Создать чек',
    },
    'add_required_chat': {
        'handler': 'cmd_add_required_chat',
        'aliases': ['add_required_chat'],
        'contexts': ['private'],
        'description': 'Добавить обязательную подписку',
    },
    'deploy': {
        'handler': 'cmd_deploy',
        'aliases': ['deploy'],
        'contexts': ['private'],
        'description': 'Деплой',
    },
    'reload': {
        'handler': 'cmd_reload',
        'aliases': ['reload'],
        'contexts': ['private'],
        'description': 'Перезагрузка',
    },
    'logs': {
        'handler': 'cmd_logs',
        'aliases': ['logs'],
        'contexts': ['private'],
        'description': 'Логи',
    },
    'backup': {
        'handler': 'cmd_backup',
        'aliases': ['backup'],
        'contexts': ['private'],
        'description': 'Бэкап',
    },
    'botstats': {
        'handler': 'cmd_botstats',
        'aliases': ['botstats'],
        'contexts': ['private'],
        'description': 'Статистика бота',
    },
    'sql': {
        'handler': 'cmd_sql',
        'aliases': ['sql'],
        'contexts': ['private'],
        'description': 'SQL',
    },
    'clear_cache': {
        'handler': 'cmd_clear_cache',
        'aliases': ['clear_cache'],
        'contexts': ['private'],
        'description': 'Очистка кеша',
    },
    'broadcast': {
        'handler': 'cmd_broadcast',
        'aliases': ['broadcast'],
        'contexts': ['private'],
        'description': 'Рассылка',
    },
    'test_logs': {
        'handler': 'cmd_test_logs',
        'aliases': ['test_logs'],
        'contexts': ['private'],
        'description': 'Тест логов',
    },
    'clearlogs': {
        'handler': 'cmd_clearlogs',
        'aliases': ['clearlogs'],
        'contexts': ['private'],
        'description': 'Очистка логов',
    },
    'maintenance': {
        'handler': 'cmd_maintenance',
        'aliases': ['maintenance'],
        'contexts': ['private'],
        'description': 'Режим обслуживания',
    },
    'admin_withdrawals': {
        'handler': 'cmd_admin_withdrawals',
        'aliases': ['admin_withdrawals'],
        'contexts': ['private'],
        'description': 'Заявки на вывод',
    },
    # --- Модерация ---
    'warn': {
        'handler': 'cmd_warn',
        'aliases': ['warn'],
        'contexts': ['group'],
        'description': 'Предупреждение',
    },
    'unwarn': {
        'handler': 'cmd_unwarn',
        'aliases': ['unwarn', 'снять_предупреждение'],
        'contexts': ['group'],
        'description': 'Снять предупреждение',
    },
    'warnings': {
        'handler': 'cmd_warnings',
        'aliases': ['warnings', 'варны', 'предупреждения'],
        'contexts': ['group'],
        'description': 'Список предупреждений',
    },
    'mute': {
        'handler': 'cmd_mute',
        'aliases': ['mute'],
        'contexts': ['group'],
        'description': 'Мут',
    },
    'unmute': {
        'handler': 'cmd_unmute',
        'aliases': ['unmute', 'размут'],
        'contexts': ['group'],
        'description': 'Размут',
    },
    'ban': {
        'handler': 'cmd_ban',
        'aliases': ['ban'],
        'contexts': ['group'],
        'description': 'Бан',
    },
    'kick': {
        'handler': 'cmd_kick',
        'aliases': ['kick', 'кик'],
        'contexts': ['group'],
        'description': 'Кик',
    },
    'unban': {
        'handler': 'cmd_unban',
        'aliases': ['unban', 'разбан'],
        'contexts': ['group'],
        'description': 'Разбан',
    },
    'rules': {
        'handler': 'cmd_rules',
        'aliases': ['rules', 'правила'],
        'contexts': ['group'],
        'description': 'Правила',
    },
    'setrules': {
        'handler': 'cmd_setrules',
        'aliases': ['setrules', 'установить_правила'],
        'contexts': ['group'],
        'description': 'Установить правила',
    },
    'clear': {
        'handler': 'cmd_clear',
        'aliases': ['clear', 'очистить', 'очистка'],
        'contexts': ['group'],
        'description': 'Очистка чата',
    },
    'pin': {
        'handler': 'cmd_pin',
        'aliases': ['pin', 'закрепить'],
        'contexts': ['group'],
        'description': 'Закрепить',
    },
    'unpin': {
        'handler': 'cmd_unpin',
        'aliases': ['unpin', 'открепить'],
        'contexts': ['group'],
        'description': 'Открепить',
    },
    'voice_vip': {
        'handler': 'cmd_voice_vip',
        'aliases': ['voice_vip'],
        'contexts': ['private'],
        'description': 'Голос VIP',
    },
    'voice_settings': {
        'handler': 'cmd_voice_settings',
        'aliases': ['voice_settings', 'voice_settings_ru'],
        'contexts': ['private'],
        'description': 'Настройки голоса',
    },
    'groupadmin': {
        'handler': 'cmd_groupadmin',
        'aliases': ['groupadmin', 'group_admin', 'управлениегруппой'],
        'contexts': ['private', 'group'],
        'description': 'Управление группой',
    },
    # --- Поддержка и FAQ ---
    'faq': {
        'handler': 'cmd_faq',
        'aliases': ['faq'],
        'contexts': ['private', 'group'],
        'description': 'FAQ',
    },
    'support': {
        'handler': 'cmd_support',
        'aliases': ['support', 'ticket'],
        'contexts': ['private'],
        'description': 'Поддержка',
    },
    'feedback': {
        'handler': 'cmd_feedback',
        'aliases': ['feedback'],
        'contexts': ['private'],
        'description': 'Обратная связь',
    },
    # --- Реклама ---
    'ad': {
        'handler': 'cmd_ad',
        'aliases': ['ad', 'ads', 'reklama'],
        'contexts': ['private', 'group'],
        'description': 'Реклама',
    },
    # --- AI (Kom) ---
    'ai': {
        'handler': 'cmd_ai',
        'aliases': ['ai', 'ask', 'chat'],
        'contexts': ['private', 'group'],
        'description': 'ИИ Ком',
    },
    'reset': {
        'handler': 'cmd_ai_reset',
        'aliases': ['reset'],
        'contexts': ['private', 'group'],
        'description': 'Сброс контекста Ком',
    },
    'ai_limits': {
        'handler': 'cmd_ai_limits',
        'aliases': ['ai_limits'],
        'contexts': ['private', 'group'],
        'description': 'Лимиты Ком',
    },
    # --- Профиль и инфо ---
    'profile': {
        'handler': 'cmd_profile',
        'aliases': ['profile', 'info', 'инфо'],
        'contexts': ['private', 'group'],
        'description': 'Профиль',
    },
    'chatstats': {
        'handler': 'cmd_chatstats',
        'aliases': ['chatstats', 'cstats'],
        'contexts': ['private', 'group'],
        'description': 'Статистика чата',
    },
    'stats': {
        'handler': 'cmd_user_stats',
        'aliases': ['stats'],
        'contexts': ['private', 'group'],
        'description': 'Статистика пользователя',
    },
    'whoami': {
        'handler': 'cmd_whoami',
        'aliases': ['whoami', 'me'],
        'contexts': ['private', 'group'],
        'description': 'Кто я',
    },
    'chatinfo': {
        'handler': 'cmd_chatinfo',
        'aliases': ['chatinfo'],
        'contexts': ['private', 'group'],
        'description': 'Инфо о чате',
    },
    'ping': {
        'handler': 'cmd_ping',
        'aliases': ['ping'],
        'contexts': ['private', 'group'],
        'description': 'Пинг',
    },
    'botcheck': {
        'handler': 'cmd_botcheck',
        'aliases': ['botcheck', 'alive'],
        'contexts': ['private', 'group'],
        'description': 'Проверка бота',
    },
    'debug': {
        'handler': 'cmd_debug',
        'aliases': ['debug'],
        'contexts': ['private'],
        'description': 'Отладка',
    },
    'version_check': {
        'handler': 'cmd_version_check',
        'aliases': ['version_check'],
        'contexts': ['private'],
        'description': 'Проверка версии',
    },
    'staff_me': {
        'handler': 'cmd_staff_me',
        'aliases': ['staff_me'],
        'contexts': ['private'],
        'description': 'Персонал',
    },
    'transfer_rights': {
        'handler': 'cmd_transfer_rights',
        'aliases': ['transfer_rights'],
        'contexts': ['private'],
        'description': 'Передача прав',
    },
    'cfg_button': {
        'handler': 'cmd_cfg_button',
        'aliases': ['cfg_button', 'setbutton'],
        'contexts': ['private'],
        'description': 'Настройка кнопки',
    },
    'alias': {
        'handler': 'cmd_alias',
        'aliases': ['alias', 'aliases'],
        'contexts': ['private'],
        'description': 'Алиасы команд',
    },
    'modcfg': {
        'handler': 'cmd_modcfg',
        'aliases': ['modcfg'],
        'contexts': ['private'],
        'description': 'Настройки модерации',
    },
    'cmdcfg': {
        'handler': 'cmd_cmdcfg',
        'aliases': ['cmdcfg', 'cmdaccess'],
        'contexts': ['private'],
        'description': 'Настройки команд',
    },
    'top_activity': {
        'handler': 'cmd_top_activity',
        'aliases': ['top_activity', 'topactive'],
        'contexts': ['private', 'group'],
        'description': 'Топ по активности',
    },
    'test_emoji': {
        'handler': 'cmd_test_emoji',
        'aliases': ['test_emoji'],
        'contexts': ['private'],
        'description': 'Тест эмодзи',
    },
    'rankperm': {
        'handler': 'cmd_perm',
        'aliases': ['perm', 'rankperm'],
        'contexts': ['private', 'group'],
        'description': 'Права по рангу',
    },
}


def get_canonical_command(text: Optional[str]) -> Optional[str]:
    """
    Преобразует любой алиас (текст команды с или без /) в каноническое имя из реестра.
    :param text: строка сообщения, например "/balance" или "баланс"
    :return: каноническое имя, например 'balance', или None
    """
    if not text or not text.strip():
        return None
    cmd = text.strip().split(maxsplit=1)[0].lower().lstrip('/')
    if not cmd:
        return None
    for canon, data in COMMAND_REGISTRY.items():
        aliases = data.get('aliases') or []
        if cmd in aliases or cmd == canon:
            return canon
    return None


def get_handler_name(canonical: str) -> Optional[str]:
    """Возвращает имя функции-обработчика для канонической команды."""
    entry = COMMAND_REGISTRY.get(canonical)
    if not entry:
        return None
    return entry.get('handler')


# ---------------------------------------------------------------------------
# Этап 1: полные списки для справки (извлечены из bot.py, не менять логику)
# ---------------------------------------------------------------------------

ALL_COMMANDS_LIST: List[str] = [
    "ach", "achievements", "ad", "add_required_chat", "admin", "admin_help", "admin_withdrawals",
    "ads", "ai", "ai_limits", "alias", "aliases", "alive", "ask", "backup", "bal", "balance",
    "ban", "botcheck", "botstats", "breakup", "broadcast", "buy", "calc", "cfg_button", "chat",
    "chatinfo", "chatstats", "check", "city", "clear", "clear_cache", "clearlogs", "cmdaccess",
    "cmdcfg", "commands", "commission", "convert", "create_check", "crypto", "cstats", "currency",
    "daily", "debug", "deploy", "dev", "developer", "dice", "divorce", "donate", "donaters",
    "accept", "cancel_game", "cpc", "decline", "duel", "duel_stats", "faq", "feedback", "flip", "forecast", "group_admin", "group_pay",
    "group_treasury", "groupadmin", "groupstats", "h", "help", "info", "inv", "inventory",
    "joke", "joke18", "kick", "lang", "language", "logs", "maintenance", "marriage", "marriages", "marry",
    "marry_accept", "marry_auto_divorce", "marry_decline", "marry_extend", "marry_other",
    "marry_top_off", "marry_top_on", "me", "modcfg", "mute", "my_marriage", "mydonates",
    "mygroups", "nick", "owner_help", "perm", "pin", "ping", "profile", "quote", "rankperm",
    "rate", "rating", "referral", "referrals", "reklama", "rel", "relations", "relationship",
    "reload", "reset", "roll", "roulette", "rp_commands", "rules", "send", "set_admin_password",
    "setbutton", "setnick", "setrules", "settings", "shop", "sql", "staff_me", "start", "stats",
    "support", "test_emoji", "test_logs", "ticket", "time", "time_msk", "timezone", "top",
    "top_activity", "topactive", "transfer_rights", "tz", "unban", "unmute", "unpin", "unwarn",
    "version_check", "voice_settings", "voice_settings_ru", "voice_vip", "warn", "warnings",
    "weather", "whoami", "withdraw", "withdraw_status",
    "анекдот", "анекдот18", "брак", "брак_продлить", "брак_режим_развода", "брак_рейтинг_вкл", "брак_рейтинг_выкл",
    "брак_статус", "браки", "в_отношениях", "валюта", "варны", "время", "вывод", "выплата_из_казны",
    "город", "донатеры", "жениться", "закрепить", "инфо", "казна_группы", "калькулятор", "кик",
    "комиссии", "конверт", "конвертация", "крипта", "криптовалюта", "кубик", "кнб", "курс", "курсы",
    "мои_группы", "мои_донаты", "мои_комиссии", "мои_рефералы", "монетка", "настройки", "ник",
    "никнейм", "открепить", "отношения", "отношения_список", "очистить", "очистка", "пары",
    "погода", "правила", "предупреждения", "прогноз", "разбан", "развод", "размут", "расстаться",
    "рейтинг", "реферал", "рефералы", "реферальная_ссылка", "рп_команды", "снять_предупреждение",
    "статистика_группы", "твой_брак", "управлениегруппой", "установить_правила", "цитата",
    "часовой_пояс", "шутка", "шутка18", "язык",
]

ALL_CALLBACK_DATA_LITERALS: List[str] = [
    "achievements_add", "achievements_list", "achievements_stats", "ad_refresh_stats", "ad_request_cancel",
    "admin_achievements", "admin_back", "admin_backups", "admin_bot_groups", "admin_broadcast",
    "admin_constructor", "admin_content", "admin_economy", "admin_economy_shop", "admin_edit_button",
    "admin_edit_link", "admin_edit_welcome", "admin_games", "admin_group_features", "admin_lang_en",
    "admin_lang_ru", "admin_logs", "admin_logs_backups", "admin_maintenance_toggle", "admin_moderation",
    "admin_moderation_back", "admin_moderation_games", "admin_open_current_group", "admin_panel_back",
    "admin_panel_switch_group", "admin_quick_daily_random", "admin_quick_msg_guard", "admin_refresh_withdrawals",
    "admin_reload", "admin_remove_preview", "admin_restart_bot", "admin_settings", "admin_shop",
    "admin_stats", "admin_system", "admin_telegraph_update", "admin_test_logs", "admin_testing",
    "admin_translate_missing_en", "admin_upload_photo", "admin_url_photo", "admin_watermark",
    "admin_welcome_preview", "ai_enter", "ai_exit", "ai_export", "ai_mode_chat", "ai_mode_code",
    "ai_mode_creative", "ai_mode_default", "ai_mode_expert", "ai_mode_help", "ai_mode_party",
    "ai_reset", "ai_save", "backup_cleanup", "backup_create", "backup_restore", "backup_restore_confirm",
    "broadcast_cancel", "broadcast_confirm", "broadcast_preview", "broadcast_send", "broadcast_type_photo",
    "broadcast_type_text", "broadcast_type_video", "buy_menu", "buy_menu_back", "check_sub",
    "check_subscriptions", "constructor_alias_add", "constructor_alias_del", "constructor_aliases",
    "constructor_button_text", "constructor_cmd_access", "constructor_emojis", "constructor_mod_set_maxwarn",
    "constructor_mod_set_mute", "constructor_mod_toggle_auto", "constructor_mod_toggle_autoban",
    "constructor_mod_toggle_profanity", "constructor_modcfg", "constructor_perm_rank_1", "constructor_perm_rank_2",
    "constructor_perm_rank_3", "constructor_perm_rank_4", "constructor_perm_rank_5", "constructor_perms",
    "crypto_btc", "crypto_ton", "crypto_usdt", "currency_menu", "custom_rub", "custom_usd",
    "dev_panel_quick", "economy_give_coins", "economy_set_daily_random_max", "economy_set_daily_random_min",
    "economy_set_daily_reward", "economy_set_developer_commission", "economy_set_max_streak",
    "economy_set_message_reward", "economy_set_msg_cooldown", "economy_set_msg_duplicate_window",
    "economy_set_msg_min_chars", "economy_set_msg_per_min", "economy_set_name",
    "economy_set_purchase_donation_group", "economy_set_referral_commission", "economy_set_streak_bonus",
    "economy_set_tax", "economy_settings", "economy_stats", "economy_toggle_daily_random",
    "economy_toggle_enabled", "features", "flip_choice_orel", "flip_choice_reshka", "games_dice_max",
    "games_dice_min", "games_duel_max", "games_duel_min", "games_duel_rounds", "games_limits",
    "games_roulette_max", "games_roulette_min", "games_set_ttl", "games_settings", "games_stats",
    "games_toggle_autodelete", "games_toggle_duels", "games_toggle_enabled", "gift_open_kom",
    "group_lang_en", "group_lang_ru", "group_welcome_balance", "group_welcome_daily", "group_welcome_faq",
    "group_welcome_help", "group_welcome_shop", "inv_back_to_start", "inv_faq", "inv_my_tickets",
    "inv_support_cancel", "lang_set_en", "lang_set_ru", "logs_clear", "logs_economy", "logs_errors",
    "logs_games", "logs_moderation", "logs_recent", "main_menu", "marriage_activity_back",
    "marriage_activity_menu", "marriage_history", "moderation_add_mod", "moderation_add_word",
    "moderation_mods", "moderation_remove_mod", "moderation_remove_word", "moderation_set_max_warns",
    "moderation_set_mute_duration", "moderation_set_strength", "moderation_settings", "moderation_stats",
    "moderation_toggle_auto", "moderation_toggle_autoban", "moderation_toggle_profanity", "moderation_words",
    "p2p_buy_menu", "p2p_create_sell", "p2p_currency_EUR", "p2p_currency_RUB", "p2p_currency_TON",
    "p2p_currency_UAH", "p2p_currency_USD", "p2p_currency_USDT", "p2p_my_orders", "p2p_my_trades",
    "p2p_order_book", "pay_crypto", "pay_international", "pay_rub", "pay_stars", "rating",
    "rating_current", "rel_activity_back", "rp18_once_no", "rp18_once_yes", "settings_auto_delete",
    "settings_auto_moderate", "settings_economy", "settings_games", "settings_language",
    "settings_service_notifications", "settings_stats", "shop_add_item", "shop_back_to_list",
    "shop_close", "shop_group_0", "shop_list_items", "shop_list_next_0", "shop_stats",
    "start_menu_balance", "start_menu_buy", "start_menu_commission", "start_menu_donations",
    "start_menu_games", "start_menu_group_settings", "start_menu_help", "start_menu_my_groups",
    "start_menu_profile", "start_menu_referrals", "start_menu_shop", "start_menu_stats",
    "start_menu_top", "start_tour", "stats_periods_config", "support_start", "test_exit_mode",
    "test_mode_group_admin", "test_mode_user", "user_settings", "user_settings_lang",
    "watermark_delete", "watermark_opacity", "watermark_pos_bottom_left", "watermark_pos_bottom_right",
    "watermark_pos_center", "watermark_pos_top_left", "watermark_pos_top_right", "watermark_position",
    "watermark_preview", "watermark_size", "watermark_toggle", "watermark_upload",
    "withdraw_card_rub", "withdraw_crypto", "withdraw_direct_menu", "withdraw_instant_menu",
    "withdraw_menu_back", "withdraw_p2p_menu", "withdraw_status",
]
