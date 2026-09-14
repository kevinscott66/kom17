"""DI container factory.

Use :func:`make_container` from process bootstrap (``app.py`` /
``__main__.py``). Tests can pass their own provider list to swap
implementations.
"""

from __future__ import annotations

from dishka import STRICT_VALIDATION, AsyncContainer, Provider, make_async_container

from telegram_invite_bot.di.providers import AppProvider, DBSessionProvider


def make_container(extra_providers: list[Provider] | None = None) -> AsyncContainer:
    providers: list[Provider] = [AppProvider(), DBSessionProvider()]
    if extra_providers:
        providers.extend(extra_providers)
    # STRICT_VALIDATION turns a duplicate provider key from a silent
    # last-one-wins into an ``ImplicitOverrideDetectedError`` at build
    # time. The five per-DB session factories all provided a bare
    # ``AsyncSession`` once and four of them were dead code nobody could
    # have noticed; the guard is what stops that recurring. A test (or a
    # caller) that deliberately replaces a factory must now say so with
    # ``@provide(override=True)``.
    return make_async_container(*providers, validation_settings=STRICT_VALIDATION)
