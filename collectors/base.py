"""Базовый коллектор: общий каркас сбора, маппинг полей и жёсткие фильтры.

Схема одна для всех источников: получить «сырые» элементы → отфильтровать по
item_filters → смапить поля из fields → отдать список RawItem.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping

import httpx

from config.loader import Source
from database.models import RawItem

from . import normalize
from .http import DEFAULT_ATTEMPTS, DEFAULT_BACKOFF, DEFAULT_TIMEOUT, make_client

logger = logging.getLogger(__name__)


class CollectorError(Exception):
    """Источник недоступен или ответ не удалось разобрать."""


def dig(data: Any, path: str, default: Any = None) -> Any:
    """Достать значение по точечному пути.

    Понимает и вложенность (employer.name), и «странные» имена полей
    открытых данных (job-name, vac_url, creation-date), и индексы списков.
    """
    if not path:
        return data if data is not None else default
    current: Any = data
    for part in path.split("."):
        if current is None:
            return default
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, (list, tuple)):
            if not part.isdigit() or int(part) >= len(current):
                return default
            current = current[int(part)]
        else:
            return default
    return default if current is None else current


def dig_list(data: Any, path: str) -> list[Any]:
    """Достать список элементов: путь пустой — корень ответа."""
    value = dig(data, path)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Mapping):
        return [value]
    return []


@dataclass
class Batch:
    """Результат сбора одного источника."""

    source_id: str
    raw_items: list[RawItem] = field(default_factory=list)
    fetched: int = 0
    filtered: int = 0
    pages: int = 0


class BaseCollector:
    type_name = "base"

    def __init__(
        self,
        source: Source,
        *,
        lookback_days: int = 1,
        client: httpx.AsyncClient | None = None,
        env: Mapping[str, str] | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.source = source
        self.lookback_days = max(1, int(lookback_days))
        self.attempts = attempts
        self.backoff = backoff
        # Таймаут источника важнее общего: у части открытых API ответ генерируется
        # по 15–20 секунд, и общий лимит 10 с их просто не дожидается.
        self.timeout = float(getattr(source, "timeout", None) or timeout)
        self._client = client
        self._env = os.environ if env is None else env

    # -- каркас ------------------------------------------------------------- #

    async def fetch_elements(self) -> list[Any]:
        """Вернуть «сырые» элементы источника. Реализуется в наследниках."""
        raise NotImplementedError

    async def collect(self) -> Batch:
        elements = await self.fetch_elements()
        batch = Batch(source_id=self.source.id)
        for element in elements:
            batch.fetched += 1
            if not normalize.passes_filters(self.extract_extra(element), self.source.item_filters):
                batch.filtered += 1
                continue
            raw = self.map_item(element)
            if raw is None:
                batch.filtered += 1
                logger.debug("[%s] элемент пропущен: нет заголовка", self.source.id)
                continue
            batch.raw_items.append(raw)
        return batch

    # -- маппинг ------------------------------------------------------------ #

    def map_item(self, element: Any) -> RawItem | None:
        fields = self.source.fields or {}
        title = normalize.clean_text(dig(element, fields.get("title", "title"), ""))
        if not title:
            return None

        url_field = fields.get("url", "url")
        url = normalize.clean_text(dig(element, url_field, "")) or None

        description = ""
        if fields.get("description"):
            description = normalize.clean_text(dig(element, fields["description"], ""))

        created_at = None
        if fields.get("created_at"):
            created_at = dig(element, fields["created_at"])

        return RawItem(
            title=title,
            url=url,
            description=description,
            created_at=created_at,
            source=self.source.id,
            label=self.source.label,
            extra=self.extract_extra(element),
        )

    def extract_extra(self, element: Any) -> dict[str, Any]:
        """Значения для жёстких фильтров и для показа в дайджесте."""
        return {
            key: dig(element, path)
            for key, path in (self.source.extra_fields or {}).items()
        }

    # -- инфраструктура ----------------------------------------------------- #

    @asynccontextmanager
    async def client(self) -> AsyncIterator[httpx.AsyncClient]:
        """Внешний клиент (например, из тестов) не закрываем."""
        if self._client is not None:
            yield self._client
            return
        async with make_client(timeout=self.timeout, env=self._env) as client:
            yield client
