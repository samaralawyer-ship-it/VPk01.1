"""Коллектор RSS: feedparser получает байты, а не декодированную строку.

Это принципиально: charset в HTTP-заголовке у части лент врёт, и если скормить
feedparser результат response.text, кириллица превратится в мусор. feedparser
сам определяет кодировку по XML-декларации.
"""

from __future__ import annotations

import logging
from typing import Any

import feedparser

from .base import BaseCollector, CollectorError
from .http import request_with_retries

logger = logging.getLogger(__name__)


class RSSCollector(BaseCollector):
    type_name = "rss"

    async def fetch_elements(self) -> list[Any]:
        headers = {str(k): str(v) for k, v in (self.source.headers or {}).items()} or None

        async with self.client() as client:
            response = await request_with_retries(
                client,
                "GET",
                self.source.url,
                headers=headers,
                attempts=self.attempts,
                backoff=self.backoff,
            )
            payload = response.content          # именно байты
            status = response.status_code

        parsed = feedparser.parse(payload)
        entries = list(parsed.entries or [])
        problem = getattr(parsed, "bozo_exception", None)

        if not entries:
            detail = f"{type(problem).__name__}: {problem}" if problem else "записей нет"
            raise CollectorError(f"[{self.source.id}] лента пуста или не разобрана (HTTP {status}, {detail})")

        if problem:
            logger.warning("[%s] лента разобрана с замечанием: %s", self.source.id, problem)

        logger.debug("[%s] лента отдала %s записей", self.source.id, len(entries))
        return entries
