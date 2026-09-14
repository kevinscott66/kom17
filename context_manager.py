# -*- coding: utf-8 -*-
"""
Менеджер контекста чата для маршрутизации команд.
Этап 1: только данные и функции определения контекста.
Не изменяет bot.py, не регистрирует хэндлеры.
"""

from typing import Optional, Any


class ChatContext:
    """Типы контекста чата для маршрутизации."""
    PRIVATE = 'private'
    GROUP = 'group'
    SUPERGROUP = 'supergroup'

    @staticmethod
    def get_context(chat: Any) -> str:
        """Определяет контекст чата по объекту telebot.types.Chat."""
        if chat is None:
            return ChatContext.PRIVATE
        chat_type = getattr(chat, 'type', None) or ''
        if chat_type in ('group', 'supergroup'):
            return 'group'  # для роутера оба типа — группа
        return ChatContext.PRIVATE


class CommandRouter:
    """
    Маршрутизатор команд на основе контекста и роли.
    Использует COMMAND_REGISTRY из command_aliases (импорт при вызове, чтобы избежать циклических зависимостей).
    """

    # Команды, которые обрабатываются везде одинаково (точки входа)
    UNIVERSAL_COMMANDS = {'start', 'help', 'h', 'commands'}

    @staticmethod
    def route(command: str, context: str, user_role: str, registry: Optional[dict] = None) -> str:
        """
        Определяет, как обрабатывать команду.
        :param command: каноническое имя команды (без слэша), например 'balance'
        :param context: 'private' или 'group'
        :param user_role: 'developer' | 'group_admin' | 'user' | 'guest'
        :param registry: COMMAND_REGISTRY из command_aliases (опционально)
        :return: 'universal' | 'group_public' | 'private' | 'redirect_to_private'
        """
        if registry is None:
            try:
                from command_aliases import COMMAND_REGISTRY
                registry = COMMAND_REGISTRY
            except ImportError:
                return 'private'

        cmd_lower = (command or '').strip().lstrip('/').lower()
        if not cmd_lower:
            return 'private'

        if cmd_lower in CommandRouter.UNIVERSAL_COMMANDS:
            return 'universal'

        entry = registry.get(cmd_lower)
        if not entry:
            # по каноническому имени не нашли — ищем по алиасу
            for canon, data in registry.items():
                aliases = data.get('aliases') or []
                if cmd_lower in aliases or f'/{cmd_lower}' in [a.lstrip('/') for a in aliases]:
                    entry = data
                    break
            if not entry:
                return 'private'

        contexts = entry.get('contexts', ['private'])
        if context == 'group':
            if 'group' in contexts:
                return 'group_public'
            return 'redirect_to_private'
        return 'private'
