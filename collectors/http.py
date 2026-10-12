"""Общий HTTP-слой коллекторов: клиент, повторы, вежливость к источникам.

Здесь одна политика на весь проект: таймаут 10 секунд, три попытки с
экспоненциальным backoff, уважение Retry-After, единый User-Agent.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Mapping

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF = 1.0
DEFAULT_USER_AGENT = "news-vacancy-aggregator/1.0 (educational project)"
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(Exception):
    """HTTP-ошибка с кодом и URL — то, что попадает в run_log."""


def default_user_agent(env: Mapping[str, str] | None = None) -> str:
    environment = os.environ if env is None else env
    return environment.get("HTTP_USER_AGENT") or DEFAULT_USER_AGENT


def make_client(
    *,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str | None = None,
    env: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    proxy: str | None = None,
) -> httpx.AsyncClient:
    """Клиент с общими настройками. transport и proxy нужны для тестов и обхода блокировок."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        headers={"User-Agent": user_agent or default_user_agent(env)},
        transport=transport,
        proxy=proxy or None,
    )


def proxy_from_env(*names: str, env: Mapping[str, str] | None = None) -> str | None:
    """Первый заданный прокси из перечисленных переменных окружения."""
    environment = os.environ if env is None else env
    for name in names:
        value = (environment.get(name) or "").strip()
        if value:
            return value
    return None


def describe_response(response: httpx.Response) -> str:
    """Короткое описание ошибки для лога: код, URL и начало тела."""
    body = ""
    try:
        body = response.text.strip().replace("\n", " ")[:200]
    except Exception:  # noqa: BLE001 — тело может быть нечитаемым
        body = "<тело недоступно>"
    return f"HTTP {response.status_code} {response.request.url}: {body}"


def retry_after_seconds(response: httpx.Response, default: float) -> float:
    raw = response.headers.get("Retry-After")
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    describe: Callable[[httpx.Response], str] | None = None,
) -> httpx.Response:
    """Запрос с повторами. 4xx без повторов, 429/5xx — с паузой и повтором."""
    attempts = max(1, attempts)
    last_error = ""

    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Сбой запроса %s (попытка %s/%s): %s", url, attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(backoff * 2 ** (attempt - 1))
            continue

        if response.status_code < 400:
            return response

        describe_fn = describe or describe_response
        last_error = describe_fn(response)
        if response.status_code not in RETRY_STATUSES or attempt == attempts:
            raise HttpError(last_error)

        pause = retry_after_seconds(response, backoff * 2 ** (attempt - 1))
        logger.warning(
            "HTTP %s на %s (попытка %s/%s), пауза %.1f с",
            response.status_code,
            url,
            attempt,
            attempts,
            pause,
        )
        await asyncio.sleep(pause)

    raise HttpError(last_error or f"не удалось получить {url}")
