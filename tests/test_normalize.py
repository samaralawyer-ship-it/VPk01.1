"""Юнит-тесты нормализации: URL, текст, валюта, даты, фильтры, ключевые слова."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from collectors.normalize import (
    build_item,
    clean_text,
    dedup_hash,
    match_keywords,
    normalize_currency,
    normalize_url,
    parse_date,
    passes_filters,
    truncate,
)
from config.loader import DedupConfig
from database.db import to_iso, utc_now
from database.models import RawItem

DEDUP = DedupConfig(strip_params=("utm_source", "utm_medium", "gclid", "fbclid", "from", "ref"))


class TestNormalizeUrl:
    def test_scheme_and_host_cleanup(self):
        assert normalize_url("http://www.Example.com/Path/", DEDUP) == "https://example.com/Path"

    def test_mobile_and_amp_prefixes(self):
        assert normalize_url("https://m.example.com/a", DEDUP) == "https://example.com/a"
        assert normalize_url("https://amp.example.com/a", DEDUP) == "https://example.com/a"

    def test_tracking_params_removed(self):
        url = "https://example.com/a?utm_source=tg&utm_medium=soc&gclid=1&keep=yes"
        assert normalize_url(url, DEDUP) == "https://example.com/a?keep=yes"

    def test_remaining_params_sorted(self):
        assert normalize_url("https://example.com/a?b=2&a=1", DEDUP) == "https://example.com/a?a=1&b=2"

    def test_fragment_removed(self):
        assert normalize_url("https://example.com/a#comments", DEDUP) == "https://example.com/a"

    def test_root_slash_removed(self):
        assert normalize_url("https://example.com/", DEDUP) == "https://example.com"

    def test_path_case_preserved(self):
        assert normalize_url("https://example.com/News/Item", DEDUP) == "https://example.com/News/Item"

    def test_empty_and_none(self):
        assert normalize_url(None, DEDUP) == ""
        assert normalize_url("   ", DEDUP) == ""

    def test_idempotent(self):
        once = normalize_url("http://www.example.com/a/?utm_source=x&b=1#top", DEDUP)
        assert normalize_url(once, DEDUP) == once

    def test_settings_can_be_disabled(self):
        loose = DedupConfig(strip_fragment=False, force_https=False, strip_www=False,
                            strip_trailing_slash=False, sort_query=False, strip_params=())
        assert normalize_url("http://www.example.com/a/?b=1#top", loose) == "http://www.example.com/a/?b=1#top"


class TestDedupHash:
    def test_same_url_with_tracking_is_same_hash(self):
        first = dedup_hash("https://example.com/a?utm_source=tg", "Заголовок", "tass_news", DEDUP)
        second = dedup_hash("https://example.com/a", "Заголовок", "tass_news", DEDUP)
        assert first == second

    def test_different_urls_differ(self):
        assert dedup_hash("https://example.com/a", "X", "s", DEDUP) != dedup_hash("https://example.com/b", "X", "s", DEDUP)

    def test_without_url_uses_title_and_source(self):
        first = dedup_hash(None, "  Python-разработчик ", "trudvsem_vacancies", DEDUP)
        second = dedup_hash("", "python-разработчик", "trudvsem_vacancies", DEDUP)
        assert first == second

    def test_same_title_different_source_differs(self):
        assert dedup_hash(None, "Python", "a", DEDUP) != dedup_hash(None, "Python", "b", DEDUP)


class TestCleanText:
    def test_strips_html_tags(self):
        assert clean_text("<p>Текст</p> <b>жирный</b>") == "Текст\nжирный"

    def test_strips_highlighttext_from_api(self):
        assert clean_text("Опыт <highlighttext>Python</highlighttext> от 3 лет") == "Опыт Python от 3 лет"

    def test_unescapes_entities(self):
        assert clean_text("&laquo;ИИ&raquo; &amp; ML") == "«ИИ» & ML"

    def test_collapses_spaces_and_blank_lines(self):
        assert clean_text("a\u00a0\u00a0  b\n\n\n\nc") == "a b\n\nc"

    def test_none_and_non_string(self):
        assert clean_text(None) == ""
        assert clean_text(42) == "42"


class TestTruncate:
    def test_short_text_untouched(self):
        assert truncate("коротко", 50) == "коротко"

    def test_long_text_cut_by_word(self):
        result = truncate("слово " * 40, 30)
        assert result.endswith("…")
        assert len(result) <= 31
        assert " " not in result[-2:]


class TestCurrency:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("«руб.»", "RUR"),
            ("руб", "RUR"),
            ("₽", "RUR"),
            ("RUR", "RUR"),
            ("rub", "RUR"),
            ("USD", "USD"),
            ("$", "USD"),
            ("€", "EUR"),
            ("тенге", "KZT"),
            ("", ""),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_currency(raw) == expected


class TestParseDate:
    def test_iso_with_z(self):
        iso, missing = parse_date("2026-10-09T05:09:15Z")
        assert (iso, missing) == ("2026-10-09T05:09:15+00:00", 0)

    def test_iso_with_offset_converted_to_utc(self):
        iso, missing = parse_date("2026-10-09T08:09:15+03:00")
        assert (iso, missing) == ("2026-10-09T05:09:15+00:00", 0)

    def test_struct_time_from_feedparser(self):
        iso, missing = parse_date(time.struct_time((2026, 10, 9, 5, 9, 15, 0, 0, 0)))
        assert (iso, missing) == ("2026-10-09T05:09:15+00:00", 0)

    def test_rfc822(self):
        iso, missing = parse_date("Thu, 09 Oct 2026 05:09:15 GMT")
        assert (iso, missing) == ("2026-10-09T05:09:15+00:00", 0)

    def test_unix_timestamp(self):
        iso, missing = parse_date(1791522555)
        assert missing == 0
        assert iso.endswith("+00:00")

    def test_russian_format(self):
        iso, missing = parse_date("09.10.2026 08:09")
        assert (iso, missing) == ("2026-10-09T08:09:00+00:00", 0)

    def test_empty_means_missing(self):
        iso, missing = parse_date("")
        assert missing == 1 and iso.endswith("+00:00")
        assert parse_date(None)[1] == 1

    def test_garbage_means_missing(self):
        assert parse_date("не дата")[1] == 1

    def test_too_old_means_missing(self):
        assert parse_date("1999-01-01T00:00:00+00:00")[1] == 1

    def test_far_future_means_missing(self):
        future = to_iso(utc_now() + timedelta(days=3))
        assert parse_date(future)[1] == 1


class TestBuildItem:
    def test_keeps_raw_url_but_hashes_normalized(self):
        raw = RawItem(
            title="Заголовок",
            url="https://example.com/a?utm_source=tg",
            description="<p>описание</p>",
            created_at="2026-10-09T05:09:15Z",
            source="tass_news",
            label="news",
        )
        item = build_item(raw, "tass_news", "news", DEDUP)

        assert item.url == "https://example.com/a?utm_source=tg"      # исходный URL сохранён
        assert item.dedup_hash == dedup_hash("https://example.com/a", "Заголовок", "tass_news", DEDUP)
        assert item.description == "описание"
        assert item.created_at == "2026-10-09T05:09:15+00:00"
        assert item.date_missing == 0
        assert item.fetched_at.endswith("+00:00")

    def test_missing_date_is_flagged(self):
        item = build_item(RawItem(title="T", url="https://e.com/1", source="s", label="news"), "s", "news", DEDUP)
        assert item.date_missing == 1


class TestPassesFilters:
    def test_salary_min_pass_and_fail(self):
        assert passes_filters({"salary_min": 250000}, {"salary_min": 200000}) is True
        assert passes_filters({"salary_min": 150000}, {"salary_min": 200000}) is False

    def test_salary_absent_fails_by_default(self):
        assert passes_filters({}, {"salary_min": 200000}) is False

    def test_salary_absent_allowed_by_flag(self):
        filters = {"salary_min": 200000, "salary_max": 350000, "include_unspecified_salary": True}
        assert passes_filters({}, filters) is True

    def test_zero_salary_means_unspecified(self):
        filters = {"salary_min": 200000, "include_unspecified_salary": True}
        assert passes_filters({"salary_min": 0, "salary_max": 0}, filters) is True
        assert passes_filters({"salary_min": 0}, {"salary_min": 200000}) is False

    def test_salary_max_uses_lowest_known_bound(self):
        assert passes_filters({"salary_max": 300000}, {"salary_max": 400000}) is True
        assert passes_filters({"salary_min": 500000}, {"salary_max": 400000}) is False

    def test_range_filters_use_overlap(self):
        filters = {"salary_min": 200000, "salary_max": 350000}
        assert passes_filters({"salary_min": 100000, "salary_max": 250000}, filters) is True
        assert passes_filters({"salary_min": 400000, "salary_max": 500000}, filters) is False
        assert passes_filters({"salary_min": 50000, "salary_max": 150000}, filters) is False

    def test_source_with_only_lower_bound(self):
        # «от 100 000» без верхней границы: вакансия дотягивает до потолка
        assert passes_filters({"salary_min": 100000}, {"salary_min": 50000, "salary_max": 200000}) is True

    def test_currency_checked_when_salary_specified(self):
        assert passes_filters(
            {"salary_min": 250000, "salary_currency": "«руб.»"}, {"salary_currency": "RUR"}
        ) is True
        assert passes_filters({"salary_min": 250000, "salary_currency": "USD"}, {"salary_currency": "RUR"}) is False

    def test_currency_follows_unspecified_flag(self):
        assert passes_filters({}, {"salary_currency": "RUR"}) is False
        assert passes_filters({}, {"salary_currency": "RUR", "include_unspecified_salary": True}) is True

    def test_region_substring_case_insensitive(self):
        assert passes_filters({"region": "Самарская область"}, {"region": "самарская"}) is True
        assert passes_filters({"region": "Москва"}, {"region": "самарская"}) is False

    def test_region_applies_even_when_salary_unspecified(self):
        filters = {"salary_min": 200000, "include_unspecified_salary": True, "region": "Москва"}
        assert passes_filters({"region": "Москва"}, filters) is True
        assert passes_filters({"region": "Самара"}, filters) is False

    def test_no_filters_passes_everything(self):
        assert passes_filters({}, {}) is True


class TestNormalizeExtra:
    def test_zero_salary_is_not_stored(self):
        raw = RawItem(
            title="Python-разработчик",
            url="https://trudvsem.ru/vacancy/1",
            created_at="2026-10-08T10:00:00+03:00",
            extra={"salary_min": 250000, "salary_max": 0, "salary_currency": "«руб.»", "region": "Москва"},
        )
        item = build_item(raw, "trudvsem_vacancies", "vacancies", DEDUP)

        assert item.extra == {
            "salary_min": 250000,
            "salary_currency": "RUR",
            "region": "Москва",
        }

    def test_empty_values_are_dropped(self):
        raw = RawItem(title="Вакансия", extra={"region": "", "company": None, "salary_min": None})
        item = build_item(raw, "s", "vacancies", DEDUP)
        assert item.extra == {}


class TestMatchKeywords:
    def test_any(self):
        cfg = {"any": ["python", "asyncio"]}
        assert match_keywords({"title": "Python-разработчик"}, cfg) is True
        assert match_keywords({"title": "Менеджер"}, cfg) is False

    def test_case_insensitive(self):
        assert match_keywords({"title": "PYTHON"}, {"any": ["python"]}) is True

    def test_exclude_wins(self):
        cfg = {"any": ["python"], "exclude": ["1С"]}
        assert match_keywords({"title": "Python и 1С"}, cfg) is False

    def test_exclude_works_without_any(self):
        assert match_keywords({"title": "Скидки"}, {"exclude": ["скидк"]}) is False
        assert match_keywords({"title": "Новость"}, {"exclude": ["скидк"]}) is True

    def test_all(self):
        cfg = {"all": ["python", "django"]}
        assert match_keywords({"title": "Python Django"}, cfg) is True
        assert match_keywords({"title": "Python Flask"}, cfg) is False

    def test_fields_restrict_search_area(self):
        cfg = {"any": ["python"], "fields": ["title"]}
        assert match_keywords({"title": "Новость", "description": "Python"}, cfg) is False
        assert match_keywords({"title": "Новость", "description": "Python"}, {"any": ["python"]}) is True

    def test_stem_matches_russian_forms(self):
        cfg = {"any": ["нейросет"]}
        assert match_keywords({"title": "Нейросети научились"}, cfg) is True
        assert match_keywords({"title": "Нейросетевой подход"}, cfg) is True

    def test_short_keyword_matches_by_word_boundary(self):
        """«ИИ» не должен ловить «России», «информации», «Туркмении»."""
        cfg = {"any": ["ии"]}
        assert match_keywords({"title": "Внедрение ИИ в медицину"}, cfg) is True
        assert match_keywords({"title": "ИИ-помощник для ветеринаров"}, cfg) is True
        assert match_keywords({"title": "Путин и лидеры стран СНГ"}, cfg) is False
        assert match_keywords({"description": "Заседание открыл президент Туркмении"}, cfg) is False
        assert match_keywords({"description": "Источник информации о России"}, cfg) is False

    def test_short_exclude_also_uses_boundaries(self):
        cfg = {"exclude": ["ии"]}
        assert match_keywords({"title": "Новости России"}, cfg) is True
        assert match_keywords({"title": "Развитие ИИ"}, cfg) is False

    def test_phrase_group_matches_any_case_form(self):
        cfg = {"any": [["машинн", "обучени"]]}
        assert match_keywords({"title": "Рынок машинного обучения растёт"}, cfg) is True
        assert match_keywords({"title": "Машинное обучение в промышленности"}, cfg) is True
        assert match_keywords({"title": "Курсы машинного обучения"}, cfg) is True

    def test_phrase_group_needs_all_words(self):
        cfg = {"any": [["машинн", "обучени"]]}
        assert match_keywords({"title": "Машинное зрение на заводе"}, cfg) is False
        assert match_keywords({"title": "Обучение сотрудников"}, cfg) is False

    def test_full_phrase_in_nominative_misses_cases(self):
        """Иллюстрация проблемы: полная фраза ловит только саму себя."""
        cfg = {"any": ["искусственный интеллект"]}
        assert match_keywords({"title": "Искусственный интеллект в медицине"}, cfg) is True
        assert match_keywords({"title": "Этика искусственного интеллекта"}, cfg) is False

    def test_group_replaces_full_phrase(self):
        cfg = {"any": [["искусственн", "интеллект"]]}
        assert match_keywords({"title": "Искусственный интеллект в медицине"}, cfg) is True
        assert match_keywords({"title": "Этика искусственного интеллекта"}, cfg) is True
        assert match_keywords({"title": "Искусственный отбор"}, cfg) is False

    def test_groups_and_plain_keys_together(self):
        cfg = {"any": ["нейросет", ["машинн", "обучени"]]}
        assert match_keywords({"title": "Нейросети в быту"}, cfg) is True
        assert match_keywords({"title": "Методы машинного обучения"}, cfg) is True
        assert match_keywords({"title": "Ничего похожего"}, cfg) is False

    def test_exclude_accepts_group(self):
        cfg = {"exclude": [["промокод", "скидк"]]}
        assert match_keywords({"title": "Промокод на скидку"}, cfg) is False
        assert match_keywords({"title": "Промокод на подписку"}, cfg) is True

    def test_html_is_cleaned_before_match(self):
        assert match_keywords({"description": "<p>Про <b>Python</b></p>"}, {"any": ["python"]}) is True

    def test_empty_config_matches_everything(self):
        assert match_keywords({"title": "что угодно"}, {}) is True
        assert match_keywords({"title": "что угодно"}, None) is True
