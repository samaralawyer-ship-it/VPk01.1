"""Опциональное саммари через DeepSeek.

Официального Python-SDK у DeepSeek нет — используется openai SDK с другим base_url.
Ключа нет или запрос упал — дайджест строится молча без саммари: это необязательная
возможность, а не зависимость.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT = 30.0
MAX_ITEMS = 10                 # в LLM уходят только заголовки и короткие описания
TITLE_LIMIT = 140
DESCRIPTION_LIMIT = 160
# deepseek-flash — рассуждающая модель: сначала тратит бюджет на reasoning_content
# и только потом пишет ответ в content. На лимите 220 токенов content приходил пустым
# с finish_reason=length, поэтому запас должен покрывать и рассуждения, и ответ.
MAX_OUTPUT_TOKENS = 7500

SYSTEM_PROMPT = (
    "Ты редактор новостного дайджеста. По списку материалов напиши краткое резюме "
    "на русском языке: 2–3 строки, без вступлений, без маркированных списков, "
    "только суть и общие тенденции."
)

BATCH_SYSTEM_PROMPT = (
    "Ты редактор новостного дайджеста из нескольких разделов. По материалам всех "
    "разделов напиши одно общее резюме на русском языке: 2–3 строки, без вступлений, "
    "без маркированных списков. Обязательно упомяни, что происходит в каждом разделе, "
    "где есть материалы, — резюме должно описывать дайджест целиком, а не только "
    "первый раздел."
)


class DeepSeekSummarizer:
    """Асинхронный вызываемый объект: список материалов → 2–3 строки резюме."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        client: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_items: int = MAX_ITEMS,
    ) -> None:
        self.model = model
        self.max_items = max_items
        self._client = client
        self._timeout = timeout

        if client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=timeout)

    async def __call__(self, rows: Sequence[Any]) -> str | None:
        prompt = self.build_prompt(rows)
        if not prompt:
            return None
        return await self._complete(SYSTEM_PROMPT, prompt)

    async def summarize_sections(self, sections: Mapping[str, Sequence[Any]]) -> str | None:
        """Общее резюме по всем разделам дайджеста — то, что стоит в шапке."""
        prompt = self.build_batch_prompt(sections)
        if not prompt:
            return None
        return await self._complete(BATCH_SYSTEM_PROMPT, prompt)

    async def _complete(self, system_prompt: str, prompt: str) -> str | None:
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=MAX_OUTPUT_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001 — саммари не должно ломать дайджест
            logger.warning("саммари DeepSeek не получено: %s: %s", type(exc).__name__, exc)
            return None

        choice = response.choices[0]
        content = (choice.message.content or "").strip()
        if not content:
            used = getattr(getattr(response, "usage", None), "completion_tokens", "?")
            reasoning = getattr(choice.message, "reasoning_content", None) or ""
            logger.warning(
                "саммари DeepSeek пустое: finish_reason=%s, токенов израсходовано %s, "
                "рассуждений %s символов. Модель %s тратит бюджет на reasoning — "
                "увеличьте MAX_OUTPUT_TOKENS (сейчас %s)",
                choice.finish_reason,
                used,
                len(reasoning),
                self.model,
                MAX_OUTPUT_TOKENS,
            )
            return None

        logger.info("саммари DeepSeek получено: %s символов", len(content))
        return content

    def build_prompt(self, rows: Sequence[Any]) -> str:
        lines: list[str] = []
        for row in list(rows)[: self.max_items]:
            title = _field(row, "title")[:TITLE_LIMIT]
            description = _field(row, "description")[:DESCRIPTION_LIMIT]
            lines.append(f"— {title}. {description}".strip())
        if not lines:
            return ""
        return "Материалы дайджеста:\n" + "\n".join(lines)

    def build_batch_prompt(self, sections: Mapping[str, Sequence[Any]]) -> str:
        """Промпт общего резюме: материалы сгруппированы по разделам дайджеста."""
        blocks: list[str] = []
        for name, rows in sections.items():
            lines = [
                f"  — {_field(row, 'title')[:TITLE_LIMIT]}. {_field(row, 'description')[:DESCRIPTION_LIMIT]}".strip()
                for row in list(rows)[: self.max_items]
            ]
            if lines:
                blocks.append(f"{name}:\n" + "\n".join(lines))
        if not blocks:
            return ""
        return "Материалы по разделам:\n" + "\n\n".join(blocks)


def _field(row: Any, name: str) -> str:
    try:
        value = row[name]
    except (KeyError, IndexError, TypeError):
        return ""
    return str(value or "").replace("\n", " ").strip()


def make_summarizer(env: Mapping[str, str] | None = None) -> DeepSeekSummarizer | None:
    """Собрать саммаризатор, если задан ключ. Нет ключа — None, и это нормально."""
    environment = os.environ if env is None else env
    api_key = (environment.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        logger.info("DEEPSEEK_API_KEY не задан — дайджест будет без саммари")
        return None
    model = (environment.get("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return DeepSeekSummarizer(api_key, model)
