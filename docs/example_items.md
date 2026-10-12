# Пример собранных данных

Всего записей в базе на момент выгрузки: **1151**.

| Метка | Источник | Заголовок | Дата |
|---|---|---|---|
| `tech` | `habr` | Giper Baza − отечественная инфраструктура нового поколения | 2026-10-09 |
| `news` | `tass_news` | ВС РФ ударили по объектам портовой инфраструктуры Украины | 2026-10-09 |
| `tech` | `hackernews` | Agency offers staff two years pay if they can replace themselves with AI | 2026-10-09 |
| `news` | `lenta_news` | Российские дзюдоистки спустя 33 года повторили свой лучший результат на чемпиона | 2026-10-09 |
| `news` | `kommersant_news` | Politico: Трамп может лишить 100 тыс. украинских беженцев права остаться в США | 2026-10-09 |
| `competitors` | `vcru` | День 1689: больше трети российских компаний отказались от выплаты годовой премии | 2026-10-09 |
| `tech` | `github_trending` | hujjun/mcd-saver-strategist | 2026-10-09 |
| `vacancies` | `habr_career` | Требуется «ML-инженер (Python · PyTorch · fine-tuning )» (Москва) | 2026-10-06 |
| `vacancies` | `trudvsem_vacancies` | AQA python | 2026-09-29 |

## Распределение по источникам

| Источник | Записей |
|---|---|
| `kommersant_news` | 530 |
| `lenta_news` | 227 |
| `tass_news` | 173 |
| `hackernews` | 58 |
| `habr_career` | 51 |
| `habr` | 49 |
| `github_trending` | 39 |
| `vcru` | 14 |
| `trudvsem_vacancies` | 10 |

## Как выглядит запись целиком

```json
{
  "label": "tech",
  "source": "habr",
  "title": 'Giper Baza − отечественная инфраструктура нового поколения',
  "url": "https://habr.com/ru/articles/1092252/?utm_source=habrahabr&utm_medium=rss&utm_campaign=1092252",
  "created_at": "2026-10-09T06:28:28+00:00",
  "extra": {},
  "description": 'В цифровую эпоху приватность и надёжность хранения данных важны как никогда.\nСетевое разделение и недоверенные посредники ломают взаимодействие: санкции, ковров'
}
```

Файл сгенерирован скриптом из `data/aggregator.db`, значения не выдуманы.
