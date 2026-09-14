# -*- coding: utf-8 -*-
"""
Универсальный хэндлер команд (единая точка входа для текстовых команд).
Этап 2: режим логирования — логирует, как бы обработал команду, но НЕ ВЫПОЛНЯЕТ.
Старые хэндлеры обрабатывают сообщения первыми; сюда попадают только необработанные.
"""

import logging

from command_aliases import COMMAND_REGISTRY, get_canonical_command
from context_manager import ChatContext
from role_checker import UserRole

logger = logging.getLogger(__name__)


def universal_command_handler(message):
    """
    Логирует, как бы обработал команду (режим отладки).
    Срабатывает только для сообщений, дошедших до этого хэндлера (не перехваченных ранее).
    """
    if not message or not getattr(message, "text", None) or not str(message.text).strip().startswith("/"):
        return

    context = ChatContext.get_context(message.chat)
    chat_id = message.chat.id if context == "group" else None
    user_role = UserRole.get_role(message.from_user.id, chat_id)
    canon = get_canonical_command(message.text)

    raw_cmd = (message.text or "").strip().split(maxsplit=1)[0]

    if canon:
        cmd_data = COMMAND_REGISTRY.get(canon, {})
        expected_contexts = cmd_data.get("contexts", [])
        logger.info(
            "🔍 [CMD DEBUG] %s → %s | Контекст: %s, Роль: %s | Ожидаемые контексты: %s",
            raw_cmd,
            canon,
            context,
            user_role,
            expected_contexts,
        )
        if context not in expected_contexts:
            logger.warning("   ⚠️ Команда в неверном контексте!")
        # До нас дошли — значит ни один предыдущий хэндлер не обработал команду
        logger.warning("   ⚠️ Нет активного хэндлера!")
    else:
        logger.info(
            "🔍 [CMD DEBUG] %s → None | Контекст: %s, Роль: %s | Команда не найдена в реестре",
            raw_cmd,
            context,
            user_role,
        )
