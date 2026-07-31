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
4. **Context memory policy — завершён.** Явные правила наследования/очистки
   GEO, периодов, статей, grain и operation; deterministic metadata binding;
   result references без передачи полной истории.
5. **Native multi-operand planner — завершён.** Сравнения entity/entity,
   metric/metric и period/period с фиксированной baseline/target семантикой.
6. **Native executor — завершён.** Выполнение операндов через существующий
   unified strict runtime без multi-step/summary LLM; композиция фактов после
   всех subresults.
7. **Canonical grouping — базовое ядро завершено.** GEO groups и другие
   измерения, агрегация по canonical ID/официальному имени и provenance
   исходных строк.
8. **Result memory/RAG — завершён.** Переиспользование
   `chat_rag.result_artifacts/chunks`, bounded retrieval и ссылки на факты;
   embeddings не являются authoritative session state.
9. **API и UI V2 — завершён.** Revision-aware endpoints, визуализация
   операндов, активного scope и clarify confirmation; observability payload
   ограничен безопасным whitelist.
10. **Staging acceptance и найденный semantic hardening — завершены на metadata
    bundle 2026.07.7; production cutover ожидает operational checkpoint.** Выполнены DB-backed multi-turn smoke, restart recovery,
    optimistic revision, c=2/c=4 persistence load и bounded health. V2
    запускается отдельной явной командой и не подменяет действующий `pipeline`.
    Peer comparison двух городов, mixed distribution/own-consumers comparison и
    contextual grouping проверены с реальной БД. Invalid interpretation contract
    отображается как HTTP 422. Найденные degradation cases не имеют скрытого
    fallback. До cutover остаются distributed turn reservation, fault injection,
    end-to-end load и полный regression.

Полный regression и решение о production cutover выполняются отдельным
checkpoint после завершения разработки этапов.
