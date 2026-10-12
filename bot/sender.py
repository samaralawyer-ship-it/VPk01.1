"""Отправка сообщений в Telegram: HTML, пауза, разбор 429, режим DRY_RUN."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Mapping, Sequence

import httpx

from collectors.http import make_client, proxy_from_env

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 4000
DEFAULT_PAUSE = 1.0
MAX_ATTEMPTS = 3
TRUE_VALUES = {"1", "true", "yes", "on", "да"}


class SenderError(Exception):
    """Сообщение не удалось доставить."""


def split_text(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Аварийная разбивка по строкам — на случай, если рендер не уложился в лимит."""
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


class TelegramSender:
    """Отправка через Bot API напрямую: работает и в тестах, и в DRY_RUN."""

    def __init__(
        self,
        token: str | None = None,
        *,
        dry_run: bool = False,
        client: httpx.AsyncClient | None = None,
        env: Mapping[str, str] | None = None,
        pause: float = DEFAULT_PAUSE,
        timeout: float = 20.0,
    ) -> None:
        environment = os.environ if env is None else env
        self.token = (token or environment.get("TELEGRAM_BOT_TOKEN") or "").strip()
        self.dry_run = dry_run or str(environment.get("DRY_RUN", "")).strip().lower() in TRUE_VALUES
        # api.telegram.org недоступен из части российских сетей — прокси задаётся
        # отдельной переменной, чтобы не заворачивать в него источники
        self.proxy = proxy_from_env("TG_PROXY", "ALL_PROXY", "HTTPS_PROXY", env=environment)
        self.pause = pause
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    # -- публичный интерфейс ------------------------------------------------ #

    async def send(self, chat_id: str | int, text: str, *, parse_mode: str = "HTML") -> int:
        """Отправить одно логическое сообщение (длинное — по частям). Возвращает число частей."""
        chunks = split_text(text)
        for chunk in chunks:
            await self._send_one(chat_id, chunk, parse_mode)
        return len(chunks)

    async def send_many(
        self,
        chat_id: str | int,
        messages: Sequence[str],
        *,
        pause: float | None = None,
    ) -> int:
        """Отправить сообщения по порядку с паузой (лимит Telegram ~20 сообщений/мин в чат)."""
        wait = self.pause if pause is None else pause
        parts = 0
        for index, message in enumerate(messages):
            if index and wait:
                await asyncio.sleep(wait)
            parts += await self.send(chat_id, message)
        return parts

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "TelegramSender":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # -- внутреннее --------------------------------------------------------- #

    async def _send_one(self, chat_id: str | int, text: str, parse_mode: str) -> None:
        if self.dry_run:
            print(f"----- DRY_RUN → чат {chat_id} -----")
            print(text)
            print()
            return

        if not self.token:
            raise SenderError("не задан TELEGRAM_BOT_TOKEN")

        url = f"{API_BASE}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        client = self._get_client()

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise SenderError(f"сеть недоступна: {type(exc).__name__}: {exc}") from exc
                logger.warning("сбой отправки (попытка %s/%s): %s", attempt, MAX_ATTEMPTS, exc)
                await asyncio.sleep(self.pause * attempt)
                continue

            if response.status_code == 200:
                return

            data = _json_or_empty(response)
            if response.status_code == 429 and attempt < MAX_ATTEMPTS:
                retry_after = int((data.get("parameters") or {}).get("retry_after", 1) or 1)
                logger.warning("Telegram ответил 429: пауза %s с", retry_after)
                await asyncio.sleep(retry_after)
                continue

            description = data.get("description") or response.text[:200]
            raise SenderError(f"Telegram {response.status_code}: {description}")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = make_client(timeout=self.timeout, proxy=self.proxy)
            self._owns_client = True
        return self._client


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def make_sender(env: Mapping[str, str] | None = None, *, dry_run: bool = False) -> TelegramSender:
    return TelegramSender(env=env, dry_run=dry_run)
