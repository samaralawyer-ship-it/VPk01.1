"""Тесты загрузки и валидации конфигурации."""

from __future__ import annotations

import pytest

from config import ConfigError, load_config, validate_cron
from config.loader import _find_duplicate_keys
from collectors.normalize import match_keywords

MIN_LABEL = {"id": "news", "title": "Новости", "emoji": "📰", "order": 1}
MIN_SOURCE = {
    "id": "tass_news",
    "name": "ТАСС",
    "label": "news",
    "type": "rss",
    "url": "https://tass.ru/rss/v2.xml",
    "enabled": True,
    "fields": {"title": "title", "url": "link", "description": "summary", "created_at": "published_parsed"},
}
MIN_QUERY = {
    "id": "daily_news",
    "label": "news",
    "schedule": "0 9 * * *",
    "limit": 10,
    "lookback_days": 1,
    "tg_chat_id": "${TG_CHAT_ID}",
}


def test_real_config_is_valid(project_config_dir, env):
    cfg = load_config(project_config_dir, env=env)

    assert [label.id for label in cfg.labels] == ["news", "vacancies", "tech", "competitors"]
    assert len(cfg.queries) == 4
    assert cfg.run_on_start is True
    assert len(cfg.enabled_sources) >= 2, "критерий №1: минимум 2 включённых источника"

    enabled_labels = {src.label for src in cfg.enabled_sources}
    assert len(enabled_labels) >= 3, "включённые источники должны покрывать разные метки"

    # каждый запрос должен иметь хотя бы один включённый источник
    for query in cfg.queries:
        assert cfg.sources_for_query(query), f"у запроса {query.id} нет источников"
        assert query.tg_chat_id == env["TG_CHAT_ID"]


def test_real_config_requires_chat_id(project_config_dir):
    with pytest.raises(ConfigError) as exc:
        load_config(project_config_dir, env={})
    assert "TG_CHAT_ID" in str(exc.value)


def test_valid_minimal_config(write_config, workdir, env):
    cfg = load_config(write_config(workdir / "cfg"), env=env)
    assert cfg.labels[0].id == "news"
    assert cfg.sources[0].type == "rss"
    assert cfg.queries[0].limit == 10


def test_unknown_label_in_source_rejected(write_config, workdir, env):
    bad_source = {**MIN_SOURCE, "label": "unknown"}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", sources=[bad_source]), env=env)
    assert "не объявлена в labels.yaml" in str(exc.value)


def test_duplicate_source_id_rejected(write_config, workdir, env):
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", sources=[MIN_SOURCE, MIN_SOURCE]), env=env)
    assert "дубликат id" in str(exc.value)


def test_unsupported_source_type_rejected(write_config, workdir, env):
    bad_source = {**MIN_SOURCE, "type": "soap"}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", sources=[bad_source]), env=env)
    assert "не поддерживается" in str(exc.value)


def test_missing_required_mapping_rejected(write_config, workdir, env):
    bad_source = {**MIN_SOURCE, "fields": {"title": "title"}}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", sources=[bad_source]), env=env)
    assert "обязательный ключ «url»" in str(exc.value)


def test_filter_without_extra_fields_rejected(write_config, workdir, env):
    bad_source = {**MIN_SOURCE, "item_filters": {"salary_min": 100000}}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", sources=[bad_source]), env=env)
    assert "extra_fields.salary_min" in str(exc.value)


def test_unknown_filter_key_rejected(write_config, workdir, env):
    bad_source = {
        **MIN_SOURCE,
        "extra_fields": {"coffee": "vacancy.coffee"},
        "item_filters": {"coffee": "arabica"},
    }
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", sources=[bad_source]), env=env)
    assert "неизвестный фильтр" in str(exc.value)


def test_include_unspecified_salary_must_be_bool(write_config, workdir, env):
    bad_source = {
        **MIN_SOURCE,
        "extra_fields": {"salary_min": "vacancy.salary_min"},
        "item_filters": {"salary_min": 100000, "include_unspecified_salary": "да"},
    }
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", sources=[bad_source]), env=env)
    assert "include_unspecified_salary" in str(exc.value)


def test_include_unspecified_salary_needs_no_extra_field(write_config, workdir, env):
    source = {
        **MIN_SOURCE,
        "extra_fields": {"salary_min": "vacancy.salary_min"},
        "item_filters": {"salary_min": 100000, "include_unspecified_salary": True},
    }
    cfg = load_config(write_config(workdir / "cfg", sources=[source]), env=env)
    assert cfg.sources[0].item_filters["include_unspecified_salary"] is True


def test_unknown_query_filter_rejected(write_config, workdir, env):
    bad_query = {**MIN_QUERY, "filters": {"bonus": 1}}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", queries=[bad_query]), env=env)
    assert "filters.bonus" in str(exc.value)


