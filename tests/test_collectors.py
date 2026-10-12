"""Юнит-тесты коллекторов: маппинг, пагинация, повторы, RSS-кодировки, GitHub.

Сети не требуют: транспорт подменяется httpx.MockTransport.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from collectors import factory
from collectors.api_collector import APICollector
from collectors.base import CollectorError, dig, dig_list
from collectors.github_collector import GitHubCollector
from collectors.http import HttpError
from collectors.rss_collector import RSSCollector
from collectors.normalize import build_item
from config.loader import DedupConfig, Source
from database.db import to_iso, utc_now

ENV = {"GITHUB_TOKEN": "ghp_test"}
DEDUP = DedupConfig()


def make_source(**overrides) -> Source:
    data = dict(
        id="trudvsem_vacancies",
        name="Работа России",
        label="vacancies",
        type="api",
        url="https://opendata.trudvsem.ru/api/v1/vacancies",
        enabled=True,
        params={"text": "python", "limit": 2},
        headers={},
        fields={
            "title": "vacancy.job-name",
            "url": "vacancy.vac_url",
            "description": "vacancy.requirements",
            "created_at": "vacancy.creation-date",
        },
        extra_fields={
            "salary_min": "vacancy.salary_min",
            "salary_currency": "vacancy.currency",
            "region": "vacancy.region.name",
        },
        items_path="results.vacancies",
        pagination={},
        item_filters={},
    )
    data.update(overrides)
    return Source(**data)


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def json_response(payload, status: int = 200, headers=None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers or {})


def trudvsem_payload(count: int = 2, salary: int | None = 250000) -> dict:
    return {
        "meta": {"total": count},
        "results": {
            "vacancies": [
                {
                    "vacancy": {
                        "job-name": f"Python-разработчик {index}",
                        "vac_url": f"https://trudvsem.ru/vacancy/{index}",
                        "requirements": "<p>Опыт <b>Python</b> от 3 лет</p>",
                        "creation-date": "2026-10-08T10:00:00+03:00",
                        "salary_min": salary,
                        "currency": "«руб.»",
                        "region": {"name": "Самарская область"},
                    }
                }
                for index in range(1, count + 1)
            ]
        },
    }


RSS_XML = """<?xml version="1.0" encoding="windows-1251"?>
<rss version="2.0"><channel>
  <title>Лента</title>
  <item>
    <title>Новости технологий</title>
    <link>https://example.com/news/1?utm_source=rss</link>
    <description>Краткое описание</description>
    <pubDate>Thu, 09 Oct 2026 05:09:15 GMT</pubDate>
  </item>
</channel></rss>"""

EMPTY_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Пусто</title></channel></rss>"""


class TestDig:
    def test_nested_and_hyphenated_keys(self):
        payload = {"vacancy": {"job-name": "Разработчик", "region": {"name": "Москва"}}}
        assert dig(payload, "vacancy.job-name") == "Разработчик"
        assert dig(payload, "vacancy.region.name") == "Москва"

    def test_missing_paths_return_default(self):
        assert dig({"a": 1}, "a.b.c") is None
        assert dig({"a": 1}, "b", "нет") == "нет"
        assert dig(None, "a", 0) == 0

    def test_list_index(self):
        assert dig({"items": [{"id": 7}]}, "items.0.id") == 7
        assert dig({"items": [{"id": 7}]}, "items.5.id") is None

    def test_dig_list_variants(self):
        assert dig_list({"items": [1, 2]}, "items") == [1, 2]
        assert dig_list([1, 2], "") == [1, 2]
        assert dig_list({"results": {"vacancies": [{"a": 1}]}}, "results.vacancies") == [{"a": 1}]
        assert dig_list({}, "нет") == []


