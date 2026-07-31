# Совместимость с pipeline

Проверено относительно `pipeline` commit
`c2e7b4caeeeb5af1ca7838b09d6a1d8ba3cdd535` (`multi-region compare`).

Изменения добавили `IntentPlan.geos`, `ExecutionPlan.comparison_mode`,
multi-region resolution, SQL и summary. Публичные Context Chat функции
`create_chat_session`, `get_chat_session`, `delete_chat_session`,
`execute_chat_turn` и `ResultEnvelope` не изменились.

В V2 несколько регионов представлены отдельными `AnalysisOperand`, поэтому
данные не сводятся обратно в один scalar GEO slot. Compatibility runtime
по-прежнему используется только для одной scalar task с
`backend_override=unified_strict`, `allow_multi_step=false` и
`apply_summary=false`.

Новые V2 endpoints имеют префикс `/api/v2` и не конфликтуют с текущими
`/api/chat` endpoints.
