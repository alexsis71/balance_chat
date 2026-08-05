# Context Chat: отчёт тестирования этапов 1 и 2

Дата: 2026-08-05

## Область проверки

- Этап 1: бизнес-сценарии из `acceptance/context_chat_business_scenarios.feature`.
- Этап 2: calculation/formula, bucket ranking, N-way comparison, mixed metrics,
  расширенные metric policies и canonical binding.
- Runtime: gateway `127.0.0.1:8787/v2`, V2 backend `127.0.0.1:8790`, реальный
  Qwen structured output, metadata bundle `2026.08.1`, PostgreSQL и result memory.
- Semantic pass определяется по typed intent, canonical entities, периодам и
  result graph. HTTP 200 сам по себе не считается успехом.

## Автоматические тесты

- Полный `pytest`: **150 passed**, одна deprecation warning Starlette TestClient.
- `compileall`: pass.
- `git diff --check`: pass; присутствуют только предупреждения Git о будущей
  нормализации LF/CRLF.

## Live DB-backed: этап 1

Сессия: `2986f989-4009-4deb-8290-c0a1c6b868f1`.

Сценарий «Поставки в регион, смена периода, экстремумы и сравнение GEO»:

| Turn | Результат | Проверка |
|---|---|---|
| T1 Самарская область, весна 2025 | PASS | show/distribution, canonical GEO, `[2025-03-01, 2025-06-01)` |
| T2 лето того же года | PASS | GEO сохранён, период заменён на `[2025-06-01, 2025-09-01)` |
| T3 сравни весну и лето | **FAIL** | создано два одинаковых летних operand вместо весны и лета |
| T4 максимальный суточный объём | PASS | rank max/day, дата `2025-08-26` |
| T5 минимальный за тот же период | PASS | rank min/day, дата `2025-06-01` |
| T6 поставки в Казань | PASS | GEO заменён, летний период сохранён |
| T7 Самарская область и Казань | PASS | два canonical GEO operand, одинаковый летний период |

Итого этапа 1 по этому P0-сценарию: **6/7 = 85,7%**.

## Live DB-backed: этап 2

| Кейс | Результат | Наблюдение |
|---|---|---|
| Максимум incoming Сургут → Томск как процент от avg distribution Томска | **FAIL** | Qwen сформировал formula и периоды, но пропустил entity mentions; итог `no_data` |
| Контекстная замена 2025 на 2024 для той же формулы | BLOCKED | предыдущий turn не создал успешный active scope |
| В каком месяце максимум incoming Сургут → Томск | **FAIL** | HTTP 200, но операция деградировала в aggregate max без bucket ranking |
| N-way сравнение Самары, Казани и Ярославля | **FAIL** | HTTP 422 `resolved_comparison_degraded` |
| Поставки в Казань против собственных потребителей ГП ТГ Казань | **FAIL** | HTTP 422 `resolved_comparison_degraded` |
| Процент собственных нужд от распределения ГП ТГ Москва | **FAIL** | HTTP 200 с execution status `error` |

Независимые бизнес-кейсы этапа 2: **0/5 semantic pass**. Контракты и
deterministic execution покрыты unit-тестами, но routing/interpretation не
проводит реальные формулировки в эти контракты стабильно.

Ключевые request IDs:

- `stage12-calc-1-9122677e-1330-4df0-aa3a-759a39da7049`
- `stage12-rank_month-bbbb0519-7181-4321-9425-c144dca7a71e`
- `stage12-nway_geo-ba73a73d-de08-4276-9f4c-277abb6d5bbf`
- `stage12-mixed_metric-3b5ee4d3-c63c-4a6d-8dc9-05f6cd834d98`
- `stage12-own_needs_percent-f9b35611-ccdf-452a-85c6-d3c0f7b33946`

## Операционные проверки

- Health после restart: PASS; все пять checks ready.
- Recovery сохранённой calculation-сессии после restart: PASS, session
  `6ca39309-d190-4bc6-8740-0ea3a198f408`, revision 1.
