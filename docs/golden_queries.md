# Golden Queries

Golden Queries — небольшой вручную утверждённый набор сквозных продуктовых
контрактов. Он проверяет не HTTP-статус и не посимвольный текст summary, а всю
семантическую цепочку:

`user query → interpretation → canonical binding → context → planning/SQL → result shape → result data`.

Каталог находится в
[`../golden/golden_queries.toml`](../golden/golden_queries.toml). В наборе пока
утверждён только `GQ-001`; расширять его до отдельного согласования нельзя.

## Чем Golden Queries отличаются от acceptance

Acceptance-каталог `BC-01…BC-18` проверяет широту: длинные диалоги, вариативные
формулировки, operational recovery и будущие `contract-gap`. Golden Queries
фиксируют узкое неприкосновенное ядро на конкретной metadata fixture и
фиксированной тестовой БД. Любое падение утверждённого P0 блокирует merge.

BC-сценарий нельзя целиком объявить Golden Query. Сначала из него выделяется
одна атомарная пользовательская операция, вручную утверждаются canonical IDs,
период, план и контрольные данные, и только затем она получает `GQ-NNN`.

## Сопоставление BC-01…BC-18

| BC | Содержание | Роль в новой модели | Возможный Golden-уровень |
|---|---|---|---|
| BC-01 | GEO, смена периода, extrema, сравнение | Декомпозировать; не golden целиком | P0 для простого show/context, P1/P2 для compare/rank |
| BC-02 | Направленный транспорт и reverse | Аналитический диалог | P1 Analytics |
| BC-03 | Incoming и ranking временной корзины | Аналитический диалог | P1, ranking в P2 |
| BC-04 | Business/GEO homonym | Binding safety | P1 Analytics |
| BC-05 | GEO group и canonical aggregation | Группировка | P1 Analytics |
| BC-06 | Процент own needs/distribution | Составная формула | P2 Advanced |
| BC-07 | Процент flow/stock extrema | Составная формула | P2 Advanced |
| BC-08 | ПХГ: injection/withdrawal/stock | Метрики P1, формулы P2 | P1/P2 |
| BC-09 | Cross-balance physical relation | Составная связь | P1/P2 |
| BC-10 | Stock point-in-time и периоды | Аналитика состояния | P1 Analytics |
| BC-11 | Export, country aliases, periods | Export/GEO | P1 Analytics |
| BC-12 | Full balance и drill-down | Источник атомарных full-balance P0 | P0/P1 после декомпозиции |
| BC-13 | Paraphrase fingerprint | Поперечная проверка формулировок | P0/P1 после выбора атомарных пар |
| BC-14 | no_data/error/clarification context | Core safety | P0 Core |
| BC-15 | Non-adjacent context references | Core conversation contract | P0 Core |
| BC-16 | Матрица GEO × period | Составная аналитика | P2 Advanced |
| BC-17 | Grouping + top-N | Ranking поверх группировки | P2 Advanced |
| BC-18 | Restart/revision | Operational acceptance, не продуктовый golden | Остаётся acceptance |

Текущие теги `@P0` в acceptance-каталоге не являются автоматическим решением
о включении в Golden P0.

## Предлагаемые P0 Core-кандидаты

Ниже backlog для отдельного утверждения. Он ещё не является golden-набором.

1. Полный баланс на одну дату (`GQ-001`).
2. Раздел «Ресурсы» на дату.
3. Раздел «Поступление» на дату.
4. Раздел «Распределение» на дату.
5. Одна canonical статья на дату.
6. Сумма flow-статьи за календарный месяц.
7. Среднее flow-статьи за период с правильной зернистостью.
8. Собственные нужды на дату.
9. Производство за месяц.
10. Запас как point-in-time, без суммирования дней.
11. Закачка ПХГ за месяц.
12. Отбор ПХГ за месяц.
13. Поступление от указанного source business object.
14. Распределение в указанный destination business object.
15. Распределение в указанный GEO object.
16. Продолжение «а на следующий день?» с наследованием объекта.
17. Смена статьи с сохранением явно установленного периода.
18. Смена баланса с сохранением явно установленной даты.
19. Strict `no_data`, не разрушающий последний успешный scope.
20. Действительно неоднозначная статья с typed clarification и без DB-запроса.