def test_bad_cron_rejected(write_config, workdir, env):
    bad_query = {**MIN_QUERY, "schedule": "0 9 * *"}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", queries=[bad_query]), env=env)
    assert "cron" in str(exc.value)


def test_unknown_source_in_query_rejected(write_config, workdir, env):
    bad_query = {**MIN_QUERY, "sources": ["nope"]}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", queries=[bad_query]), env=env)
    assert "не найден в sources.yaml" in str(exc.value)


def test_unknown_keyword_key_rejected(write_config, workdir, env):
    bad_query = {**MIN_QUERY, "keywords": {"maybe": ["x"]}}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", queries=[bad_query]), env=env)
    assert "неизвестный ключ" in str(exc.value)


def test_keywords_must_be_string_lists(write_config, workdir, env):
    bad_query = {**MIN_QUERY, "keywords": {"any": [1, 2]}}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", queries=[bad_query]), env=env)
    assert "списком строк" in str(exc.value)


def test_keyword_group_is_accepted(write_config, workdir, env):
    query = {**MIN_QUERY, "keywords": {"any": ["нейросет", ["машинн", "обучени"]]}}
    cfg = load_config(write_config(workdir / "cfg", queries=[query]), env=env)

    assert cfg.queries[0].keywords["any"] == ["нейросет", ["машинн", "обучени"]]


def test_empty_keyword_group_is_rejected(write_config, workdir, env):
    query = {**MIN_QUERY, "keywords": {"any": [[]]}}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", queries=[query]), env=env)
    assert "групп строк" in str(exc.value)


def test_keyword_group_with_numbers_is_rejected(write_config, workdir, env):
    query = {**MIN_QUERY, "keywords": {"any": [["машинн", 5]]}}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", queries=[query]), env=env)
    assert "групп строк" in str(exc.value)


def test_bad_timeout_rejected(write_config, workdir, env):
    bad_source = {**MIN_SOURCE, "timeout": "долго"}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", sources=[bad_source]), env=env)
    assert "timeout" in str(exc.value)


class TestKeywordsDocumentedBehaviour:
    """Проверки ровно тех правил, что описаны в README (раздел «Правила keywords»).

    Смысл: документация и код не должны разъезжаться. Если кто-то поменяет
    SHORT_KEYWORD_LENGTH или логику групп, эти тесты упадут вместе с README.
    """

    def test_all_four_keys_are_documented_and_accepted(self, write_config, workdir, env):
        """В README заявлены any, all, exclude, fields — все должны работать."""
        query = {
            **MIN_QUERY,
            "keywords": {
                "any": ["нейросет", "ИИ"],
                "all": [["машинн", "обучени"]],
                "exclude": ["промокод", "гороскоп"],
                "fields": ["title", "description"],
            },
        }
        cfg = load_config(write_config(workdir / "cfg", queries=[query]), env=env)

        keywords = cfg.queries[0].keywords
        assert set(keywords) == {"any", "all", "exclude", "fields"}

    def test_short_key_does_not_match_inside_word(self):
        """«ИИ» не должно срабатывать на «России» — это главный пример в README."""
        assert match_keywords({"title": "ИИ-агент для кода"}, {"any": ["ИИ"]}) is True
        assert match_keywords({"title": "Ситуация в России"}, {"any": ["ИИ"]}) is False
        assert match_keywords({"title": "Туркмении посвящается"}, {"any": ["ИИ"]}) is False

    def test_case_does_not_matter(self):
        """README обещает, что «ИИ» и «ии» — один и тот же ключ."""
        for title in ("ИИ-агент", "ии-агент", "Ии-агент"):
            assert match_keywords({"title": title}, {"any": ["ИИ"]}) is True

    def test_long_key_matches_word_forms(self):
        """«нейросет» ловит все словоформы — обещание из README."""
        for title in ("Нейросеть", "Нейросети", "Нейросетевой"):
            assert match_keywords({"title": title}, {"any": ["нейросет"]}) is True

    def test_phrase_group_needs_all_words(self):
        """Группа = фраза: «машинн» + «обучени» ловит «машинного обучения»."""
        assert match_keywords({"title": "Методы машинного обучения"}, {"any": [["машинн", "обучени"]]}) is True
        assert match_keywords({"title": "Машинное зрение"}, {"any": [["машинн", "обучени"]]}) is False

    def test_exclude_is_stronger_than_any(self):
        """README: материал под exclude отбрасывается, даже если попал под any."""
        keywords = {"any": ["нейросет"], "exclude": ["промокод"]}

        assert match_keywords({"title": "Нейросети и промокод"}, keywords) is False
        assert match_keywords({"title": "Нейросети без скидок"}, keywords) is True

    def test_any_and_all_work_together(self):
        keywords = {"any": ["нейросет"], "all": [["машинн", "обучени"]]}

        assert match_keywords({"title": "Машинное обучение на нейросетях"}, keywords) is True
        assert match_keywords({"title": "Нейросети для игр"}, keywords) is False

    def test_fields_restricts_search(self):
        """Если fields задан, описание не учитывается."""
        values = {"title": "Обычный заголовок", "description": "внутри есть нейросет"}

        assert match_keywords(values, {"any": ["нейросет"]}) is True
        assert match_keywords(values, {"any": ["нейросет"], "fields": ["title"]}) is False

    def test_no_keywords_lets_everything_through(self):
        assert match_keywords({"title": "Что угодно"}, {}) is True
        assert match_keywords({"title": "Что угодно"}, {"exclude": []}) is True

    def test_substring_catches_extra_words(self):
        """Обратная сторона подстрочного поиска, о которой предупреждает README."""
        assert match_keywords({"title": "Contest results"}, {"any": ["test"]}) is True
        assert match_keywords({"title": "Реагент в воде"}, {"any": ["агент"]}) is True


