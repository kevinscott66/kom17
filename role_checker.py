# -*- coding: utf-8 -*-
"""
Проверка прав (ролевая модель).
Этап 1: только структура. Функции is_group_admin, is_feature_enabled_for_chat и т.д.
должны передаваться из bot.py при вызове (инъекция зависимостей), чтобы избежать циклического импорта.
"""

from typing import Optional, Callable, Any

# Типы ролей
ROLE_DEVELOPER = 'developer'
ROLE_GROUP_ADMIN = 'group_admin'
ROLE_USER = 'user'
ROLE_GUEST = 'guest'


class UserRole:
    """
    Определяет роль пользователя в контексте чата.
    Все зависимости (is_admin, get_user_manageable_groups, is_user_registered) передаются
    из bot.py при первом использовании через set_dependencies().
    """

    _is_developer: Optional[Callable[[int], bool]] = None
    _is_group_admin: Optional[Callable[[int, int], bool]] = None
    _is_user_registered: Optional[Callable[[int], bool]] = None
    _is_feature_enabled: Optional[Callable[[int, str, Optional[int]], bool]] = None

    @classmethod
    def set_dependencies(
        cls,
        is_developer: Callable[[int], bool],
        is_group_admin: Callable[[int, int], bool],
        is_user_registered: Callable[[int], bool],
        is_feature_enabled: Optional[Callable[[int, str, Optional[int]], bool]] = None,
    ) -> None:
        """Вызывается из bot.py при инициализации для инъекции зависимостей."""
        cls._is_developer = is_developer
        cls._is_group_admin = is_group_admin
        cls._is_user_registered = is_user_registered
        cls._is_feature_enabled = is_feature_enabled

    @classmethod
    def get_role(cls, user_id: int, chat_id: Optional[int] = None) -> str:
        """
        Определяет роль пользователя в контексте.
        :param user_id: ID пользователя
        :param chat_id: ID чата (для проверки админства в группе)
        :return: 'developer' | 'group_admin' | 'user' | 'guest'
        """
        if cls._is_developer and cls._is_developer(user_id):
            return ROLE_DEVELOPER
        if chat_id and cls._is_group_admin and cls._is_group_admin(user_id, chat_id):
            return ROLE_GROUP_ADMIN
        if cls._is_user_registered and cls._is_user_registered(user_id):
            return ROLE_USER
        return ROLE_GUEST

    @classmethod
    def can_access_feature(
        cls,
        user_id: int,
        feature: str,
        chat_id: Optional[int] = None,
    ) -> bool:
        """
        Проверяет доступ к функции (с учётом feature toggles в группах).
        Разработчик может всё. В группах проверяется is_feature_enabled_for_chat.
        """
        if cls._is_developer and cls._is_developer(user_id):
            return True
        if chat_id and cls._is_feature_enabled:
            return cls._is_feature_enabled(chat_id, feature, user_id)
        if cls.get_role(user_id, chat_id) in (ROLE_USER, ROLE_GROUP_ADMIN):
            return True
        return False
