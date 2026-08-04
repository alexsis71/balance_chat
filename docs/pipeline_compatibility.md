# Совместимость с pipeline

Проверено относительно `pipeline` commit
`e2f751cf1b19cf9f5c6913c31a298bc64a83ac28` (`reviewed aliases bundle`).

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

## Граница resolved plan → AnalysisIntent

Проверены все значения raw intent, которые формирует текущий analyzer:
`show`, `compare`, `rank`, `multi_query`, а также resolved operations `show`,
`aggregate`, `compare`, `compare_periods`.

- `rank` сохраняется как typed `aggregate`, когда pipeline возвращает extremum;
  месячное bucket-ranking остаётся `show` с `grain=month`, как его фактически
  исполняет pipeline.
- `multi_query` сохраняется как явный `multi_step`, но compatibility runtime не
  декомпозирует и не исполняет его скрыто. Пользователь получает исходный
  upstream status и warnings.
- Сравнение более двух canonical operands сохраняет все operands и помечается
  как `multi_step`, потому что `AnalysisIntent.compare` намеренно бинарный.
- `group_by=geo|balance|article` переносится в typed `GroupingSpec`; неизвестная
  dimension или grouped comparison отклоняются явно.
- Неуспешный plan можно зафиксировать без периода. Успешный/partial plan без
  canonical period не попадает в active scope и возвращает `period_required`.

`from_node`/`to_node` текущего resolved plan содержат только labels без
canonical IDs. Они не материализуются как новые сущности эвристически; фактическая
directional семантика сохраняется через canonical balance/article/route, а
расширение контракта требует отдельного согласования metadata IDs.