class TestAPICollectorMapping:
    async def test_maps_nested_items(self):
        client = make_client(lambda request: json_response(trudvsem_payload()))

        batch = await APICollector(make_source(), client=client, env=ENV).collect()

        assert batch.fetched == 2
        assert len(batch.raw_items) == 2
        raw = batch.raw_items[0]
        assert raw.title == "Python-разработчик 1"
        assert raw.url == "https://trudvsem.ru/vacancy/1"
        assert raw.description == "Опыт Python от 3 лет"        # теги вычищены
        assert raw.created_at == "2026-10-08T10:00:00+03:00"
        assert raw.source == "trudvsem_vacancies"
        assert raw.label == "vacancies"
        assert raw.extra["salary_min"] == 250000
        assert raw.extra["region"] == "Самарская область"

    async def test_item_without_title_is_skipped(self):
        payload = trudvsem_payload(1)
        payload["results"]["vacancies"][0]["vacancy"]["job-name"] = ""
        client = make_client(lambda request: json_response(payload))

        batch = await APICollector(make_source(), client=client, env=ENV).collect()

        assert batch.fetched == 1
        assert batch.raw_items == []
        assert batch.filtered == 1

    async def test_normalized_item_keeps_raw_url_but_dedups_by_clean(self):
        client = make_client(lambda request: json_response(trudvsem_payload(1)))
        batch = await APICollector(make_source(), client=client, env=ENV).collect()
        item = build_item(batch.raw_items[0], "trudvsem_vacancies", "vacancies", DEDUP)

        assert item.extra["salary_currency"] == "RUR"           # «руб.» нормализована
        assert item.created_at == "2026-10-08T07:00:00+00:00"   # +03:00 → UTC
        assert item.date_missing == 0


class TestAPICollectorFilters:
    async def test_salary_filter_drops_items_without_salary(self):
        payload = trudvsem_payload(2)
        payload["results"]["vacancies"][1]["vacancy"]["salary_min"] = None
        source = make_source(
            item_filters={"salary_min": 200000, "salary_currency": "RUR"},
        )
        client = make_client(lambda request: json_response(payload))

        batch = await APICollector(source, client=client, env=ENV).collect()

        assert batch.fetched == 2
        assert len(batch.raw_items) == 1
        assert batch.filtered == 1

    async def test_salary_filter_keeps_high_salary(self):
        payload = trudvsem_payload(1, salary=300000)
        source = make_source(item_filters={"salary_min": 200000, "salary_currency": "RUR"})
        client = make_client(lambda request: json_response(payload))

        batch = await APICollector(source, client=client, env=ENV).collect()

        assert len(batch.raw_items) == 1
        assert batch.filtered == 0


class TestAPICollectorPagination:
    async def test_offset_pagination_stops_on_short_page(self):
        seen_offsets: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            offset = request.url.params.get("offset")
            seen_offsets.append(offset)
            size = {None: 2, "2": 2, "4": 1}.get(offset, 0)
            return json_response(trudvsem_payload(size) if size else {"results": {"vacancies": []}})

        source = make_source(
            pagination={"offset_param": "offset", "max_pages": 3},
            params={"text": "python", "limit": 2},
        )
        client = make_client(handler)

        batch = await APICollector(source, client=client, env=ENV).collect()

        assert seen_offsets == [None, "2", "4"]
        assert batch.fetched == 5

    async def test_single_page_by_default(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url)
            return json_response(trudvsem_payload(2))

        client = make_client(handler)
        await APICollector(make_source(), client=client, env=ENV).collect()

        assert len(calls) == 1