class TestDuplicateKeys:
    """PyYAML молча оставляет последнее значение, поэтому дубли ловим сами.

    Реальный случай: в queries.yaml ключ «any» был записан четыре раза
    («промпт», «prompt», «python», «питон»), выжил только последний —
    три ключа исчезли без единого сообщения.
    """

    def test_detects_duplicate_in_keywords(self):
        text = """
queries:
  - id: daily_vacancies
    keywords:
      any: ["промпт"]
      any: ["python"]
"""
        found = _find_duplicate_keys(text)

        assert len(found) == 1
        key, line = found[0]
        assert key.endswith("keywords.any")
        assert line == 5                     # номер строки указывает на первый дубль

    def test_detects_duplicate_query_field(self):
        found = _find_duplicate_keys("queries:\n  - id: a\n    limit: 10\n    limit: 20\n")

        assert len(found) == 1
        assert found[0][0].endswith("limit")

    def test_same_key_in_different_objects_is_not_duplicate(self):
        """«id» есть у каждого запроса — это разные объекты, не дубль."""
        text = """
queries:
  - id: first
    limit: 10
  - id: second
    limit: 10
"""
        assert _find_duplicate_keys(text) == []

    def test_same_key_at_different_levels_is_not_duplicate(self):
        text = "defaults:\n  limit: 10\nqueries:\n  - id: a\n    limit: 20\n"

        assert _find_duplicate_keys(text) == []

    def test_clean_config_has_no_duplicates(self):
        text = """
queries:
  - id: a
    keywords:
      any: ["x"]
      exclude: ["y"]
      fields: [title]
"""
        assert _find_duplicate_keys(text) == []

    def test_broken_yaml_does_not_crash_detector(self):
        assert _find_duplicate_keys("a: [1, 2\nb: 3\n") == []

    def test_duplicate_is_reported_as_config_error(self, workdir, env):
        directory = workdir / "dup"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "labels.yaml").write_text(
            "labels:\n  - id: news\n    title: Новости\n    emoji: \"\"\n    order: 1\n",
            encoding="utf-8",
        )
        (directory / "sources.yaml").write_text(
            "dedup:\n  strip_params: [utm_source]\n"
            "sources:\n  - id: tass_news\n    name: ТАСС\n    label: news\n    type: rss\n"
            "    url: https://tass.ru/rss/v2.xml\n    fields:\n      title: title\n      url: link\n",
            encoding="utf-8",
        )
        (directory / "queries.yaml").write_text(
            "run_on_start: false\n"
            "queries:\n  - id: daily_news\n    label: news\n    schedule: \"0 9 * * *\"\n"
            "    tg_chat_id: \"123\"\n    limit: 10\n    limit: 20\n",
            encoding="utf-8",
        )

        with pytest.raises(ConfigError) as exc:
            load_config(directory, env=env)

        assert "записан дважды" in str(exc.value)
        assert "limit" in str(exc.value)

    def test_real_config_has_no_duplicate_keys(self, project_config_dir, env):
        """Страховка от повторения: любой дубль в конфиге проекта сломает этот тест."""
        cfg = load_config(project_config_dir, env=env)

        assert len(cfg.queries) == 4
        for query in cfg.queries:
            assert query.lookback_days >= 1

    def test_real_config_keywords_are_complete(self, project_config_dir, env):
        """В вакансиях ключи «промпт/prompt/python/питон» должны работать все четыре."""
        cfg = load_config(project_config_dir, env=env)
        vacancies = next(q for q in cfg.queries if q.id == "daily_vacancies")

        assert vacancies.keywords["any"] == ["промпт", "prompt", "python", "питон"]


