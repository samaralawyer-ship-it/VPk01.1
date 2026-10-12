"""Загрузка и валидация конфигов проекта.

Три файла и три ответственности:
  * labels.yaml  — реестр меток (смысл материала);
  * sources.yaml — источники (транспорт) и правила дедупликации;
  * queries.yaml — подписки: что и когда собирать, что показывать.

Все проблемы собираются в один ConfigError: на старте должно быть видно сразу всё,
а не по одной ошибке за перезапуск.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

# --------------------------------------------------------------------------- #
# Константы

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent

LABEL_ID_RE = re.compile(r"^[a-z0-9_-]+$")
ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

SOURCE_TYPES = ("rss", "api", "github")
KEYWORD_KEYS = ("any", "all", "exclude", "fields")
# Коды языков для машинного перевода источника (ISO 639-3 плюс привычные двухбуквенные).
# Список держим в конфиге, чтобы не тянуть сюда LLM-модуль ради одной проверки.
TRANSLATION_CODES = frozenset({
    "eng", "en", "deu", "ger", "de", "fra", "fre", "fr", "spa", "es", "ita", "it",
    "por", "pt", "nld", "dut", "nl", "pol", "pl", "ukr", "uk", "tur", "tr",
    "jpn", "ja", "kor", "ko", "zho", "chi", "zh", "ara", "ar", "heb", "he",
})


def _is_language_code(value: str) -> bool:
    return value.strip().lower() in TRANSLATION_CODES


# Допустимые ключи жёстких фильтров этапа 2 (п. 6.2 ТЗ).
# include_unspecified_salary — переключатель, ему не нужен extra_fields.
FILTER_KEYS = (
    "salary_min",
    "salary_max",
    "salary_currency",
    "region",
    "area",
    "employer",
    "include_unspecified_salary",
)
BOOL_FILTER_KEYS = ("include_unspecified_salary",)

# Границы полей cron: минуты, часы, день месяца, месяц, день недели
CRON_FIELD_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
CRON_FIELD_NAMES = ("минуты", "часы", "день месяца", "месяц", "день недели")


class ConfigError(Exception):
    """Конфигурация невалидна. В сообщении — список всех найденных проблем."""


# --------------------------------------------------------------------------- #
# Модели конфигурации


@dataclass(frozen=True)
class Label:
    id: str
    title: str
    emoji: str = ""
    order: int = 100

    @property
    def header(self) -> str:
        return f"{self.emoji} {self.title}".strip()


@dataclass(frozen=True)
class DedupConfig:
    strip_fragment: bool = True
    force_https: bool = True
    strip_www: bool = True
    strip_trailing_slash: bool = True
    sort_query: bool = True
    strip_params: tuple[str, ...] = ()


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    label: str
    type: str
    url: str
    enabled: bool = True
    params: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, Any] = field(default_factory=dict)
    fields: Mapping[str, str] = field(default_factory=dict)
    extra_fields: Mapping[str, str] = field(default_factory=dict)
    items_path: str = ""
    pagination: Mapping[str, Any] = field(default_factory=dict)
    item_filters: Mapping[str, Any] = field(default_factory=dict)
    timeout: float | None = None
    translation: str = ""          # ISO 639-3 исходного языка: материалы переводятся при сборе


@dataclass(frozen=True)
class Query:
    id: str
    label: str
    schedule: str
    sources: tuple[str, ...] = ()
    keywords: Mapping[str, Any] = field(default_factory=dict)
    filters: Mapping[str, Any] = field(default_factory=dict)
    limit: int = 10
    lookback_days: int = 1
    summarize: bool = False
    tg_chat_id: str = ""
    notify_empty: bool = True      # «может не находить, но не может молчать»


@dataclass(frozen=True)
class AppConfig:
    labels: tuple[Label, ...]
    sources: tuple[Source, ...]
    queries: tuple[Query, ...]
    dedup: DedupConfig
    run_on_start: bool = True

    # -- удобные выборки ---------------------------------------------------- #
    def label(self, label_id: str) -> Label:
        for item in self.labels:
            if item.id == label_id:
                return item
        raise KeyError(label_id)

    def source(self, source_id: str) -> Source:
        for item in self.sources:
            if item.id == source_id:
                return item
        raise KeyError(source_id)

    @property
    def enabled_sources(self) -> tuple[Source, ...]:
        return tuple(s for s in self.sources if s.enabled)

    def sources_for_query(self, query: Query) -> tuple[Source, ...]:
        """Источники запроса: явный список или все включённые источники его метки."""
        if query.sources:
            return tuple(self.source(sid) for sid in query.sources)
        return tuple(s for s in self.enabled_sources if s.label == query.label)


# --------------------------------------------------------------------------- #
# Вспомогательные валидаторы


def validate_cron(expr: str) -> str | None:
    """Проверить cron из 5 полей. Возвращает текст ошибки или None."""
    if not isinstance(expr, str) or not expr.strip():
        return "расписание не задано"
    parts = expr.split()
    if len(parts) != 5:
        return f"cron должен состоять из 5 полей, получено {len(parts)}: «{expr}»"
    for part, limits, name in zip(parts, CRON_FIELD_LIMITS, CRON_FIELD_NAMES):
        error = _validate_cron_field(part, *limits)
        if error:
            return f"{name}: {error}"
    return None


def _validate_cron_field(value: str, low: int, high: int) -> str | None:
    for chunk in value.split(","):
        if not chunk:
            return f"пустой элемент в «{value}»"
        body, sep, step = chunk.partition("/")
        if sep and (not step.isdigit() or int(step) == 0):
            return f"некорректный шаг «{chunk}»"
        if body == "*":
            continue
        if "-" in body:
            start, _, end = body.partition("-")
            if not (start.isdigit() and end.isdigit()):
                return f"некорректный диапазон «{chunk}»"
            start_i, end_i = int(start), int(end)
            if start_i > end_i or not (low <= start_i <= high and low <= end_i <= high):
                return f"диапазон «{chunk}» вне границ {low}-{high}"
        else:
            if not body.isdigit():
                return f"некорректное значение «{chunk}»"
            if not low <= int(body) <= high:
                return f"значение «{chunk}» вне границ {low}-{high}"
    return None


def _resolve_env(value: Any, env: Mapping[str, str], path: str, errors: list[str]) -> Any:
    """Рекурсивно подставить ${VAR}. Незаданная переменная — ошибка, а не пустая строка."""
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env or env[name] == "":
                errors.append(f"{path}: переменная окружения ${{{name}}} не задана (проверьте .env)")
                return ""
            return str(env[name])

        return ENV_RE.sub(replace, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v, env, f"{path}.{k}", errors) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v, env, f"{path}[{i}]", errors) for i, v in enumerate(value)]
    return value


def _find_duplicate_keys(text: str) -> list[tuple[str, int]]:
    """Найти ключи, записанные дважды в одном объекте YAML.

    PyYAML молча оставляет последнее значение — проверено на 6.0.3, предупреждений
    нет. Из-за этого в queries.yaml можно написать:

        keywords:
          any: ["промпт"]
          any: ["prompt"]
          any: ["python"]     # выживет только это

    и три ключа из четырёх исчезнут без единого сообщения. Для конфига фильтров
    это дорогая ошибка: раздел молча перестаёт находить материалы.

    Разбираем дерево узлов (yaml.compose), а не строки по отступам: отступы
    не различают соседние элементы списка («- id: a» и «- id: b» стоят на одной
    глубине, но это разные объекты), и проверка по строкам давала ложные срабатывания.
    """
    duplicates: list[tuple[str, int]] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, yaml.MappingNode):
            keys = [key_node.value for key_node, _ in node.value]
            for key in dict.fromkeys(keys):
                if keys.count(key) > 1:
                    line = next(
                        key_node.start_mark.line + 1
                        for key_node, _ in node.value
                        if key_node.value == key and key_node.start_mark
                    )
                    duplicates.append((f"{path}.{key}" if path else key, line))
            for key_node, value_node in node.value:
                walk(value_node, f"{path}.{key_node.value}" if path else str(key_node.value))
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                walk(item, f"{path}[{index}]")

    try:
        walk(yaml.compose(text), "")
    except yaml.YAMLError:
        return []                     # битый YAML и так попадёт в ошибки при safe_load
    return duplicates


def _load_yaml(path: Path, errors: list[str]) -> Any:
    if not path.exists():
        errors.append(f"не найден файл конфигурации: {path}")
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"{path.name}: не удалось прочитать — {exc}")
        return {}

    for key, number in _find_duplicate_keys(text):
        errors.append(
            f"{path.name}:{number}: ключ «{key}» записан дважды в одном объекте — "
            f"останется только последнее значение. Объедините значения в один список"
        )

    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        errors.append(f"{path.name}: некорректный YAML — {exc}")
        return {}


def _as_list(value: Any, path: str, errors: list[str]) -> list[Any]:
    if value is None:
        errors.append(f"{path}: секция отсутствует")
        return []
    if not isinstance(value, list):
        errors.append(f"{path}: ожидался список")
        return []
    return value


def _is_keyword_item(item: Any) -> bool:
    """Ключ — строка или группа строк (все должны встретиться, см. п. 6.2 ТЗ)."""
    if isinstance(item, str):
        return bool(item.strip())
    if isinstance(item, list):
        return bool(item) and all(isinstance(nested, str) and nested.strip() for nested in item)
    return False


def _check_unique(ids: Iterable[str], path: str, errors: list[str]) -> None:
    seen: set[str] = set()
    for item_id in ids:
        if item_id in seen:
            errors.append(f"{path}: дубликат id «{item_id}»")
        seen.add(item_id)


# --------------------------------------------------------------------------- #
# Сборка секций


def _build_labels(raw: Any, errors: list[str]) -> tuple[Label, ...]:
    labels: list[Label] = []
    for index, entry in enumerate(_as_list(raw, "labels.yaml:labels", errors)):
        path = f"labels.yaml:labels[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{path}: ожидался объект")
            continue
        label_id = str(entry.get("id", "")).strip().lower()
        title = str(entry.get("title", "")).strip()
        if not label_id:
            errors.append(f"{path}: не задан id")
            continue
        if not LABEL_ID_RE.match(label_id):
            errors.append(
                f"{path}: id «{label_id}» недопустим — разрешены строчные латинские буквы, цифры, «-» и «_»"
            )
        if not title:
            errors.append(f"{path} ({label_id}): не задан title")
        order = entry.get("order", 100)
        if not isinstance(order, int):
            errors.append(f"{path} ({label_id}): order должен быть целым числом")
            order = 100
        labels.append(Label(id=label_id, title=title or label_id, emoji=str(entry.get("emoji", "")), order=order))
    _check_unique([lbl.id for lbl in labels], "labels.yaml", errors)
    return tuple(sorted(labels, key=lambda lbl: (lbl.order, lbl.id)))


def _build_dedup(raw: Any, errors: list[str]) -> DedupConfig:
    if raw is None:
        return DedupConfig()
    if not isinstance(raw, dict):
        errors.append("sources.yaml:dedup: ожидался объект")
        return DedupConfig()
    bools: dict[str, bool] = {}
    for key in ("strip_fragment", "force_https", "strip_www", "strip_trailing_slash", "sort_query"):
        value = raw.get(key, True)
        if not isinstance(value, bool):
            errors.append(f"sources.yaml:dedup.{key}: ожидалось true/false")
            value = True
        bools[key] = value
    strip_params = raw.get("strip_params", [])
    if not isinstance(strip_params, list) or not all(isinstance(p, str) for p in strip_params):
        errors.append("sources.yaml:dedup.strip_params: ожидался список строк")
        strip_params = []
    return DedupConfig(strip_params=tuple(p.lower() for p in strip_params), **bools)


def _build_sources(raw: Any, label_ids: set[str], errors: list[str]) -> tuple[Source, ...]:
    sources: list[Source] = []
    for index, entry in enumerate(_as_list(raw, "sources.yaml:sources", errors)):
        path = f"sources.yaml:sources[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{path}: ожидался объект")
            continue
        source_id = str(entry.get("id", "")).strip()
        if not source_id:
            errors.append(f"{path}: не задан id")
            continue
        path = f"{path} ({source_id})"

        label = str(entry.get("label", "")).strip().lower()
        if not label:
            errors.append(f"{path}: не задана метка label")
        elif label not in label_ids:
            errors.append(f"{path}: метка «{label}» не объявлена в labels.yaml")

        source_type = str(entry.get("type", "")).strip().lower()
        if source_type not in SOURCE_TYPES:
            errors.append(f"{path}: type «{source_type}» не поддерживается (нужен один из {', '.join(SOURCE_TYPES)})")

        url = str(entry.get("url", "")).strip()
        if not url:
            errors.append(f"{path}: не задан url")

        fields = entry.get("fields") or {}
        if not isinstance(fields, dict):
            errors.append(f"{path}: fields должен быть объектом")
            fields = {}
        for required in ("title", "url"):
            if required not in fields:
                errors.append(f"{path}: в fields не описан обязательный ключ «{required}»")

        params = entry.get("params") or {}
        headers = entry.get("headers") or {}
        pagination = entry.get("pagination") or {}
        item_filters = entry.get("item_filters") or {}
        for name, value in (("params", params), ("headers", headers), ("pagination", pagination), ("item_filters", item_filters)):
            if not isinstance(value, dict):
                errors.append(f"{path}: {name} должен быть объектом")
                value = {}
                if name == "params":
                    params = value
                elif name == "headers":
                    headers = value
                elif name == "pagination":
                    pagination = value
                else:
                    item_filters = value

        enabled = entry.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(f"{path}: enabled должен быть true/false")
            enabled = True

        max_pages = pagination.get("max_pages")
        if max_pages is not None and (not isinstance(max_pages, int) or max_pages < 1):
            errors.append(f"{path}: pagination.max_pages должен быть целым числом ≥ 1")

        extra_fields = entry.get("extra_fields") or {}
        if not isinstance(extra_fields, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in extra_fields.items()
        ):
            errors.append(f"{path}: extra_fields должен быть объектом вида {{имя: путь_в_источнике}}")
            extra_fields = {}
        for key in item_filters:
            if key not in FILTER_KEYS:
                errors.append(
                    f"{path}: item_filters.{key} — неизвестный фильтр (допустимы {', '.join(FILTER_KEYS)})"
                )
            elif key in BOOL_FILTER_KEYS:
                if not isinstance(item_filters[key], bool):
                    errors.append(f"{path}: item_filters.{key} должен быть true/false")
            elif key not in extra_fields:
                errors.append(
                    f"{path}: для item_filters.{key} нужно описать источник значения в extra_fields.{key}"
                )

        timeout = entry.get("timeout")
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                errors.append(f"{path}: timeout должен быть положительным числом секунд")
                timeout = None
            else:
                timeout = float(timeout)

        translation = str(entry.get("translation", "") or "").strip().lower()
        if translation:
            if not _is_language_code(translation):
                errors.append(
                    f"{path}: translation «{translation}» неизвестен. "
                    f"Ожидается код языка ISO 639-3 (например eng, deu, fra, jpn) "
                    f"или двухбуквенный (en, de, fr)"
                )
                translation = ""

        sources.append(
            Source(
                id=source_id,
                name=str(entry.get("name", source_id)).strip() or source_id,
                label=label,
                type=source_type,
                url=url,
                enabled=enabled,
                params=params,
                headers=headers,
                fields=fields,
                extra_fields=extra_fields,
                items_path=str(entry.get("items_path", "") or ""),
                pagination=pagination,
                item_filters=item_filters,
                timeout=timeout,
                translation=translation,
            )
        )
    _check_unique([s.id for s in sources], "sources.yaml", errors)
    return tuple(sources)


def _build_queries(
    raw: Any,
    defaults: Mapping[str, Any],
    label_ids: set[str],
    source_ids: set[str],
    errors: list[str],
) -> tuple[Query, ...]:
    queries: list[Query] = []
    for index, entry in enumerate(_as_list(raw, "queries.yaml:queries", errors)):
        path = f"queries.yaml:queries[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{path}: ожидался объект")
            continue
        query_id = str(entry.get("id", "")).strip()
        if not query_id:
            errors.append(f"{path}: не задан id")
            continue
        path = f"{path} ({query_id})"
        merged = {**defaults, **entry}

        label = str(merged.get("label", "")).strip().lower()
        if not label:
            errors.append(f"{path}: не задана метка label")
        elif label not in label_ids:
            errors.append(f"{path}: метка «{label}» не объявлена в labels.yaml")

        schedule = str(merged.get("schedule", "")).strip()
        cron_error = validate_cron(schedule)
        if cron_error:
            errors.append(f"{path}: {cron_error}")

        sources = merged.get("sources") or []
        if not isinstance(sources, list):
            errors.append(f"{path}: sources должен быть списком")
            sources = []
        for source_id in sources:
            if source_id not in source_ids:
                errors.append(f"{path}: источник «{source_id}» не найден в sources.yaml")

        limit = merged.get("limit", 10)
        if not isinstance(limit, int) or limit < 1:
            errors.append(f"{path}: limit должен быть целым числом ≥ 1")
            limit = 10

        lookback_days = merged.get("lookback_days", 1)
        if not isinstance(lookback_days, int) or lookback_days < 1:
            errors.append(f"{path}: lookback_days должен быть целым числом ≥ 1")
            lookback_days = 1

        keywords = merged.get("keywords") or {}
        if not isinstance(keywords, dict):
            errors.append(f"{path}: keywords должен быть объектом с ключами {', '.join(KEYWORD_KEYS)}")
            keywords = {}
        for key, value in keywords.items():
            if key not in KEYWORD_KEYS:
                errors.append(f"{path}: keywords.{key} — неизвестный ключ (допустимы {', '.join(KEYWORD_KEYS)})")
            elif not isinstance(value, list) or not all(_is_keyword_item(item) for item in value):
                errors.append(
                    f"{path}: keywords.{key} должен быть списком строк "
                    f"или групп строк (например [\"машинн\", \"обучени\"])"
                )

        filters = merged.get("filters") or {}
        if not isinstance(filters, dict):
            errors.append(f"{path}: filters должен быть объектом")
            filters = {}
        else:
            for key, value in filters.items():
                if key not in FILTER_KEYS:
                    errors.append(
                        f"{path}: filters.{key} — неизвестный фильтр (допустимы {', '.join(FILTER_KEYS)})"
                    )
                elif key in BOOL_FILTER_KEYS and not isinstance(value, bool):
                    errors.append(f"{path}: filters.{key} должен быть true/false")

        summarize = merged.get("summarize", False)
        if not isinstance(summarize, bool):
            errors.append(f"{path}: summarize должен быть true/false")
            summarize = False

        notify_empty = merged.get("notify_empty", True)
        if not isinstance(notify_empty, bool):
            errors.append(f"{path}: notify_empty должен быть true/false")
            notify_empty = True

        chat_id = str(merged.get("tg_chat_id", "")).strip()
        if not chat_id:
            errors.append(f"{path}: tg_chat_id не задан (проверьте TG_CHAT_ID в .env)")

        queries.append(
            Query(
                id=query_id,
                label=label,
                schedule=schedule,
                sources=tuple(sources),
                keywords=keywords,
                filters=filters,
                limit=limit,
                lookback_days=lookback_days,
                summarize=summarize,
                tg_chat_id=chat_id,
                notify_empty=notify_empty,
            )
        )
    _check_unique([q.id for q in queries], "queries.yaml", errors)
    return tuple(queries)


# --------------------------------------------------------------------------- #
# Точка входа


def load_config(config_dir: str | Path | None = None, env: Mapping[str, str] | None = None) -> AppConfig:
    """Загрузить и проверить конфигурацию. При проблемах поднимает ConfigError."""
    directory = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    environment = dict(os.environ) if env is None else dict(env)
    errors: list[str] = []

    labels_raw = _load_yaml(directory / "labels.yaml", errors)
    sources_raw = _load_yaml(directory / "sources.yaml", errors)
    queries_raw = _load_yaml(directory / "queries.yaml", errors)

    labels = _build_labels(labels_raw.get("labels"), errors)
    label_ids = {lbl.id for lbl in labels}

    dedup = _build_dedup(sources_raw.get("dedup"), errors)
    sources = _build_sources(sources_raw.get("sources"), label_ids, errors)
    source_ids = {src.id for src in sources}

    queries_raw = _resolve_env(queries_raw, environment, "queries.yaml", errors)
    defaults = queries_raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        errors.append("queries.yaml:defaults: ожидался объект")
        defaults = {}
    default_keys = {"limit", "lookback_days", "summarize", "notify_empty"}
    for key in defaults:
        if key not in default_keys:
            errors.append(f"queries.yaml:defaults.{key} — неизвестный ключ (допустимы {', '.join(sorted(default_keys))})")

    queries = _build_queries(queries_raw.get("queries"), defaults, label_ids, source_ids, errors)

    run_on_start = queries_raw.get("run_on_start", True)
    if not isinstance(run_on_start, bool):
        errors.append("queries.yaml:run_on_start должен быть true/false")
        run_on_start = True

    if not labels:
        errors.append("реестр меток пуст — дайджест не из чего собирать")
    if not any(src.enabled for src in sources):
        errors.append("нет ни одного включённого источника (enabled: true)")
    if not queries:
        errors.append("не объявлено ни одного запроса (queries.yaml)")

    if errors:
        raise ConfigError("Конфигурация невалидна:\n" + "\n".join(f"  - {err}" for err in errors))

    return AppConfig(
        labels=labels,
        sources=sources,
        queries=queries,
        dedup=dedup,
        run_on_start=run_on_start,
    )
