"""Фабрика коллекторов: выбор класса по полю type источника."""

from __future__ import annotations

from typing import Mapping

import httpx

from config.loader import Source

from .api_collector import APICollector
from .base import BaseCollector
from .github_collector import GitHubCollector
from .rss_collector import RSSCollector

REGISTRY: dict[str, type[BaseCollector]] = {
    RSSCollector.type_name: RSSCollector,
    APICollector.type_name: APICollector,
    GitHubCollector.type_name: GitHubCollector,
}


def get_collector(
    source: Source,
    *,
    lookback_days: int = 1,
    client: httpx.AsyncClient | None = None,
    env: Mapping[str, str] | None = None,
    attempts: int | None = None,
    backoff: float | None = None,
) -> BaseCollector:
    """Вернуть коллектор для источника. Неизвестный type — ошибка конфигурации."""
    collector_class = REGISTRY.get(source.type)
    if collector_class is None:
        raise ValueError(
            f"источник {source.id}: неизвестный type «{source.type}» "
            f"(поддерживаются {', '.join(sorted(REGISTRY))})"
        )

    kwargs: dict[str, object] = {"lookback_days": lookback_days, "client": client, "env": env}
    if attempts is not None:
        kwargs["attempts"] = attempts
    if backoff is not None:
        kwargs["backoff"] = backoff
    return collector_class(source, **kwargs)  # type: ignore[arg-type]
