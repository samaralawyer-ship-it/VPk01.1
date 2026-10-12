"""Нормализация материалов: URL, HTML, валюты, даты, dedup_hash, ключевые слова.

Здесь живёт вся «сермяжная» логика, из-за которой проект не ломается на реальных
данных: кириллица в описаниях, теги <highlighttext>, «руб.» вместо RUR,
даты в пяти форматах и ссылки с utm-метками.
"""

from __future__ import annotations

import hashlib
import html as html_module
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from database.db import iso_now, to_iso, utc_now
from database.models import Item, RawItem

TAG_RE = re.compile(r"<[^>]+>")
BREAK_RE = re.compile(r"<\s*(?:br|/p|/div|/li)\s*/?\s*>", re.IGNORECASE)
SPACE_RE = re.compile(r"[ \t\u00a0]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")

CURRENCY_MAP = {
    "rur": "RUR",
    "rub": "RUR",
    "руб": "RUR",
    "руб.": "RUR",
    "рублей": "RUR",
    "₽": "RUR",
    "usd": "USD",
    "$": "USD",
    "долл": "USD",
    "долл.": "USD",
    "eur": "EUR",
    "€": "EUR",
    "евро": "EUR",
    "kzt": "KZT",
    "тг": "KZT",
    "тенге": "KZT",
    "byn": "BYN",
    "uah": "UAH",
    "грн": "UAH",
    "gbp": "GBP",
    "£": "GBP",
}

# Разумные границы для дат: раньше 2000 года и позже «завтра» — это мусор в ленте
MIN_VALID_DATE = datetime(2000, 1, 1, tzinfo=timezone.utc)
MAX_FUTURE_SKEW_SECONDS = 24 * 3600

_HTML_ESCAPES = {"&nbsp;": " ", "&laquo;": "«", "&raquo;": "»", "&mdash;": "—", "&ndash;": "–"}


# --------------------------------------------------------------------------- #
# Текст


def clean_text(value: Any) -> str:
    """Убрать HTML-теги и сущности, выровнять пробелы и пустые строки."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    for entity, replacement in _HTML_ESCAPES.items():
        text = text.replace(entity, replacement)
    text = html_module.unescape(text)
    text = BREAK_RE.sub("\n", text)
    text = TAG_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def truncate(text: str, limit: int = 300) -> str:
    """Обрезать описание по границе слова — для дайджеста длинные тексты не нужны."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-—")
    return f"{cut}…"


def normalize_currency(value: Any) -> str:
    """«руб.» / ₽ / RUR → RUR. Неизвестное значение возвращаем как есть в верхнем регистре."""
    raw = clean_text(value).strip().strip("«»\"'").lower()
    if not raw:
        return ""
    if raw in CURRENCY_MAP:
        return CURRENCY_MAP[raw]
    compact = raw.replace(" ", "")
    if compact in CURRENCY_MAP:
        return CURRENCY_MAP[compact]
    if len(raw) <= 4 and raw.isascii() and raw.isalpha():
        return raw.upper()
    return clean_text(value)


# --------------------------------------------------------------------------- #
# URL и дедупликация


def normalize_url(url: Any, dedup: Any) -> str:
    """Нормализовать URL для дедупликации. Идемпотентна: f(f(x)) == f(x)."""
    raw = (url or "").strip() if isinstance(url, str) else ""
    if not raw:
        return ""

    parts = urlsplit(raw)
    if not parts.scheme and not parts.netloc:
        return raw  # не URL (например urn:) — оставляем как есть

    scheme = parts.scheme.lower()
    if getattr(dedup, "force_https", True) and scheme == "http":
        scheme = "https"

    netloc = parts.netloc.lower()
    if getattr(dedup, "strip_www", True):
        host, _, port = netloc.partition(":")
        for prefix in ("www.", "m.", "amp."):
            if host.startswith(prefix) and len(host) > len(prefix):
                host = host[len(prefix):]
                break
        netloc = f"{host}:{port}" if port else host

    path = parts.path
    if getattr(dedup, "strip_trailing_slash", True) and path.endswith("/"):
        path = path.rstrip("/")

    query = parts.query
    strip_params = {p.lower() for p in getattr(dedup, "strip_params", ()) or ()}
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        pairs = [(k, v) for k, v in pairs if k.lower() not in strip_params]
        if getattr(dedup, "sort_query", True):
            pairs.sort()
        query = urlencode(pairs)

    fragment = "" if getattr(dedup, "strip_fragment", True) else parts.fragment
    return urlunsplit((scheme, netloc, path, query, fragment))


