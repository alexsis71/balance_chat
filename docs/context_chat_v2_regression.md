# Context Chat V2 — финальный regression checkpoint

Дата: 2026-08-03.

## Новая версия

- Полный набор `balance_chat`: **84 passed**, одно внешнее deprecation-warning
  Starlette/httpx.
- Focused grouping: **25 passed**.
- Backend `127.0.0.1:8790`: health `ok`; PostgreSQL context store, metadata
  bundle `2026.07.7`, Qwen и result memory готовы.
- DB-backed сценарий «поставки по областям» → «суммируй данные по областям»:
  оба turn имеют status `ok`; 100 строк объединены в 63 canonical GEO ID.
  Ульяновские aliases объединены в «Ульяновская область» по curated metadata.
  Второй turn использует authoritative `ResultReference`, не повторяет DB/LLM
  запрос и не публикует provenance.

## Sibling pipeline

Полный прогон текущего рабочего дерева: **441 passed, 4 skipped, 13 failed**,
58 subtests passed. Перед прогоном в `pipeline` уже находились пользовательские
изменения; они сохранены без отката.

Контрольный прогон упавших кейсов на чистом архиве `pipeline HEAD` показал 10
существующих падений:

- два ожидания SQL function в `test_article_normalizer.py`;
- три ожидания SQLIR/reverse repair в `test_pipeline_api.py`;
- incoming article, route alias и semantic-equivalence в
  `test_pipeline_unified.py`;
- два устаревших deployment expectation для прежнего nvidia/modelopt профиля,
  тогда как зафиксирован профиль `unsloth/...-NVFP4-Fast`/compressed-tensors.

Только на текущем dirty upstream добавляются три падения:

- `test_context_article_override_clears_analyzer_only_geo`;
- `test_context_extremum_override_restores_explicit_article_policy`;
- `test_reverse_direction_resolves_new_article_or_fails_before_sql`.

Они являются явным compatibility risk перед переключением production entry
point. V2 не маскирует их legacy fallback.
