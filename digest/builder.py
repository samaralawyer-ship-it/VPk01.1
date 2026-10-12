"""Сборка дайджеста: отбор новых материалов, HTML, разбивка на сообщения.

Разделение обязанностей:
  * select_items — что показать (ключевые слова и filters запроса применяются ДО лимита,
    иначе лимит съедали бы отброшенные материалы);
  * render_section — как показать (HTML, разбивка по границам элементов);
  * build_digest — связать это с базой и (опционально) саммари.

Ни одной строки вида «📰 Новости отрасли» здесь нет: заголовки и эмодзи берутся
из реестра меток, название источника — из конфига.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from collectors.normalize import match_keywords, passes_filters, truncate
from config.loader import AppConfig, Label, Query
from database import repo

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 4000                 # запас на заголовок и служебные символы
DESCRIPTION_LIMIT = 220
CANDIDATE_MULTIPLIER = 25         # сколько кандидатов просматриваем, чтобы набрать лимит после фильтров

CURRENCY_SYMBOLS = {
    "RUR": "₽",
    "USD": "$",
    "EUR": "€",
    "KZT": "₸",
    "BYN": "Br",
    "UAH": "₴",
    "GBP": "£",
}

Summarizer = Callable[[Sequence[sqlite3.Row]], Awaitable[str | None]]


@dataclass
class Section:
    label: Label
    messages: list[str]
    item_ids: list[int]
    rows: list[Any] = field(default_factory=list)   # материалы раздела: нужны для резюме

    @property
    def items_count(self) -> int:
        return len(self.item_ids)


@dataclass
class Digest:
    query_id: str
    chat_id: str
    label: Label
    sections: list[Section] = field(default_factory=list)
    rows: list[Any] = field(default_factory=list)   # отобранные материалы (для общего резюме)

    @property
    def messages(self) -> list[str]:
        return [message for section in self.sections for message in section.messages]

    @property
    def item_ids(self) -> list[int]:
        return [item_id for section in self.sections for item_id in section.item_ids]

    def is_empty(self) -> bool:
        return not self.item_ids


# --------------------------------------------------------------------------- #
# Отбор


def select_items(conn: sqlite3.Connection, cfg: AppConfig, query: Query) -> list[sqlite3.Row]:
    """Новые материалы метки, прошедшие ключевые слова и filters запроса."""
    candidates = repo.get_new_items(
        conn,
        query.label,
        limit=max(query.limit, query.limit * CANDIDATE_MULTIPLIER),
        lookback_days=query.lookback_days,
    )

    selected: list[sqlite3.Row] = []
    for row in candidates:
        values = {"title": row["title"], "description": row["description"] or ""}
        if not match_keywords(values, query.keywords):
            continue
        if not passes_filters(_row_extra(row), query.filters):
            continue
        selected.append(row)
        if len(selected) >= query.limit:
            break

    logger.debug(
        "отбор %s: кандидатов %s, выбрано %s (лимит %s)",
        query.id,
        len(candidates),
        len(selected),
        query.limit,
    )
    return selected


def select_last(conn: sqlite3.Connection, label: str, limit: int = 5) -> list[sqlite3.Row]:
    """Последние материалы метки из БД — для /last, без сети и без пометки «отправлено»."""
    return conn.execute(
        "SELECT * FROM items WHERE label = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (label, limit),
    ).fetchall()


def _row_extra(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["extra"] if "extra" in row.keys() else None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# Форматирование


def format_salary(extra: Mapping[str, Any]) -> str:
    """«250000–300000 ₽», «от 250000 ₽», «до 300000 ₽» либо пустая строка."""
    low = extra.get("salary_min")
    high = extra.get("salary_max")
    currency = str(extra.get("salary_currency") or "")
    symbol = CURRENCY_SYMBOLS.get(currency, currency)

    def number(value: Any) -> str:
        return f"{int(value):,}".replace(",", " ")

    if low and high and low != high:
        text = f"{number(low)}–{number(high)}"
    elif low:
        text = f"от {number(low)}"
    elif high:
        text = f"до {number(high)}"
    else:
        return ""
    return f"{text} {symbol}".strip()


def item_block(index: int, row: sqlite3.Row, cfg: AppConfig) -> str:
    """Один элемент дайджеста. Весь текст из источника экранируется."""
    title = html.escape(str(row["title"]))
    lines = [f"{index}. <b>{title}</b>"]

    url = row["url"]
    if url:
        link = f'<a href="{html.escape(str(url), quote=True)}">Читать</a>'
    else:
        link = "ссылки нет"

    source_id = str(row["source"])
    try:
        source_name = cfg.source(source_id).name
    except KeyError:
        source_name = source_id
    lines.append(f"   {link} — {html.escape(source_name)}")

    extra = _row_extra(row)
    details = [value for value in (extra.get("company"), extra.get("region"), format_salary(extra)) if value]
    if details:
        lines.append(f"   {html.escape(' · '.join(str(value) for value in details))}")

    description = truncate(str(row["description"] or ""), DESCRIPTION_LIMIT)
    if description:
        lines.append(f"   <i>{html.escape(description)}</i>")

    return "\n".join(lines)


def render_section(
    label: Label,
    rows: Sequence[sqlite3.Row],
    cfg: AppConfig,
    *,
    summary: str | None = None,
    tz_name: str | None = None,
    limit: int = SAFE_LIMIT,
) -> list[str]:
    """Сообщения одного раздела: разрыв только между элементами, теги не рвутся."""
    if not rows:
        return []

    stamp = _today(tz_name)
    header = f"{label.header} — {stamp} ({len(rows)})"
    if summary:
        header = f"{header}\n\n<i>{html.escape(summary)}</i>"

    blocks = [item_block(index, row, cfg) for index, row in enumerate(rows, start=1)]

    messages: list[str] = []
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > limit and current != header:
            messages.append(current)
            current = f"{label.header} — {stamp} (продолжение)"
        current = f"{current}\n\n{block}"
    messages.append(current)

    over = [message for message in messages if len(message) > TELEGRAM_LIMIT]
    if over:
        logger.warning(
            "раздел %s: %s сообщений длиннее лимита Telegram (максимум %s)",
            label.id,
            len(over),
            max(len(message) for message in over),
        )
    return messages


def _today(tz_name: str | None) -> str:
    zone = ZoneInfo(tz_name) if tz_name else None
    moment = datetime.now(zone) if zone else datetime.now()
    return moment.strftime("%d.%m.%Y")


def today(tz_name: str | None = None) -> str:
    """Дата раздела в таймзоне проекта — для заголовка общего дайджеста."""
    return _today(tz_name)


def split_messages(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Разбить текст по абзацам, не превышая лимит и не разрывая абзац."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        while len(paragraph) > limit:            # аварийный случай: абзац длиннее лимита
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > limit and current:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------- #
# Сборка


async def build_digest(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    query: Query,
    *,
    summarizer: Summarizer | None = None,
    tz_name: str | None = None,
) -> Digest:
    """Собрать дайджест запроса: отбор в отдельном потоке, затем (опц.) саммари и рендер."""
    rows = await asyncio.to_thread(select_items, conn, cfg, query)
    digest = Digest(query_id=query.id, chat_id=query.tg_chat_id, label=cfg.label(query.label), rows=list(rows))
    if not rows:
        return digest

    summary = None
    if summarizer is not None:
        summary = await summarizer(rows)

    messages = render_section(cfg.label(query.label), rows, cfg, summary=summary, tz_name=tz_name)
    digest.sections.append(
        Section(
            label=cfg.label(query.label),
            messages=messages,
            item_ids=[int(row["id"]) for row in rows],
            rows=list(rows),
        )
    )
    return digest
