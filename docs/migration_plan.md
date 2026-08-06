# План миграции Context Chat V2

Разработка разбита на десять checkpoint’ов. После каждого коммита система
остаётся запускаемой: новая capability включается явно, а действующий
`pipeline` не изменяется.

1. **Versioned contract и reducer — завершён.** Несколько операндов,
   exclusive-end периоды, явные мутации, три scope и optimistic revision.
2. **Authoritative persistence — завершён.** Durable SQLite для локального
   режима; PostgreSQL `chat_rag.context_sessions_v2` и append-only журнал
   `context_mutations_v2` для staging/production.
3. **Unified interpretation — завершён.** Один typed LLM contract возвращает
   нормализованный turn, mutation draft, `clarify` или настоящий `unsupported`.
   Canonical IDs принимает только deterministic binder следующего этапа.
4. **Context memory policy — завершён и усилен seven-turn ledger.** Явные
   правила наследования/очистки GEO, периодов, статей, grain и operation;
   deterministic metadata binding; последние семь исходных запросов и их
   resolved typed frames передаются модели через стабильные handles. Полные
   таблицы не передаются: используются bounded facts и result references.
5. **Native multi-operand planner — завершён.** Сравнения entity/entity,
   metric/metric и period/period с фиксированной baseline/target семантикой.
6. **Native executor — завершён.** Выполнение операндов через существующий
   unified strict runtime без multi-step и без summary на scalar subqueries;
   композиция фактов после всех subresults и один bounded summary-вызов над
   готовым результатом без повторного DB execution.
7. **Canonical grouping — завершено.** GEO groups и другие
   измерения, агрегация по canonical ID/официальному имени и provenance
   исходных строк.
8. **Result memory/RAG — завершён.** Переиспользование
   `chat_rag.result_artifacts/chunks`, bounded retrieval и ссылки на факты;
   embeddings не являются authoritative session state.
9. **API и UI V2 — завершён.** Revision-aware endpoints, визуализация
   операндов, активного scope и clarify confirmation; observability payload
   ограничен безопасным whitelist.
10. **Operational hardening — реализован на metadata bundle 2026.07.7.**
    Выполнены executable-shape invariants, operand-aware inheritance, typed
    clarification, extremum dimensions, native canonical grouping, единый
    ResultReference/RAG path, post-commit outbox, distributed reservation,
    idempotency, metadata compatibility, soft-delete audit, bounded API и
    безопасный health/logging. Выполнены DB-backed multi-turn smoke, restart recovery,
    optimistic revision, c=2/c=4 persistence load и bounded health. V2
    запускается отдельной явной командой и не подменяет действующий `pipeline`.
    Peer comparison двух городов, mixed distribution/own-consumers comparison и
    contextual grouping проверены с реальной БД. Invalid interpretation contract
    отображается как HTTP 422. Найденные degradation cases не имеют скрытого
    fallback.

11. **Seven-turn unified context — завершён.** Для активной сессии Qwen получает
    полное authoritative окно последних семи turn. `ContextIntentGraph`
    ссылается на operand/entity/period handles; deterministic compiler запрещает
    неизвестные и неоднозначные ссылки, сохраняет GEO/период после сравнений и
    поддерживает явную смену либо peer comparison сущностей. Каждый начатый и
    каждый зафиксированный turn пишется в structured JSONL log.

Архитектурные checkpoint 1–11 реализованы, но это означает завершение
механизмов, а не достижение продуктовой acceptance. Первоначальный checkpoint
2026-08-03 дал полный V2 regression `84 passed`.
DB-backed grouping acceptance: 100 физических строк сведены в 63 уникальных
canonical GEO ID; повторные LLM/MCP/PostgreSQL вызовы для группировки сохранённого
результата не выполняются. Полный regression sibling `pipeline`: `441 passed`,
`4 skipped`, `13 failed`. Контрольный прогон тех же кейсов на чистом `pipeline
HEAD` подтвердил 10 уже существующих падений; ещё три связаны с находящимися в
его рабочем дереве пользовательскими изменениями (`context article override`,
`context extremum override`, `reverse direction`). Эти upstream failures не
скрыты fallback и остаются явным блокером production cutover, но не нарушают
запускаемость отдельного V2 backend.

## Этап 12: contextual stabilization — выполняется

Срез от 2026-08-06:

- текущий локальный regression расширен до `226 passed`;
- утверждённый DB-backed Golden P0 gate: `7/7 passed`;
- широкий DB-backed acceptance: `0/12` полностью пройденных сценариев,
  `267/308` отдельных checks, 41 result `ok` из 55 выполненных turn;
- основные блокеры находятся в inheritance/reference исторических operands,
  period comparison, reverse direction, business/GEO role stability,
  grouping scope, clarification/no_data mapping и compound summary semantics.

Этап 12 считается завершённым только после 100% утверждённого P0 Golden,
отсутствия регрессии ранее поддержанного P1, `Success Rate >= 95%` и
semantic/helpfulness acceptance не ниже 80% на согласованном DB-backed
каталоге. До этого отдельный V2 backend остаётся демонстрационным/staging
контуром; production cutover не объявляется.
