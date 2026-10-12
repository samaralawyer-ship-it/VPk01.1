"""Коллектор REST API: params, headers, пагинация, повторы."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import httpx

from .base import BaseCollector, CollectorError, dig_list
from .http import describe_response, request_with_retries

logger = logging.getLogger(__name__)

PER_PAGE_KEYS = ("per_page", "hitsPerPage", "limit", "count", "size")


class APICollector(BaseCollector):
    type_name = "api"

    # -- подготовка запроса ------------------------------------------------- #

    def prepare_url(self) -> str:
        return self.source.url

    def prepare_headers(self) -> dict[str, str]:
        return {str(key): str(value) for key, value in (self.source.headers or {}).items()}

    def prepare_params(self, page_index: int) -> dict[str, Any]:
        params: dict[str, Any] = dict(self.source.params or {})
        pagination = self.source.pagination or {}
        per_page = self._per_page()

        if page_index > 0:
            offset_param = pagination.get("offset_param")
            page_param = pagination.get("page_param")
            if offset_param and per_page:
                params[offset_param] = int(per_page) * page_index
            elif page_param:
                params[page_param] = page_index
        return params

    def describe_error(self, response: httpx.Response) -> str:
        return describe_response(response)

    # -- сбор --------------------------------------------------------------- #

    async def fetch_elements(self) -> list[Any]:
        pages = max(1, int((self.source.pagination or {}).get("max_pages", 1) or 1))
        per_page = self._per_page()
        elements: list[Any] = []

        async with self.client() as client:
            for page_index in range(pages):
                response = await request_with_retries(
                    client,
                    "GET",
                    self.prepare_url(),
                    params=self.prepare_params(page_index),
                    headers=self.prepare_headers(),
                    attempts=self.attempts,
                    backoff=self.backoff,
                    describe=self.describe_error,
                )
                payload = self._parse_json(response)
                page_elements = dig_list(payload, self.source.items_path or "")
                if not page_elements:
                    break

                elements.extend(page_elements)
                if per_page and len(page_elements) < int(per_page):
                    break

        return elements

    # -- вспомогательное ---------------------------------------------------- #

    def _per_page(self) -> int | None:
        params = self.source.params or {}
        for key in PER_PAGE_KEYS:
            value = params.get(key)
            if isinstance(value, int) and value > 0:
                return value
            if isinstance(value, str) and value.isdigit() and int(value) > 0:
                return int(value)
        return None

    @staticmethod
    def _parse_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            snippet = response.text[:200].replace("\n", " ")
            raise CollectorError(f"ответ не является JSON ({exc}): {snippet}") from exc