- Idempotent replay одного request_id: PASS; revision осталась 1, result тот же.
- Concurrent/stale revision protection: PASS; HTTP 409 `revision_conflict` до
  нового исполнения.

## Вывод

Операционная основа и локальные контракты стабильны, но система пока не достигла
целевых **Success Rate 95%** и **Acceptance/Helpfulness Rate 80%** на live
формулировках этапа 2. Главный разрыв находится между evidence extraction,
выбором unified interpretation path и обязательным canonical binding нового
calculation graph. Исправлять его набором phrase-specific fallback нельзя:
нужен единый fail-closed routing rule и проверка полноты entity/period evidence
до планирования и PostgreSQL.

## Повторный live DB-backed прогон после unified evidence gate

Время: 2026-08-05 12:42–12:44 MSK. Backend PID `37780`, gateway
`127.0.0.1:8787/v2`, metadata bundle `2026.08.1`. Проверялся semantic pass, а
не только HTTP status.

| Кейс | Результат | Наблюдение |
|---|---|---|
| Максимум incoming Сургут → Томск как процент от avg distribution Томска | **PASS** | `calculate`, два canonical operand, `percent_of`; 42 681,199 / 47 935,142 = 89,04%, максимум 24.01.2025 |
| Контекстная замена 2025 на 2024 для той же формулы | **PASS semantic / no_data** | оба operand и formula унаследованы, периоды заменены на `[2024-01-01, 2025-01-01)`; DB не вернула фактов, active scope сохранил последний успешный 2025 |
| В каком месяце максимум incoming Сургут → Томск | **PASS** | `rank(max, month, bucket=sum)`, 12 buckets, выбран декабрь 2025: 1 127 244,788 тыс. м3 |
| N-way сравнение Самары, Казани и Ярославля | **FAIL** | extractor нашёл 3 GEO, но Qwen вернул один operand без mentions; compile завершился `interpretation_binding_failed` до evidence repair |
| Поставки в Казань против собственных потребителей ГП ТГ Казань | **FAIL** | старый `role_separated_standalone` перехватил запрос до unified gate; `resolved_comparison_degraded` |
| Процент собственных нужд от распределения ГП ТГ Москва | **FAIL** | span `в ГП ТГ Москва` ошибочно получил роль destination вместо balance; global article lookup неоднозначен, `interpretation_binding_failed` |

Независимые бизнес-кейсы этапа 2 после исправлений: **2/5 semantic pass = 40%**.
Контекстная замена периода разблокирована и семантически корректна, но не входит
в знаменатель пяти независимых кейсов.

Оставшиеся архитектурные исправления:

1. запускать completeness repair до окончательной компиляции либо валидировать
   draft graph shape до binder, чтобы восстановить N-way operands;
2. переместить compound/mixed-metric routing gate перед scalar
   `role_separated_standalone`;
3. различать `в <GEO>` и `в ГП ТГ <business balance>`: квалификатор `ГП ТГ`
   должен иметь роль balance, если запрос вычисляет внутреннюю статью этого
   баланса.

Request IDs:

- `stage2-retest-calc-2ab517d0-1ddf-498f-844d-4cc46cb9b230`;
- `stage2-retest-calc-context-300f6da8-98dd-40bd-a628-274d91d3a43c`;
- `stage2-retest-rank-8742460f-b02a-4c40-9335-826148587dea`;
- `stage2-retest-nway-b1edd19d-454d-44cb-8420-ac4eead6aace`;
- `stage2-retest-mixed-b779c3d6-98a0-4a6c-a4c2-2a68494afda6`;
- `stage2-retest-own-needs-06cb420f-bda0-4d2c-badd-ab0ca4ed56bb`.

## Финальный прогон после pre-binding hardening

Время: 2026-08-05 13:12–13:27 MSK. Проверялись фактические canonical intent,
scalar task results, арифметика result graph и публичное представление, а не
только HTTP status.

Реализовано:

- routing gate теперь предшествует scalar role-separated shortcut; mixed
  comparison не может деградировать в один legacy operand;