class TestHttpRetries:
    async def test_retries_after_503(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, text="service unavailable")
            return json_response(trudvsem_payload(1))

        client = make_client(handler)
        batch = await APICollector(make_source(), client=client, env=ENV, attempts=3, backoff=0).collect()

        assert calls["n"] == 2
        assert batch.fetched == 1

    async def test_403_is_not_retried(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(403, text="forbidden")

        client = make_client(handler)
        with pytest.raises(HttpError):
            await APICollector(make_source(), client=client, env=ENV, attempts=3, backoff=0).collect()

        assert calls["n"] == 1

    async def test_non_json_answer_is_collector_error(self):
        client = make_client(lambda request: httpx.Response(200, text="<html>не JSON</html>"))

        with pytest.raises(CollectorError):
            await APICollector(make_source(), client=client, env=ENV).collect()


class TestRSSCollector:
    async def test_cp1251_feed_keeps_cyrillic(self):
        """Байты вместо response.text: иначе кириллица в cp1251-ленте ломается."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=RSS_XML.encode("cp1251"),
                headers={"content-type": "application/xml; charset=windows-1251"},
            )

        source = make_source(
            id="lenta_news",
            label="news",
            type="rss",
            url="https://lenta.ru/rss/news",
            fields={
                "title": "title",
                "url": "link",
                "description": "summary",
                "created_at": "published_parsed",
            },
            extra_fields={},
            items_path="",
        )
        client = make_client(handler)
        batch = await RSSCollector(source, client=client, env=ENV).collect()

        assert batch.fetched == 1
        raw = batch.raw_items[0]
        assert raw.title == "Новости технологий"
        assert raw.url == "https://example.com/news/1?utm_source=rss"
        assert raw.created_at is not None

        item = build_item(raw, "lenta_news", "news", DEDUP)
        assert item.created_at == "2026-10-09T05:09:15+00:00"
        assert item.date_missing == 0

    async def test_empty_feed_raises(self):
        client = make_client(lambda request: httpx.Response(200, content=EMPTY_RSS.encode("utf-8")))
        source = make_source(type="rss", items_path="", fields={"title": "title", "url": "link"})

        with pytest.raises(CollectorError, match="лента пуста"):
            await RSSCollector(source, client=client, env=ENV).collect()

    async def test_strips_tracking_from_dedup_hash(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=RSS_XML.encode("cp1251"))

        source = make_source(
            id="lenta_news",
            label="news",
            type="rss",
            url="https://lenta.ru/rss/news",
            fields={"title": "title", "url": "link", "description": "summary"},
            extra_fields={},
            items_path="",
        )
        client = make_client(handler)
        batch = await RSSCollector(source, client=client, env=ENV).collect()
        item = build_item(batch.raw_items[0], "lenta_news", "news", DEDUP)

        assert item.url == "https://example.com/news/1?utm_source=rss"      # исходный URL
        clean = build_item(batch.raw_items[0], "lenta_news", "news", DEDUP)
        assert item.dedup_hash == clean.dedup_hash


class TestGitHubCollector:
    async def test_since_is_substituted_with_date_from_lookback(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["q"] = request.url.params.get("q")
            captured["auth"] = request.headers.get("authorization")
            captured["accept"] = request.headers.get("accept")
            return json_response({"total_count": 1, "items": [{"full_name": "a/b", "html_url": "https://github.com/a/b"}]})

        source = make_source(
            id="github_trending",
            label="tech",
            type="github",
            url="https://api.github.com/search/repositories",
            params={"q": "language:python created:>{since}", "per_page": 1},
            headers={"Accept": "application/vnd.github+json"},
            fields={"title": "full_name", "url": "html_url", "description": "description", "created_at": "created_at"},
            extra_fields={},
            items_path="items",
        )
        client = make_client(handler)
        batch = await GitHubCollector(source, lookback_days=7, client=client, env=ENV).collect()

        expected_date = (utc_now() - timedelta(days=7)).date().isoformat()
        assert captured["q"] == f"language:python created:>{expected_date}"
        assert captured["auth"] == "Bearer ghp_test"
        assert captured["accept"] == "application/vnd.github+json"
        assert batch.fetched == 1

    async def test_without_token_no_authorization_header(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            return json_response({"items": []})

        source = make_source(type="github", params={"q": "language:python created:>{since}"}, items_path="items")
        client = make_client(handler)
        await GitHubCollector(source, lookback_days=1, client=client, env={}).collect()

        assert captured["auth"] is None

    async def test_rate_limit_error_is_descriptive(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                text='{"message":"API rate limit exceeded"}',
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1791522555"},
            )

        source = make_source(type="github", params={"q": "language:python created:>{since}"}, items_path="items")
        client = make_client(handler)

        with pytest.raises(HttpError) as exc:
            await GitHubCollector(source, lookback_days=1, client=client, env=ENV).collect()

        message = str(exc.value)
        assert "лимит запросов исчерпан" in message
        assert "X-RateLimit-Remaining=0" in message

    def test_since_date_uses_iso_format(self):
        source = make_source(type="github", params={"q": "language:python created:>{since}"})
        collector = GitHubCollector(source, lookback_days=30, env=ENV)

        query = collector.prepare_params(0)["q"]

        expected = (utc_now() - timedelta(days=30)).date().isoformat()
        assert query.endswith(f"created:>{expected}")
        assert "T" not in expected        # именно дата, иначе GitHub вернёт 422


class TestPerSourceTimeout:
    def test_source_timeout_overrides_default(self):
        collector = APICollector(make_source(timeout=25))
        assert collector.timeout == 25

    def test_default_timeout_is_used_when_not_set(self):
        collector = APICollector(make_source())
        assert collector.timeout == 10.0

    async def test_timeout_reaches_http_client(self):
        """Медленный источник: таймаут источника должен применяться в запросе."""
        import httpx

        source = make_source(timeout=42, params={"text": "python"})
        collector = APICollector(source)

        async with collector.client() as client:
            assert client.timeout.read == 42


class TestFactory:
    @pytest.mark.parametrize(
        ("source_type", "expected"),
        [("rss", RSSCollector), ("api", APICollector), ("github", GitHubCollector)],
    )
    def test_type_selects_class(self, source_type, expected):
        collector = factory.get_collector(make_source(type=source_type))
        assert isinstance(collector, expected)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="неизвестный type"):
            factory.get_collector(make_source(type="soap"))
