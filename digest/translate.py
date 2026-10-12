"""Машинный перевод материалов иностранных источников через DeepSeek.

Зачем: Hacker News, Dev.to и подобные отдают английские заголовки. Перевод
делается один раз при сборе — в базе и дайджесте материал сразу на русском,
и ключевые слова раздела работают по русскому тексту.

Правила проекта, которые соблюдает модуль:
  * нет ключа — фича молча выключена, сбор работает как раньше;
  * сбой API не роняет материал: оригинал уходит в базу как есть и помечается;
  * переводятся только новые материалы (это решает пайплайн), иначе платно
    переводились бы повторы, которых на каждом прогоне большинство;
  * дедупликация считается по оригиналу, поэтому у одного материала не может
    появиться двух хешей из-за разного перевода.

Как это работает по шагам:

    1. источник помечен в sources.yaml полем translation: eng;
    2. сбор собирает материалы, как обычно, и считает dedup_hash по ОРИГИНАЛУ;
    3. пайплайн спрашивает базу, каких хешей ещё нет, и отдаёт сюда только новые;
    4. translate_items собирает из них один список строк (заголовки и описания
       вперемешку) и переводит его батчами по 20 строк;
    5. успешный перевод заменяет поля на месте, оригинал кладётся в extra;
    6. сохранение в базу идёт как обычно — с тем же хешем, что считали в п. 2.

Почему батчами, а не по одному материалу: у каждого вызова LLM есть накладные
расходы на «рассуждения» модели, и на одном материале они больше самого текста.
30 материалов одним батчем по 20 — это 2 запроса и ~30 секунд вместо 30 запросов.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT = 60.0
DEFAULT_TARGET_LANG = "rus"
MAX_BATCH = 20                 # строк (не материалов!) за один вызов
TITLE_LIMIT = 200              # что вообще отправляем в модель
DESCRIPTION_LIMIT = 400
MAX_OUTPUT_TOKENS = 7500       # deepseek-flash тратит бюджет на reasoning

# Коды языков оригинала. Ключ — то, что можно написать в sources.yaml, значение —
# как назвать язык в промпте («переводчик с английского языка»). Отдельная копия
# списка есть в config/loader.py (TRANSLATION_CODES): загрузчику нельзя зависеть
# от LLM-модуля, а здесь нужны человеческие названия языков.
LANGUAGE_CODES = {
    "eng": "английского", "en": "английского",
    "deu": "немецкого", "ger": "немецкого", "de": "немецкого",
    "fra": "французского", "fre": "французского", "fr": "французского",
    "spa": "испанского", "es": "испанского",
    "ita": "итальянского", "it": "итальянского",
    "por": "португальского", "pt": "португальского",
    "nld": "нидерландского", "dut": "нидерландского", "nl": "нидерландского",
    "pol": "польского", "pl": "польского",
    "ukr": "украинского", "uk": "украинского",
    "tur": "турецкого", "tr": "турецкого",
    "jpn": "японского", "ja": "японского",
    "kor": "корейского", "ko": "корейского",
    "zho": "китайского", "chi": "китайского", "zh": "китайского",
    "ara": "арабского", "ar": "арабского",
    "heb": "иврита", "he": "иврита",
}
TARGET_LANGUAGES = {"rus": "русский", "ru": "русский", "eng": "английский", "en": "английский"}

# Запасной разбор ответа: «1. перевод», «2) перевод». Нужен потому, что модель
# иногда игнорирует просьбу вернуть JSON и отвечает нумерованным списком.
INDEX_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*(.+)$")


def normalize_language(code: Any) -> str:
    """Привести код языка к трём буквам ISO 639-3: en → eng, ger → deu.

    В конфиге удобно писать коротко (en), а в базе и в промпте — однозначно
    (eng). Заодно приводим устаревшие коды ISO 639-2 (ger, fre, dut, chi)
    к современным: модель на них реагирует предсказуемее.
    """
    value = str(code or "").strip().lower()
    aliases = {"ger": "deu", "fre": "fra", "dut": "nld", "chi": "zho"}
    value = aliases.get(value, value)
    if value in ("en", "de", "fr", "es", "it", "pt", "nl", "pl", "uk", "tr", "ja", "ko", "zh", "ar", "he"):
        return {
            "en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "pt": "por",
            "nl": "nld", "pl": "pol", "uk": "ukr", "tr": "tur", "ja": "jpn", "ko": "kor",
            "zh": "zho", "ar": "ara", "he": "heb",
        }[value]
    return value


def language_name(code: Any) -> str | None:
    """Название исходного языка для промпта. Неизвестный код — None.

    Пробуем и как есть (eng), и после нормализации (en → eng): так один и тот же
    словарь обслуживает оба написания.
    """
    return LANGUAGE_CODES.get(str(code or "").strip().lower()) or LANGUAGE_CODES.get(normalize_language(code))


def target_language_name(code: Any) -> str:
    """Название целевого языка. Неизвестное значение — русский по умолчанию."""
    return TARGET_LANGUAGES.get(str(code or "").strip().lower(), TARGET_LANGUAGES[DEFAULT_TARGET_LANG])


class DeepSeekTranslator:
    """Асинхронный переводчик: список текстов → список переводов той же длины.

    Длина результата всегда равна длине входа: на месте непереведённого стоит None.
    Благодаря этому вызывающий код сопоставляет ответ с материалом по индексу
    и не может перепутать переводы между материалами.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        client: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        target_lang: str = DEFAULT_TARGET_LANG,
        max_batch: int = MAX_BATCH,
    ) -> None:
        self.model = model
        self.target_lang = target_lang
        self.max_batch = max(1, max_batch)
        self._client = client
        if client is None:
            # openai SDK с другим base_url: своего SDK у DeepSeek нет.
            # client можно передать снаружи — так тесты работают без сети.
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=timeout)

    async def translate_many(self, texts: Sequence[str], source_lang: str | None = None) -> list[str | None]:
        """Перевести тексты батчами. None на месте текста — перевод не удался.

        Разбивка на батчи здесь, а не в вызывающем коде: пайплайну всё равно,
        сколько запросов уйдёт, ему важен результат той же длины, что и вход.
        """
        results: list[str | None] = [None] * len(texts)
        for start in range(0, len(texts), self.max_batch):
            chunk = texts[start : start + self.max_batch]
            answers = await self._translate_chunk(chunk, source_lang)
            for offset, answer in enumerate(answers):
                results[start + offset] = answer
        return results

    async def _translate_chunk(self, texts: Sequence[str], source_lang: str | None) -> list[str | None]:
        """Один запрос к модели. Любая ошибка → None на всю пачку, но не исключение."""
        prompt = self.build_prompt(texts, source_lang)
        if not prompt:
            return [None] * len(texts)
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.build_system_prompt(source_lang)},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,          # перевод: меньше вариативности — стабильнее формат
                max_tokens=MAX_OUTPUT_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001 — перевод не должен ломать сбор
            # Ловим всё: сеть, лимиты, ошибки SDK. Сбор обязан продолжиться без перевода.
            logger.warning(
                "перевод не получен (%s текстов, %s): %s: %s",
                len(texts), source_lang or "язык не указан", type(exc).__name__, exc,
            )
            return [None] * len(texts)

        content = (response.choices[0].message.content or "").strip()
        parsed = parse_translations(content, len(texts))
        missing = sum(1 for value in parsed if not value)
        if missing:
            # Не ошибка: часть строк модель пропустила или склеила.
            # Эти материалы останутся на языке оригинала и получат пометку.
            logger.warning("перевод неполный: пусто %s из %s", missing, len(texts))
        return parsed

    def build_system_prompt(self, source_lang: str | None) -> str:
        """Инструкция модели: формат ответа, стиль и что не переводить.

        Требование «только JSON-массив той же длины» — не украшение: именно оно
        делает ответ разбираемым. Просьбы «переведи строки» модель выполняет
        по-разному (списком, с нумерацией, с пояснениями), и это ломало разбор.
        """
        origin = language_name(source_lang) or "иностранного"
        target = target_language_name(self.target_lang)
        return (
            f"Ты переводчик технических новостей с {origin} языка на {target}. "
            "Тебе дают JSON-массив строк. Верни ТОЛЬКО JSON-массив переводов той же "
            "длины и в том же порядке — без пояснений, без markdown, без лишнего текста. "
            "Переводи смысл, а не слова: заголовки должны читаться как русские новостные. "
            "Названия компаний, продуктов, проектов и аббревиатуры (OpenAI, Kubernetes, "
            "React) оставляй в оригинале. Пометку «Show HN:» оставляй как есть."
        )

    def build_prompt(self, texts: Sequence[str], source_lang: str | None = None) -> str:
        """Пользовательская часть: только тексты, JSON-массивом.

        Пустые строки выбрасываем: они не несут смысла, но занимают место в ответе
        и сбивают соответствие «строка входа → строка выхода».
        """
        clean = [" ".join(str(text or "").split())[:DESCRIPTION_LIMIT] for text in texts]
        clean = [text for text in clean if text]
        if not clean:
            return ""
        return json.dumps(clean, ensure_ascii=False)


