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
8. **Result memory/RAG.** Переиспользование `chat_rag.result_artifacts/chunks`,
   bounded retrieval и ссылки на факты; embeddings не являются authoritative
   session state.
9. **API и UI V2.** Revision-aware endpoints, визуализация операндов, активного
   scope, источника наследования и clarify confirmation.
10. **Shadow acceptance и cutover.** Parity, DB-backed multi-turn acceptance,
    restart recovery, c=2/c=4 load, observability и явное переключение.

Полный regression запускается только после завершения разработки этапов.
