# Поэтапный перенос

1. **Контракт и reducer (текущий checkpoint).** Версионированное состояние,
   deterministic mutation semantics, optimistic concurrency и strict adapter.
2. **PostgreSQL store.** Замена уже работающего durable SQLite store реализацией
   того же boundary в `chat_rag`, плюс журнал мутаций и embeddings результатов.
3. **Unified interpretation.** Один LLM-контракт возвращает нормализованный turn,
   typed mutations и clarify/unsupported; reducer остаётся детерминированным.
4. **Native multi-operand execution.** Планирование сравнений сущность-сущность,
   метрика-метрика и период-период без сведения к scalar legacy override.
5. **Grouping execution.** Каноническое объединение GEO и других измерений с
   provenance исходных строк.
6. **UI V2.** Визуализация активного scope, операндов, источника наследования и
   подтверждение неоднозначных мутаций.
7. **Cutover.** Shadow parity, DB-backed acceptance, нагрузка и явное переключение
   с сохранением strict rollback adapter.

Каждый этап добавляет capability за отдельным feature flag и не изменяет
действующий `pipeline`.