def parse_translations(content: str, expected: int) -> list[str | None]:
    """Разобрать ответ модели. Ожидается JSON-массив той же длины.

    Ответ модели — не контракт, а вероятность, поэтому разборов три, от строгого
    к мягкому:
      1. JSON-массив (как просили): лишнее обрезаем, недостающее добиваем None;
      2. нумерованный список «1. перевод» — если модель проигнорировала формат;
      3. просто строки — если их ровно столько, сколько просили.

    Результат ВСЕГДА приводится к длине expected: вызывающий код сопоставляет
    ответы с материалами по индексу и не должен проверять длину сам.
    """
    raw = (content or "").strip()
    if raw.startswith("```"):                                  # модель обернула в markdown
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw

    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None

    if isinstance(parsed, list):
        values = [str(value).strip() if value is not None else None for value in parsed]
        if len(values) == expected:
            return values
        if len(values) > expected:
            return values[:expected]
        # Короткий ответ: чего не хватило — остаётся непереведённым.
        return [*values, *([None] * (expected - len(values)))]

    answers: dict[int, str] = {}
    for line in raw.splitlines():
        match = INDEX_RE.match(line)
        if not match:
            continue
        number = int(match.group(1))
        if 1 <= number <= expected:
            answers[number] = match.group(2).strip()
    if answers:
        # Раскладываем по номерам: модель может ответить не по порядку.
        return [answers.get(index) or None for index in range(1, expected + 1)]

    stripped = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(stripped) == expected:
        return [line or None for line in stripped]
    return [None] * expected