## Структура контракта

Каждая запись TOML содержит:

- `id`, `approval_status`, `priority`, исходный `query`;
- `result_shape`, `operation`, `metric`, обязательный `value_mode = fact`;
- exclusive-end `period`;
- entity mentions с semantic role, type, canonical ID и display name;
- явные списки canonical balance/article/GEO/relation IDs, включая пустые;
- semantic SQL, разрешённый execution layer, SQL function, обязательные и
  запрещённые параметры;
- минимальную форму результата, разделы, иерархию, порядок и единицу;
- контрольные строки фиксированной БД с tolerance;
- допустимость clarification и разрешённые вопросы;
- запрещённые article-level/scalar/period/balance интерпретации.

Canonical IDs дополнительно сверяются с ready metadata manifest. SQL в audit
не пишется: runner использует безопасное evidence — имя функции и bounded
canonical parameters. DSN, API key, raw SQL и DB rows в журнал не попадают.

## GQ-001

`GQ-001` утверждает запрос:

> Покажи баланс ГП ТГ Москва за 25.06.2025.

Контракт требует `balance_snapshot`, `BAL:2010000039953`, период
`[2025-06-25, 2025-06-26)`, слой `unified_balance_level`, функцию
`api.show_balance_day(balance_id=2010000039953, day=2025-06-25)`, не менее 100
иерархических строк, `fact_value`, единицу `тыс. м3` и контрольные значения трёх canonical
статей.

Контрольные значения получены отдельным выполнением с canonical context
override на тестовой БД, а не скопированы из ошибочного публичного ответа.

### Дефект, обнаруживаемый на текущем runtime

Текущая standalone-цепочка:

- интерпретирует точную дату как `[2025-01-01, 2026-01-01)`;
- выбирает `unified_strict`, а не dedicated full-balance execution;
- вызывает `api.show_balance_day(..., day=2025-01-01)`;
- возвращает данные 1 января вместо 25 июня;
- возвращает только `fact_value`, но без публичной единицы в строке.

Поэтому live `GQ-001` обязан завершиться `FAIL` по слоям `context`, `planning`,
`SQL`, `result_shape` и `result_data`. Менять эталон под это поведение нельзя.

### Состояние после исправления deterministic full-balance routing

Общий разбор форматов `DD.MM.YYYY`, `DD/MM/YYYY`, `YYYY-MM-DD` и русской
календарной даты теперь формирует точный exclusive-end день до LLM. Explicit
full-balance запрос строит canonical intent и выполняется один раз через
`unified_balance_level`; строки получают публичную единицу `тыс. м3`.

Повторный live-run устранил расхождения `context`, `planning`, `SQL` и
`result_data`. Оставшийся `result_shape` выявил ошибку самого первоначального
эталона: предметная область не содержит плановых данных. Это оформлено отдельным
решением, а не подгонкой под runtime: schema каталога `1.1` допускает только
`value_mode=fact`, запрещает `plan`/`plan_value`, а единственной единицей
исходного объёма фиксирует `тыс. м3`. Публичный и сохранённый result удаляет
случайно пришедшие plan-поля и канонизирует единицу; проценты остаются
производными значениями с `%`.

## Запуск и merge gate

Проверка структуры и canonical metadata без LLM/DB:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python .\scripts\run_golden.py --validate-only
```

DB-backed запуск утверждённого P0:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python .\scripts\run_golden.py `
  --base-url http://127.0.0.1:8790 `
  --priority "P0 Core" `
  --log-file .\logs\balance_chat.jsonl
```

Отчёты сохраняются в `reports/golden/`. Процесс возвращает ненулевой exit code,
если упал хотя бы один выбранный утверждённый контракт. Перед merge требуются
100% P0 и отсутствие регрессии ранее утверждённых P1.

## Правило изменения

Исправление начинается с воспроизводящего Golden Query или regression-теста.
Изменение эталона оформляется отдельно от реализации и требует явного
обоснования. Query-specific ветки и regex под одну формулировку запрещены.
Новая capability не принимается, пока P0 не равен 100%.