def test_positive_timeout_accepted(write_config, workdir, env):
    cfg = load_config(write_config(workdir / "cfg", sources=[{**MIN_SOURCE, "timeout": 30}]), env=env)
    assert cfg.sources[0].timeout == 30.0


def test_translation_code_accepted(write_config, workdir, env):
    cfg = load_config(
        write_config(workdir / "cfg", sources=[{**MIN_SOURCE, "translation": "eng"}]), env=env
    )
    assert cfg.sources[0].translation == "eng"


def test_translation_code_is_lowercased(write_config, workdir, env):
    cfg = load_config(
        write_config(workdir / "cfg", sources=[{**MIN_SOURCE, "translation": "  ENG "}]), env=env
    )
    assert cfg.sources[0].translation == "eng"


def test_two_letter_translation_code_accepted(write_config, workdir, env):
    cfg = load_config(write_config(workdir / "cfg", sources=[{**MIN_SOURCE, "translation": "de"}]), env=env)
    assert cfg.sources[0].translation == "de"


def test_unknown_translation_code_rejected(write_config, workdir, env):
    with pytest.raises(ConfigError) as exc:
        load_config(
            write_config(workdir / "cfg", sources=[{**MIN_SOURCE, "translation": "klingon"}]), env=env
        )
    assert "translation" in str(exc.value)
    assert "ISO 639-3" in str(exc.value)


def test_translation_absent_by_default(write_config, workdir, env):
    cfg = load_config(write_config(workdir / "cfg"), env=env)
    assert cfg.sources[0].translation == ""


def test_real_config_translates_hackernews(project_config_dir, env):
    cfg = load_config(project_config_dir, env=env)
    assert cfg.source("hackernews").translation == "eng"


def test_empty_queries_rejected(write_config, workdir, env):
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", queries=[]), env=env)
    assert "ни одного запроса" in str(exc.value)


def test_all_enabled_sources_disabled_rejected(write_config, workdir, env):
    off = {**MIN_SOURCE, "enabled": False}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", sources=[off]), env=env)
    assert "включённого источника" in str(exc.value)


def test_run_on_start_must_be_bool(write_config, workdir, env):
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", run_on_start="yes"), env=env)
    assert "run_on_start" in str(exc.value)


def test_all_problems_reported_together(write_config, workdir, env):
    bad_source = {**MIN_SOURCE, "label": "unknown", "type": "soap"}
    bad_query = {**MIN_QUERY, "schedule": "0 99 * * *"}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(workdir / "cfg", sources=[bad_source], queries=[bad_query]), env=env)
    message = str(exc.value)
    assert "не объявлена в labels.yaml" in message
    assert "не поддерживается" in message
    assert "часы" in message


def test_defaults_are_overridden_by_query(write_config, workdir, env):
    query = {**MIN_QUERY, "lookback_days": 7, "limit": 3}
    cfg = load_config(
        write_config(workdir / "cfg", queries=[query], defaults={"limit": 10, "lookback_days": 1, "summarize": False}),
        env=env,
    )
    assert cfg.queries[0].lookback_days == 7
    assert cfg.queries[0].limit == 3


def test_sources_for_query_by_label_and_explicit(write_config, workdir, env):
    second = {**MIN_SOURCE, "id": "lenta_news", "name": "Lenta", "url": "https://lenta.ru/rss/news"}
    other_label_source = {
        **MIN_SOURCE,
        "id": "github_trending",
        "name": "GitHub",
        "type": "github",
        "label": "tech",
        "url": "https://api.github.com/search/repositories",
    }
    labels = [MIN_LABEL, {"id": "tech", "title": "Технологии", "emoji": "🛠", "order": 2}]
    queries = [
        {**MIN_QUERY, "id": "by_label", "sources": []},
        {**MIN_QUERY, "id": "explicit", "sources": ["tass_news"]},
    ]
    cfg = load_config(
        write_config(
            workdir / "cfg",
            labels=labels,
            sources=[MIN_SOURCE, second, other_label_source],
            queries=queries,
        ),
        env=env,
    )

    by_label = cfg.sources_for_query(cfg.queries[0])
    assert {src.id for src in by_label} == {"tass_news", "lenta_news"}
    explicit = cfg.sources_for_query(cfg.queries[1])
    assert [src.id for src in explicit] == ["tass_news"]


@pytest.mark.parametrize(
    "expr",
    ["0 9 * * *", "*/15 * * * *", "0 9,18 * * 1-5", "30 8 1 * *", "0 0 * * 0"],
)
def test_validate_cron_accepts(expr):
    assert validate_cron(expr) is None


@pytest.mark.parametrize(
    "expr",
    ["", "0 9 * *", "0 99 * * *", "* * * * * *", "0 9 * * abc", "60 * * * *", "0 9 2 * *-"],
)
def test_validate_cron_rejects(expr):
    assert validate_cron(expr) is not None