def make_translator(env: Mapping[str, str] | None = None) -> DeepSeekTranslator | None:
    """Собрать переводчик, если задан ключ. Нет ключа — None, и это нормально.

    None здесь — не ошибка, а выключенная возможность: источники с полем
    translation просто сохранятся на языке оригинала.
    """
    environment = os.environ if env is None else env
    api_key = (environment.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        logger.info("DEEPSEEK_API_KEY не задан — перевод источников недоступен")
        return None
    model = (environment.get("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    target = (environment.get("TRANSLATE_TO") or DEFAULT_TARGET_LANG).strip() or DEFAULT_TARGET_LANG
    return DeepSeekTranslator(api_key, model, target_lang=target)


def same_text(left: Any, right: Any) -> bool:
    """Перевод не отличается от оригинала (модель не справилась или текст уже на месте).

    Нужно, чтобы не складывать в extra «оригинал», совпадающий с переводом: так
    ведут себя названия продуктов и короткие заголовки вроде «DocToolchain».
    """
    return _clean(left).lower() == _clean(right).lower()


async def translate_items(
    items: Sequence[Any],
    source_lang: str | None,
    translator: Any | None,
    *,
    keep_original: bool = True,
) -> int:
    """Перевести заголовки и описания материалов на месте. Возвращает число переводов.

    Заголовок и описание переводятся независимо: если описание не перевелось,
    удачный перевод заголовка не выбрасывается. Оригинал (если keep_original)
    уходит в extra — чтобы перевод можно было перепроверить и переделать.

    Меняет переданные Item на месте: пайплайну не нужно сопоставлять «до» и «после».
    """
    if translator is None or not items:
        return 0

    # Собираем плоский список строк: [заголовок1, описание1, заголовок2, ...],
    # но только непустые поля. Пустые не отправляем по двум причинам:
    #   * они сбивали соответствие «строка входа → строка выхода» (модель их
    #     пропускала, нумерация съезжала и терялась половина переводов);
    #   * за них всё равно платили бы токенами.
    # slots помнит, какой строке какой материал и какое поле соответствует.
    slots: list[tuple[Any, str]] = []          # (материал, поле)
    texts: list[str] = []
    for item in items:
        title = _clean(item.title)
        if title:
            slots.append((item, "title"))
            texts.append(title)
        description = _clean(item.description)
        if description:
            slots.append((item, "description"))
            texts.append(description)

    translated = await translator.translate_many(texts, source_lang)

    # id(item), а не индекс: один материал встречается в slots дважды (заголовок
    # и описание), и по id видно, что вышло с материалом в целом.
    done: set[int] = set()
    failed: set[int] = set()
    for (item, field), value in zip(slots, translated):
        index = id(item)
        if value is None:
            failed.add(index)
            continue
        if keep_original:
            if field == "title" and not same_text(value, item.title):
                item.extra["original_title"] = item.title
            if field == "description" and not same_text(value, item.description):
                item.extra["original_description"] = item.description
        if field == "title":
            item.title = value
        else:
            item.description = value
        done.add(index)

    for item in items:
        index = id(item)
        if index in failed and index not in done:
            # Ни одно поле не перевелось — материал сохранится на языке оригинала.
            _mark_failed(item)
        elif index in done:
            # Пометка в данных: по ней видно, что текст переведён, например с eng.
            item.extra["translated_from"] = normalize_language(source_lang) or (source_lang or "")

    count = len(done)
    if count:
        logger.info("переведено материалов: %s из %s", count, len(items))
    return count


def _mark_failed(item: Any) -> None:
    """Перевод не удался: материал остаётся на языке оригинала, но это видно в данных.

    Терять материал из-за сбоя перевода нельзя, поэтому единственное последствие —
    пометка в extra и предупреждение в лог.
    """
    item.extra["translation_failed"] = True
    logger.warning("перевод не удался для «%s» — сохранён оригинал", str(item.title)[:80])


def _clean(value: Any) -> str:
    """Схлопнуть переводы строк и пробелы и обрезать до лимита промпта."""
    return " ".join(str(value or "").split())[:DESCRIPTION_LIMIT]