def dedup_hash(url: Any, title: str, source: str, dedup: Any) -> str:
    """sha256 нормализованного URL, а при его отсутствии — sha256(source:title)."""
    normalized = normalize_url(url, dedup)
    key = normalized if normalized else f"{source}:{(title or '').strip().lower()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Даты


def parse_date(value: Any) -> tuple[str, int]:
    """Привести любую дату источника к ISO 8601 UTC.

    Возвращает (iso_utc, date_missing). Если дата отсутствует, битая или явно
    мусорная (до 2000 года либо сильно в будущем) — подставляем текущее время
    и помечаем date_missing = 1, чтобы материал не потерялся.
    """
    moment = _coerce_datetime(value)
    if moment is None:
        return iso_now(), 1

    moment = moment.astimezone(timezone.utc).replace(microsecond=0)
    if moment < MIN_VALID_DATE or (moment - utc_now()).total_seconds() > MAX_FUTURE_SKEW_SECONDS:
        return iso_now(), 1
    return to_iso(moment), 0


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value <= 0:
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    # feedparser отдаёт published_parsed — time.struct_time
    if isinstance(value, (tuple, list)) and len(value) >= 6:
        try:
            return datetime(*[int(x) for x in value[:6]], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    if hasattr(value, "tm_year") and hasattr(value, "tm_mon"):
        try:
            return datetime(value.tm_year, value.tm_mon, value.tm_mday, value.tm_hour, value.tm_min, value.tm_sec, tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    iso_candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso_candidate)
    except ValueError:
        pass

    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        pass

    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    try:  # запасной вариант, если установлен python-dateutil
        from dateutil import parser as dateutil_parser

        return dateutil_parser.parse(text)
    except Exception:  # noqa: BLE001 — любая ошибка парсинга означает «даты нет»
        return None


# --------------------------------------------------------------------------- #
# Сборка записи и фильтры


def build_item(raw: RawItem, source: str, label: str, dedup: Any) -> Item:
    """Превратить сырой материал в готовую запись items."""
    title = clean_text(raw.title)
    description = clean_text(raw.description)
    url = (raw.url or "").strip() or None
    created_at, date_missing = parse_date(raw.created_at)
    return Item(
        title=title,
        url=url,
        source=source,
        label=label,
        description=description,
        created_at=created_at,
        fetched_at=iso_now(),
        date_missing=date_missing,
        dedup_hash=dedup_hash(url, title, source, dedup),
        extra=normalize_extra(getattr(raw, "extra", None)),
    )


def normalize_extra(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Привести дополнительные значения к виду, пригодному для хранения и показа.

    Валюта нормализуется («руб.» → RUR), числа остаются числами, текст чистится.
    Пустые значения не сохраняем, чтобы не копить мусор в JSON.
    """
    if not values:
        return {}
    result: dict[str, Any] = {}
    for key, value in values.items():
        if value is None or value == "":
            continue
        if key.endswith("currency"):
            normalized: Any = normalize_currency(value)
        elif key.endswith(("_min", "_max")):
            number = _as_number(value)
            if number is None or number <= 0:
                continue          # 0 в открытых данных означает «не указано», а не ноль рублей
            normalized = int(number) if number.is_integer() else number
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            normalized = value
        else:
            normalized = clean_text(value)
        if normalized not in ("", None):
            result[key] = normalized
    return result


def passes_filters(extra: Mapping[str, Any], item_filters: Mapping[str, Any]) -> bool:
    """Жёсткие фильтры этапа 2 по значениям из extra_fields.

    Зарплата обрабатывается отдельно и как диапазон:
      * salary_min (от) — вакансия проходит, если до неё можно дотянуть: берётся
        наибольшая известная граница (salary_max, а если его нет — salary_min);
      * salary_max (до) — вакансия проходит, если её нижняя граница не выше потолка
        (наименьшая известная граница);
      * salary_currency — сверяется по нормализованной валюте, и только когда
        зарплата вообще указана;
      * include_unspecified_salary — пропускать ли вакансии без указанной зарплаты
        (по умолчанию нет). Значение 0 в открытых данных считается «не указана».

    Остальные ключи: числовые *_min/*_max сравниваются как числа, регион и
    работодатель — по подстроке без учёта регистра, прочее — на равенство.
    """
    filters = dict(item_filters or {})
    if not filters:
        return True

    if not _passes_salary_filters(extra, filters):
        return False

    for key, expected in filters.items():
        if key in SALARY_FILTER_KEYS or key in BOOL_FILTER_KEYS:
            continue

        actual = extra.get(key)

        if key in ("region", "area", "employer"):
            haystack = clean_text(actual).lower()
            needle = clean_text(expected).lower()
            if not needle:
                continue
            if needle not in haystack:
                return False
            continue

        if key.endswith("_min"):
            number = _as_number(actual)
            limit = _as_number(expected)
            if number is None or limit is None or number < limit:
                return False
            continue

        if key.endswith("_max"):
            number = _as_number(actual)
            limit = _as_number(expected)
            if number is None or limit is None or number > limit:
                return False
            continue

        if isinstance(expected, (list, tuple, set)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _passes_salary_filters(extra: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    want_min = _as_number(filters.get("salary_min"))
    want_max = _as_number(filters.get("salary_max"))
    want_currency = filters.get("salary_currency")
    if want_min is None and want_max is None and not want_currency:
        return True

    actual_min = _positive_or_none(_as_number(extra.get("salary_min")))
    actual_max = _positive_or_none(_as_number(extra.get("salary_max")))

    if actual_min is None and actual_max is None:
        # Зарплата не указана: пропускаем только по явному разрешению
        return bool(filters.get("include_unspecified_salary", False))

    if want_min is not None:
        reach = actual_max if actual_max is not None else actual_min
        if reach < want_min:
            return False

    if want_max is not None:
        floor = actual_min if actual_min is not None else actual_max
        if floor > want_max:
            return False

    if want_currency:
        return normalize_currency(extra.get("salary_currency")) == normalize_currency(want_currency)
    return True


def _positive_or_none(value: float | None) -> float | None:
    """0 в открытых данных означает «зарплата не указана», а не нулевую ставку."""
    if value is None or value <= 0:
        return None
    return value


def _as_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


DEFAULT_KEYWORD_FIELDS = ("title", "description")
SHORT_KEYWORD_LENGTH = 4
_WORD_PATTERNS: dict[str, re.Pattern[str]] = {}

# Ключи фильтров, которые считаются вместе (зарплатный диапазон) или являются
# переключателями, а не значениями из источника
SALARY_FILTER_KEYS = ("salary_min", "salary_max", "salary_currency")
BOOL_FILTER_KEYS = ("include_unspecified_salary",)


def _keyword_pattern(word: str) -> re.Pattern[str] | None:
    """Короткие ключи ищем по границам слова.

    Иначе «ии» как подстрока совпадает с «России», «информации», «Туркмении» —
    проверено на живом дайджесте, где половина новостей оказалась не по теме.
    """
    if len(word) >= SHORT_KEYWORD_LENGTH:
        return None
    pattern = _WORD_PATTERNS.get(word)
    if pattern is None:
        pattern = re.compile(rf"(?<!\w){re.escape(word)}(?!\w)")
        _WORD_PATTERNS[word] = pattern
    return pattern


def _contains(haystack: str, word: str) -> bool:
    pattern = _keyword_pattern(word)
    if pattern is None:
        return word in haystack
    return pattern.search(haystack) is not None


def _alternative_matches(haystack: str, alternative: Any) -> bool:
    """Альтернатива — один ключ или группа ключей, которые должны встретиться все.

    Группа нужна для фраз: «машинн» + «обучени» поймает «машинного обучения»,
    но не сработает на «машинное зрение» (в отличие от простого «машинн»).
    """
    if isinstance(alternative, str):
        words = [alternative]
    elif isinstance(alternative, (list, tuple)):
        words = [str(item) for item in alternative]
    else:
        return False

    cleaned = [word.strip().lower() for word in words if str(word).strip()]
    return bool(cleaned) and all(_contains(haystack, word) for word in cleaned)


def match_keywords(values: Mapping[str, Any], keywords: Mapping[str, Any] | None) -> bool:
    """Тематический отбор этапа 3: any / all / exclude по выбранным полям.

    Морфологии здесь нет и не планируется: ключ длиной 4+ ищется как подстрока,
    ключ короче 4 символов — по границам слова. Поэтому «склонения» добиваются
    усечёнными основами («нейросет» ловит нейросеть, нейросети, нейросетевой),
    а фразы задаются группой: ["машинн", "обучени"] вместо «машинное обучение».
    """
    if not keywords:
        return True

    fields: Iterable[str] = keywords.get("fields") or DEFAULT_KEYWORD_FIELDS
    haystack = " ".join(clean_text(values.get(field, "")) for field in fields).lower()

    exclude = keywords.get("exclude") or []
    if exclude and any(_alternative_matches(haystack, item) for item in exclude):
        return False

    must_all = keywords.get("all") or []
    if must_all and not all(_alternative_matches(haystack, item) for item in must_all):
        return False

    any_of = keywords.get("any") or []
    if any_of and not any(_alternative_matches(haystack, item) for item in any_of):
        return False

    return True
