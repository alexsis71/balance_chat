# Архитектура новой версии

## Граница ответственности

`balance_chat` становится владельцем диалогового состояния и интерпретации.
Текущий `pipeline` пока остаётся владельцем resolver/execution, SQL/MCP,
PostgreSQL и `ResultEnvelope`.

```text
user turn
   │
   ▼
Context Contract V2 ── deterministic reducer ── optimistic store
   │
   ▼
compatibility projection
   │  supported scalar shape
   ▼
pipeline unified_strict ── MCP/PostgreSQL

unsupported multi-operand/grouped shape ──► explicit UnsupportedLegacyShape
```

## Почему контракт многосоставной

Один запрос может одновременно содержать несколько предметов сравнения:
например, поставки в Казань и собственные нужды ГП ТГ Казань. Поэтому
`AnalysisIntent` содержит `operands[]`, а сравнение ссылается на них по
стабильным `operand_id`. GEO, период или статья не обнуляются как побочный
эффект изменения другого поля.

Периоды имеют exclusive-end семантику. Сравнение явно фиксирует направление
дельты `target - baseline` и базу процента.

## Три scope

- `active_dialog_scope` — контекст, с которым пользователь продолжает диалог;
- `last_attempted_scope` — точная интерпретация последнего выполненного turn;
- `last_successful_scope` — последний scope с успешным результатом.

`no_data`, ошибка и уточнение не подменяют последний успешный результат, но
остаются видимыми в active/attempted scope.

## Мутации

Каждое поле изменяется одной из операций:

- `keep` — оставить значение;
- `set` — заменить;
- `add` / `remove` — изменить список;
- `clear` — явно очистить;
- `reference` — скопировать поле из одного из трёх scope.

Reference принимает только путь вида
`last_successful_scope.intent.periods`. Произвольные JSONPath и вычисления не
допускаются.

## Reuse

На первом этапе не копируются metadata и production execution:

- strict-ready registry загружается из явно заданного manifest;
- unified pipeline вызывается с `backend_override=unified_strict`;
- V2 intent проецируется в старый `ContextExecutionOverride` только без потери
  смысла;
- отсутствует автоматический поиск соседнего `pipeline` и fallback.

Когда новая версия получит собственное исполнение multi-operand/grouping,
compatibility adapter останется rollback/сравнительной границей.
