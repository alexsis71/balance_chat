# Context Chat V2: staging acceptance

Дата проверки: 2026-07-31. Проверка выполнена на `balance_chat` с PostgreSQL
`chat_rag`, ready metadata bundle `2026.07.6`, реальным staging PostgreSQL и
Qwen через текущий unified strict runtime. Действующий backend `pipeline` не
переключался.

## Топология и readiness

- PostgreSQL migration `002_context_contract_v2.sql` применена. Таблицы
  `context_sessions_v2` и append-only `context_mutations_v2` доступны роли
  приложения.
- Прямой маршрут application node → DGX:8000 недоступен в текущей сети;
  acceptance использует явный SSH tunnel `127.0.0.1:18000 → DGX:8000`.
- `/api/v2/health` вернул HTTP 200: context store, ready metadata и Qwen
  structured outputs доступны. Из health исключены секреты, prompts, SQL и
  raw results.
- API key и DSN передаются только через окружение/существующую конфигурацию и
  не записаны в репозиторий.

## DB-backed сценарии

| Сценарий | Результат |
|---|---|
| Казань, май 2025 → «сравни с июнем 2025» | `compare_periods`, две canonical exclusive-end пары, два факта, comparison сформирован |
| Казань, май 2025 → «а теперь в Ярославскую область?» | период `2025-05-01—2025-06-01` унаследован; GEO заменён на каноническую Ярославскую область; один итоговый факт |
| Волгоградская и Воронежская области, апрель 2025 | текущий multi-region resolved plan разложен на четыре V2 operands и выполнен |
| Казань и Ярославль, май 2025 | legacy resolver деградировал compare до одного Казанского operand; V2 остановил запрос кодом `resolved_comparison_degraded` |

Для Ярославской области unified runtime вернул две аддитивные строки статьи
«Ярославская обл.» из суточных балансов Нижнего Новгорода и Ухты. V2 суммирует
их только потому, что scalar operand имеет `aggregate_type=sum`, единица общая;
обе исходные строки сохранены в provenance. Для `min`, `max` применяется
соответствующая редукция, а неоднозначная неаддитивная форма отклоняется.

## Persistence, recovery и concurrency

- Snapshot revision 1, metadata version и active scope успешно восстановлены
  новым экземпляром `PostgresContextStore`, имитирующим restart процесса.
- Commit с устаревшей revision 0 отклонён как конфликт до записи мутации.
- Persistence-only c=2: wall 282.5 ms, максимальный commit 19.3 ms.
- Persistence-only c=4: wall 74.5 ms, максимальный commit 24.4 ms.
- После acceptance временные V2 sessions и связанные result-memory artifacts
  удалены; таблица sessions осталась без тестовых записей.

Нагрузочные числа c=2/c=4 относятся только к authoritative store. Это не
end-to-end benchmark Qwen/unified/PostgreSQL и не основание для capacity plan.

## Focused verification

- Runtime/execution/API/PostgreSQL focused set: 16 passed.
- Health/API/runtime focused set после observability hardening: 12 passed.
- Compile check и `git diff --check`: passed.
- Полный regression намеренно не запускался по плану разработки.

## Риски и условия cutover

1. Случай сравнения двух городов всё ещё может терять второй operand внутри
   текущего legacy resolver. V2 выявляет потерю, но собственный resolver этой
   формы ещё не реализован.
2. Сериализация turn одной сессии выполняется in-process lock плюс
   PostgreSQL optimistic revision. Для нескольких application workers два
   дорогих вычисления могут стартовать параллельно; второй commit будет
   отклонён. Перед production cutover нужен distributed turn reservation или
   раннее атомарное резервирование revision.
3. Таймауты vLLM/unified остаются настройками текущего `pipeline`. V2 не делает
   скрытый retry/fallback; processing error отображается как HTTP 422, store
   outage как HTTP 503, revision conflict как HTTP 409. Нужна отдельная
   fault-injection проверка реальных timeout сценариев.
4. В acceptance первый standalone resolver занял около 20.9 s; контекстный GEO
   turn — около 4.6 s до завершения resolver/gate и DB. Нужны end-to-end c=2/c=4
   и percentile latency перед production traffic.
5. Значение единицы берётся из существующего ResultEnvelope. Проверка
   физического масштаба `млн м3`/`тыс. м3` остаётся ответственностью parity
   regression и не исправляется эвристикой в V2 translator.

Production cutover пока не выполнен. Следующий checkpoint: полный parity и
regression, fault injection, end-to-end load, затем явное переключение entry
point на `run_server.py`; старый backend остаётся отдельным rollback-процессом,
без автоматического fallback.
