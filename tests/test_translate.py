"""Тесты машинного перевода: батчи, разбор ответа, фолбэк, коды языков."""

from __future__ import annotations

import pytest

from database.models import Item
from digest.translate import (
    DeepSeekTranslator,
    LANGUAGE_CODES,
    language_name,
    make_translator,
    normalize_language,
    parse_translations,
    same_text,
    target_language_name,
    translate_items,
)

TRANSLATOR_ARGS = dict(api_key="test-key", client=object())


class FakeClient:
    """Подменённый клиент openai: отдаёт заранее заданные ответы и помнит промпты."""

    def __init__(self, answers: list[str | Exception]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.chat = self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        self.prompts.append(kwargs["messages"][-1]["content"])
        self.systems.append(kwargs["messages"][0]["content"])
        answer = self.answers.pop(0) if self.answers else '""'
        if isinstance(answer, Exception):
            raise answer

        class Message:
            content = answer
            reasoning_content = None

        class Choice:
            message = Message()
            finish_reason = "stop"

        class Response:
            choices = [Choice()]
            usage = None

        return Response()


def item(title: str, description: str = "") -> Item:
    return Item(
        title=title,
        source="hackernews",
        label="tech",
        created_at="2026-10-12T10:00:00+00:00",
        fetched_at="2026-10-12T10:00:00+00:00",
        dedup_hash="hash",
        description=description,
    )


class TestLanguageCodes:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("en", "eng"), ("eng", "eng"), ("ger", "deu"), ("de", "deu"), ("zh", "zho"),
         ("chi", "zho"), ("fr", "fra"), ("FRE", "fra"), ("JPN", "jpn"), ("", "")],
    )
    def test_normalize(self, value, expected):
        assert normalize_language(value) == expected

    def test_known_codes_have_names(self):
        for code in ("eng", "deu", "fra", "jpn", "zho"):
            assert language_name(code)

    def test_two_letter_resolves_to_name(self):
        assert language_name("en") == language_name("eng")

    def test_unknown_code_has_no_name(self):
        assert language_name("klingon") is None

    def test_target_language_default_is_russian(self):
        assert target_language_name("") == "русский"
        assert "русск" in DeepSeekTranslator("k", client=object()).build_system_prompt("eng")

    def test_every_supported_code_has_a_name(self):
        # коды, которые пропускает валидация конфига, должны быть понятны переводчику
        from config.loader import TRANSLATION_CODES

        unknown = {code for code in TRANSLATION_CODES if language_name(code) is None}
        assert not unknown, f"нет названия языка для: {sorted(unknown)}"


class TestParseTranslations:
    def test_json_array(self):
        assert parse_translations('["раз", "два"]', 2) == ["раз", "два"]

    def test_json_in_markdown_fence(self):
        assert parse_translations('```json\n["раз"]\n```', 1) == ["раз"]

    def test_json_with_extra_items_is_trimmed(self):
        assert parse_translations('["раз", "два", "три"]', 2) == ["раз", "два"]

    def test_json_shorter_than_expected_is_padded(self):
        assert parse_translations('["раз"]', 3) == ["раз", None, None]

    def test_numbered_lines_fallback(self):
        assert parse_translations("1. раз\n2. два", 2) == ["раз", "два"]

    def test_numbered_lines_out_of_order(self):
        assert parse_translations("2. два\n1. раз", 2) == ["раз", "два"]

    def test_plain_lines_when_count_matches(self):
        assert parse_translations("раз\nдва", 2) == ["раз", "два"]

    def test_empty_response(self):
        assert parse_translations("", 2) == [None, None]

    def test_garbage_response(self):
        assert parse_translations("не могу перевести", 2) == [None, None]

    def test_null_values_become_none(self):
        assert parse_translations('["раз", null]', 2) == ["раз", None]


class TestSummaryHelpers:
    def test_same_text_ignores_case_and_spaces(self):
        assert same_text("  Hello  world ", "hello world") is True
        assert same_text("Привет", "Hello") is False


