"""Коллекторы источников: сбор, нормализация, фабрика.

Тяжёлые модули подгружаются лениво (PEP 562). Это не украшательство: bot.sender
и digest нуждаются только в collectors.http, и отсутствие, скажем, feedparser
не должно валить запуск сервиса и доставку дайджеста — ошибка обязана возникнуть
там, где реально нужна эта библиотека.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from . import http, normalize

__all__ = [
    "http",
    "normalize",
    "base",
    "api_collector",
    "rss_collector",
    "github_collector",
    "factory",
    "BaseCollector",
    "Batch",
    "CollectorError",
    "dig",
    "dig_list",
    "APICollector",
    "RSSCollector",
    "GitHubCollector",
    "get_collector",
]

# имя атрибута -> (модуль, атрибут; None означает сам модуль)
_LAZY: dict[str, tuple[str, str | None]] = {
    "base": ("base", None),
    "api_collector": ("api_collector", None),
    "rss_collector": ("rss_collector", None),
    "github_collector": ("github_collector", None),
    "factory": ("factory", None),
    "BaseCollector": ("base", "BaseCollector"),
    "Batch": ("base", "Batch"),
    "CollectorError": ("base", "CollectorError"),
    "dig": ("base", "dig"),
    "dig_list": ("base", "dig_list"),
    "APICollector": ("api_collector", "APICollector"),
    "RSSCollector": ("rss_collector", "RSSCollector"),
    "GitHubCollector": ("github_collector", "GitHubCollector"),
    "get_collector": ("factory", "get_collector"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"модуль {__name__!r} не имеет атрибута {name!r}")

    module_name, attribute = target
    module = import_module(f".{module_name}", __name__)
    value = module if attribute is None else getattr(module, attribute)
    globals()[name] = value          # кешируем, чтобы не импортировать повторно
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
