"""Коллектор GitHub: подстановка даты в поисковый запрос и работа с лимитами."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import httpx

from database.db import utc_now

from .api_collector import APICollector

logger = logging.getLogger(__name__)


class GitHubCollector(APICollector):
    type_name = "github"

    def prepare_params(self, page_index: int) -> dict[str, Any]:
        params = super().prepare_params(page_index)
        query = params.get("q")
        if isinstance(query, str) and "{since}" in query:
            since = (utc_now() - timedelta(days=self.lookback_days)).date().isoformat()
            params["q"] = query.replace("{since}", since)
        return params

    def prepare_headers(self) -> dict[str, str]:
        headers = super().prepare_headers()
        headers.setdefault("Accept", "application/vnd.github+json")
        token = (self._env or {}).get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def describe_error(self, response: httpx.Response) -> str:
        base = super().describe_error(response)
        if response.status_code in (403, 429):
            remaining = response.headers.get("X-RateLimit-Remaining")
            reset = response.headers.get("X-RateLimit-Reset")
            hint = "лимит запросов исчерпан" if remaining == "0" else "доступ запрещён"
            return f"{base} | {hint} (X-RateLimit-Remaining={remaining}, X-RateLimit-Reset={reset})"
        return base
