# Context Chat V2: staging acceptance

Дата проверки: 2026-07-31; повтор после metadata rebuild выполнен в тот же день.
Проверка выполнена на `balance_chat` с PostgreSQL `chat_rag`, ready metadata
bundle `2026.07.7` (`sha256:14eb8ca1cf8ac8ee5e74257a14a7fa58923864fcfec3a2fd0f6aafb32a86308b`), реальным staging PostgreSQL и
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
| Казань и Ярославль, май 2025 | `deterministic_peer_entity`, два canonical article/balance operand, два DB-backed факта, comparison сформирован |

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

1. Сериализация turn одной сессии выполняется in-process lock плюс
   PostgreSQL optimistic revision. Для нескольких application workers два
   дорогих вычисления могут стартовать параллельно; второй commit будет
   отклонён. Перед production cutover нужен distributed turn reservation или
   раннее атомарное резервирование revision.
2. Таймауты vLLM/unified остаются настройками текущего `pipeline`. V2 не делает
   скрытый retry/fallback; processing error отображается как HTTP 422, store
   outage как HTTP 503, revision conflict как HTTP 409. Нужна отдельная
   fault-injection проверка реальных timeout сценариев.
3. В acceptance первый standalone resolver занял около 20.9 s; контекстный GEO
   turn — около 4.6 s до завершения resolver/gate и DB. Нужны end-to-end c=2/c=4
   и percentile latency перед production traffic.
4. Значение единицы берётся из существующего ResultEnvelope. Проверка
   физического масштаба `млн м3`/`тыс. м3` остаётся ответственностью parity
   regression и не исправляется эвристикой в V2 translator.
5. Невалидный structured interpretation сейчас может пройти мимо
   `TurnProcessingError`: `mode=standalone` без mutation draft дал HTTP 500.
   Перед cutover требуется единое контролируемое error mapping без retry.
6. Deterministic peer-city detector слишком широк для смешанного сравнения
   metric/entity. Запрос о поставках и собственных потребителях Казани был
   ошибочно сведён к двум distribution operands. Такой результат нельзя считать
   semantic success; детектор должен принимать только однородное entity/entity
   сравнение, а смешанную форму передавать typed interpretation/planner.
7. Canonical GEO `Ярославская` корректно исполняется, но active scope показывает
   сокращённое имя вместо официального `Ярославская область`. Это отдельная
   metadata/presentation-задача и не должно исправляться в summary.

Production cutover пока не выполнен. Следующий checkpoint: полный parity и
regression, fault injection, end-to-end load, затем явное переключение entry
point на `run_server.py`; старый backend остаётся отдельным rollback-процессом,
без автоматического fallback.

## Peer entity comparison hardening

Дополнение от 2026-07-31:

- Причина потери второго города найдена: первый standalone comparison обходил
  V2 intent и legacy resolver деградировал `compare` до одного expression.
- Для явной формы `в A и B` добавлен deterministic peer-entity path. Точные
  статьи распределения имеют приоритет над GEO; поэтому Казань и Ярославль
  представлены статьями вместе с их canonical balances. Области продолжают
  разрешаться как GEO.
- Общие metric, aggregate и canonical period извлекаются отдельным semantic
  pass без DB. Деградировавшие legacy expressions не используются.
- Каждый operand получает собственный scalar query, поэтому analyzer второго
  task не наследует текст первого operand.
- В `pipeline` canonical article override теперь очищает только analyzer-only
  GEO, если явный destination GEO не передан.
- Route `из A в B` peer-правилом не перехватывается; неоднозначные статьи не
  выбираются автоматически.

Focused `balance_chat` набор после rebuild: 25 passed. Strict registry и health
успешно приняли bundle `2026.07.7`; metadata checksum blocker закрыт.

## Повторный DB-backed acceptance на 2026.07.7

| Проверка | Фактический результат |
|---|---|
| Казань и Ярославль за май 2025 | success; два operands, два facts, `task_count=2`, 33.1 s |
| Казань за май → «сравни с июнем 2025» | success; май сохранён, июнь добавлен как вторая exclusive-end пара, два facts, 4.6 s на втором turn |
| Казань за май → «а теперь в Ярославскую область?» | success; период сохранён, destination заменён, один fact, 3.6 s на втором turn |
| Групповой запрос по областям за апрель 2025 | первый turn success; legacy projection содержит 100 operands и возвращает rows |
| Затем «суммируй данные по областям» | failed; Qwen вернул невалидный standalone contract, API ответил HTTP 500 |
| Поставки против собственных потребителей Казани | failed semantic acceptance; peer detector потерял второй metric и сравнил article Казань с GEO Казань как distribution |
| Полные имена ГП ТГ Нижнего Новгорода и Санкт-Петербурга | оба новых curated alias разрешены в точные balance IDs `2010000040110` и `2010000042550` |

Каждая acceptance-сессия удалена через V2 API после проверки; вместе с ней
удалялись связанные result-memory artifacts. Старый backend продолжал работать
на порту 8787, V2 был поднят отдельно на 8790. Полный regression и production
cutover не выполнялись.
