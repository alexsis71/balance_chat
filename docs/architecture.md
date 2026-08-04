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

## Persistence

Локальный режим использует SQLite WAL. Staging/production использует таблицу
`chat_rag.context_sessions_v2` как authoritative snapshot и
`chat_rag.context_mutations_v2` как append-only audit log. Snapshot и запись
мутации фиксируются одной PostgreSQL-транзакцией после `SELECT ... FOR UPDATE`.
Конкурентный revision и уже выполняющийся turn отклоняются до вызова resolver,
LLM или execution. Reservation хранится в PostgreSQL с TTL, а `request_id`
обеспечивает идемпотентный replay завершённого ответа.

Существующие `chat_rag.result_artifacts` и `result_chunks` остаются отдельной
памятью результатов. Векторный поиск может помочь interpretation, но не меняет
контракт сессии и не является источником canonical state.

## Unified interpretation

`UnifiedInterpreter` объединяет исправление формулировки и определение
контекстной мутации в одном structured-output вызове. Hybrid policy вызывает
модель только для контекстных ссылок, clarify answer и вероятной опечатки;
самодостаточный запрос может идти прямо в deterministic resolver.

Ответ модели содержит текстовые entity mentions, directives и canonical
exclusive-end периоды, но никогда runtime IDs. `metadata_bundle_version`
проверяется безусловно. Следующий deterministic binder связывает mentions с
ready MetadataRegistry и только после этого создаёт исполнимый
`ContextMutation`.

## Deterministic binding

`InterpretationMutationCompiler` разрешает текстовые mentions через read-only
`MetadataRegistry`. Не найденная или неоднозначная сущность останавливает
binding явно. Для source/destination сначала проверяется GEO, затем curated
GEO group и только затем единственная article candidate.

Смена GEO сохраняет canonical период через scope reference. Обратное направление
переставляет source/destination и удаляет direction-bound article до нового
resolution. Несколько явно выбранных сущностей сохраняются отдельными
операндами, поэтому их можно сравнить на следующем этапе.

## Native planning

Bound intent детерминированно раскладывается на scalar tasks. Entity/metric
comparison создаёт по задаче на операнд, period comparison — по задаче на
canonical exclusive-end период. Baseline и target сохраняются явно; planner не
вызывает LLM и не выполняет запросы.

`NativeExecutor` исполняет каждую scalar task один раз. Compatibility runner
передаёт `allow_multi_step=false` и `apply_summary=false`. Сравнение выполняется
только над явными deterministic facts; отсутствие факта, разные единицы и
ошибка subtask возвращаются явно, без подстановки другого результата.

После завершения всех DB/scalar tasks presentation model вызывается ровно один
раз над уже готовым bounded envelope. Summary не повторяет Planner, MCP или
PostgreSQL execution и не может менять факты. Для standalone DB-запроса summary
включается на финальном unified envelope; semantic/dry-run вызовы остаются без
него. Источник summary и deterministic execution layer фиксируются в JSON-логе,
а техническое сообщение про execution layer не входит в публичный ответ.

## Canonical grouping

Строки группируются только по canonical entity ID. Варианты label не создают
отдельные группы; display name берётся из canonical metadata и форматируется
для интерфейса. Суммирование разных единиц запрещено, а provenance всех
объединённых исходных фактов сохраняется.

## Result memory

`PipelineResultMemoryAdapter` переводит только успешные deterministic facts V2
в контракт существующего `PgVectorSessionRagStore`. `no_data`, error и
clarification не сохраняются как фактическая память. Для каждого artifact
фиксируется hash bound intent, фактов и metadata bundle.

Retrieval сначала ограничивается точным `session_id`, metadata bundle version,
единственной текущей метрикой и canonical exclusive-end периодами, после чего
применяются лимиты chunks, символов на chunk и общего payload. Содержимое
доступно только внутреннему interpretation input; health/API получают лишь
число chunks и result references. Ошибка pgvector явная и не включает
альтернативный скрытый backend.

## API и UI V2

FastAPI boundary предоставляет отдельные `/api/v2/chat` endpoints и не меняет
существующий API `pipeline`. Stale revision возвращает HTTP 409 до
interpretation/execution. Внутри процесса запросы одной сессии сериализованы;
authoritative store повторно проверяет revision при commit.

UI показывает активную operation, canonical periods и каждый operand отдельно.
Clarification options отправляются как typed continuation с текущим revision.
При конфликте UI перечитывает состояние и просит повторить запрос, не выполняя
автоматический retry.

API observability не принимает произвольный debug processor: наружу проходят
только whitelisted counters и result references. RAG content, embeddings, SQL,
raw rows и внутренние envelopes остаются внутри orchestration boundary.

Result-memory write создаётся как outbox-запись в той же транзакции, что snapshot
и mutation journal. Pgvector вызывается только после commit; недоставленная
запись повторяется при следующем старте приложения.

Удаление пользовательской сессии является soft-delete. Роль приложения не
имеет DELETE/UPDATE прав на mutation journal, поэтому audit сохраняется.
