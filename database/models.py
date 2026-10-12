"""Модели данных: сырой материал из источника и запись таблицы items."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Порядок колонок для INSERT — держать в одном месте
INSERT_COLUMNS = (
    "title",
    "url",
    "source",
    "label",
    "description",
    "created_at",
    "fetched_at",
    "date_missing",
    "dedup_hash",
    "extra",
)


@dataclass(slots=True)
class RawItem:
    """Материал до нормализации: то, что вернул коллектор."""

    title: str
    url: str | None = None
    description: str = ""
    created_at: Any = None          # сырое значение даты: str, struct_time, int или None
    source: str = ""
    label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Item:
    """Готовая запись для таблицы items."""

    title: str
    source: str
    label: str
    created_at: str
    fetched_at: str
    dedup_hash: str
    url: str | None = None
    description: str = ""
    date_missing: int = 0
    extra: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    def as_row(self) -> tuple[Any, ...]:
        """Значения в порядке INSERT_COLUMNS."""
        return (
            self.title,
            self.url,
            self.source,
            self.label,
            self.description,
            self.created_at,
            self.fetched_at,
            self.date_missing,
            self.dedup_hash,
            json.dumps(self.extra, ensure_ascii=False) if self.extra else None,
        )

    @classmethod
    def from_row(cls, row: Any) -> "Item":
        raw_extra = row["extra"] if "extra" in row.keys() else None
        return cls(
            id=row["id"],
            title=row["title"],
            url=row["url"],
            source=row["source"],
            label=row["label"],
            description=row["description"] or "",
            created_at=row["created_at"],
            fetched_at=row["fetched_at"],
            date_missing=row["date_missing"],
            dedup_hash=row["dedup_hash"],
            extra=json.loads(raw_extra) if raw_extra else {},
        )