class TestTranslateMany:
    async def test_translates_and_keeps_order(self):
        client = FakeClient(['["раз", "два", "три"]'])
        translator = DeepSeekTranslator(client=client, **{k: v for k, v in TRANSLATOR_ARGS.items() if k != "client"})

        result = await translator.translate_many(["one", "two", "three"], "eng")

        assert result == ["раз", "два", "три"]

    async def test_batches_large_input(self):
        client = FakeClient(['["a1", "a2"]', '["b1", "b2"]'])
        translator = DeepSeekTranslator(api_key="k", client=client, max_batch=2)

        result = await translator.translate_many(["1", "2", "3", "4"], "eng")

        assert result == ["a1", "a2", "b1", "b2"]
        assert len(client.prompts) == 2                       # два вызова, а не четыре

    async def test_api_failure_returns_none_for_chunk(self):
        client = FakeClient([RuntimeError("сеть недоступна")])
        translator = DeepSeekTranslator(api_key="k", client=client)

        assert await translator.translate_many(["one"], "eng") == [None]

    async def test_failure_in_one_chunk_does_not_break_other(self):
        client = FakeClient([RuntimeError("сбой"), '["второй"]'])
        translator = DeepSeekTranslator(api_key="k", client=client, max_batch=1)

        assert await translator.translate_many(["one", "two"], "eng") == [None, "второй"]

    async def test_prompt_is_json_array(self):
        client = FakeClient(['["раз"]'])
        translator = DeepSeekTranslator(api_key="k", client=client)

        await translator.translate_many(["One"], "eng")

        assert client.prompts[0] == '["One"]'
        assert "JSON" in client.systems[0]

    async def test_empty_input_makes_no_calls(self):
        client = FakeClient([])
        translator = DeepSeekTranslator(api_key="k", client=client)

        assert await translator.translate_many([], "eng") == []
        assert client.prompts == []

    async def test_blank_texts_are_skipped(self):
        client = FakeClient(['["раз"]'])
        translator = DeepSeekTranslator(api_key="k", client=client)

        result = await translator.translate_many(["One", "   "], "eng")

        assert result == ["раз", None]
        assert client.prompts[0] == '["One"]'


class TestTranslateItems:
    async def test_translates_title_and_description(self):
        client = FakeClient(['["Заголовок", "Описание"]'])
        translator = DeepSeekTranslator(api_key="k", client=client)
        item_one = item("Title", "Description")

        done = await translate_items([item_one], "eng", translator)

        assert done == 1
        assert item_one.title == "Заголовок"
        assert item_one.description == "Описание"
        assert item_one.extra["original_title"] == "Title"
        assert item_one.extra["original_description"] == "Description"
        assert item_one.extra["translated_from"] == "eng"

    async def test_empty_description_is_not_sent(self):
        """Пустое описание раньше сбивало нумерацию ответа — теперь не отправляется."""
        client = FakeClient(['["Первый", "Второй"]'])
        translator = DeepSeekTranslator(api_key="k", client=client)
        items = [item("One"), item("Two")]

        await translate_items(items, "eng", translator)

        assert client.prompts[0] == '["One", "Two"]'
        assert [i.title for i in items] == ["Первый", "Второй"]

    async def test_failed_translation_keeps_original_and_marks(self):
        client = FakeClient([RuntimeError("сбой")])
        translator = DeepSeekTranslator(api_key="k", client=client)
        item_one = item("Original title", "Original description")

        done = await translate_items([item_one], "eng", translator)

        assert done == 0
        assert item_one.title == "Original title"             # материал не потерян
        assert item_one.extra["translation_failed"] is True

    async def test_partial_response_keeps_translated_field(self):
        client = FakeClient(['["Заголовок", null]'])
        translator = DeepSeekTranslator(api_key="k", client=client)
        item_one = item("Title", "Description")

        done = await translate_items([item_one], "eng", translator)

        assert item_one.title == "Заголовок"                  # удачный перевод сохранён
        assert item_one.description == "Description"
        assert done == 1

    async def test_no_translator_means_no_changes(self):
        item_one = item("Title", "Description")

        assert await translate_items([item_one], "eng", None) == 0
        assert item_one.title == "Title"
        assert item_one.extra == {}

    async def test_empty_list(self):
        client = FakeClient([])
        translator = DeepSeekTranslator(api_key="k", client=client)

        assert await translate_items([], "eng", translator) == 0
        assert client.prompts == []

    async def test_original_not_stored_when_disabled(self):
        client = FakeClient(['["Заголовок", ""]'])
        translator = DeepSeekTranslator(api_key="k", client=client)
        item_one = item("Title")

        await translate_items([item_one], "eng", translator, keep_original=False)

        assert item_one.title == "Заголовок"
        assert "original_title" not in item_one.extra


class TestMakeTranslator:
    def test_no_key_means_none(self):
        assert make_translator({}) is None

    def test_key_creates_translator(self):
        translator = make_translator({"DEEPSEEK_API_KEY": "sk-test"})

        assert isinstance(translator, DeepSeekTranslator)
        assert translator.model == "deepseek-flash"

    def test_model_and_target_from_env(self):
        translator = make_translator(
            {"DEEPSEEK_API_KEY": "sk", "DEEPSEEK_MODEL": "deepseek-v4-pro", "TRANSLATE_TO": "eng"}
        )

        assert translator.model == "deepseek-v4-pro"
        assert translator.target_lang == "eng"
        assert "английский" in translator.build_system_prompt("deu")