- однозначный homogeneous N-way GEO graph достраивается из ordered typed
  evidence до canonical binding;
- две записи одного source для `compare_periods` приводятся к canonical форме
  «один operand + два global exclusive-end периода»;
- одиночный qualified business span связывается как balance scope, а роли
  source/destination используются для нескольких разных business balances;
- `own_needs` связывается с curated статьёй `Собств. нужды и потери` из
  `article_semantics.jsonl`, отдельно от `Собственные потребители`;
- semantic metrics `supply`, `consumption`, `own_needs` сохраняются в V2
  intent, а compatibility projection передаёт unified executor поддерживаемый
  execution metric;
- comparison/ranking presentation строится из deterministic result graph:
  знак `target - baseline`, проценты, единицы и даты не переопределяются LLM.

### Этап 1, P0-сценарий на 7 turn

Сессия: `d3139f4f-dfcc-4534-99ed-c360063d9f4f`.

| Turn | Результат | Проверка |
|---|---|---|
| T1 Самарская область, весна | PASS | distribution, `[2025-03-01, 2025-06-01)` |
| T2 лето того же года | PASS | GEO сохранён, effective period `[2025-06-01, 2025-09-01)` |
| T3 сравни весну и лето | PASS | `compare_periods`, один operand, две canonical пары |
| T4 maximum day | PASS | `rank(max, day)`, 26.08.2025 |
| T5 minimum same period | PASS | `rank(min, day)`, 01.06.2025 |
| T6 Казань | PASS | заменён только GEO, лето сохранено |
| T7 Самарская область и Казань | PASS | два GEO operand, comparison `target - baseline` |

Итого: **7/7 = 100% semantic success** против прежних 6/7.

### Этап 2, независимые бизнес-кейсы

| Кейс | Результат | Фактический результат |
|---|---|---|
| Incoming max Сургут → Томск / avg distribution Томска | PASS | `percent_of`, 89,04% |
| Максимальный месяц incoming Сургут → Томск | PASS | декабрь 2025, 1 127 244,788 тыс. м3 |
| N-way Самара / Казань / Ярославль | PASS | три canonical operand и три DB tasks |
| Поставки Казани / собственные потребители | PASS | `distribution` и `consumption`, статьи разделены |
| Собственные нужды / распределение Москвы | PASS | 27 488,867 / 12 370 532,546 = 0,22% |

Итого: **5/5 = 100% semantic success** против прежних 2/5.

Объединённый проверенный набор: **12/12 = 100% Success Rate**. Typed
acceptance/helpfulness checklist (правильные operation, entity roles, periods,
units, formula/rank/comparison math и отсутствие технических ID в ответе)
выполнен для **12/12 = 100%** проверенных turn/cases. Эти проценты относятся к
зафиксированному P0-набору этапов 1–2, а не являются статистической оценкой
будущего production traffic.

Автоматическая проверка после изменений: **167 passed**, одна сторонняя
Starlette deprecation warning; `compileall` и `git diff --check` — pass.

Ключевые финальные request IDs:

- `fix2-nway-006d63b3-adb0-4011-809a-63e8873623f1`;
- `final-mixed-43ccd213-434c-46e5-82c0-fd7e9538e4c6`;
- `fix2-own-needs-5fae8214-1253-44cf-b561-90c1546f5d4e`;
- `final-stage2-545db80e-4e79-4573-9362-351d04dfd519`;
- `final-stage2-08c8f008-5958-426e-9dfe-4f111b7515c7`;
- `stage1-recheck2-t3-0848a88d-1c24-4e5a-b7c0-3d861a38a31c`;
- `stage1-recheck2-t7-825a202a-3f4b-481c-b1f6-1aa5c1465d4c`.

Оставшиеся `@contract-gap` сценарии feature-каталога (derived-result chaining,
полный storage/stock graph, focus sets и matrix GEO × period) не включались в
знаменатель: бизнес-анализ прямо отмечает их как требующие следующего расширения
calculation/focus graph. Скрытый fallback для них не добавлялся.
