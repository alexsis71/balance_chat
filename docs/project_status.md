# Balance Chat: состояние проекта

Дата среза: 2026-08-06.

## Текущая точка

`balance_chat` является работоспособным staging-прототипом с защищённым узким
P0-ядром. Backend, PostgreSQL, metadata bundle, Qwen, session persistence,
seven-turn ledger, pgvector result memory и UI V2 доступны. Система пока не
готова к тестовой эксплуатации как произвольный контекстный чат: широкий
DB-backed acceptance выявляет несогласованность interpretation, binding и
context mutation на последовательностях T1–T7.

## Runtime checkpoint

- V2 backend: `http://127.0.0.1:8790`;
- UI через gateway: `http://127.0.0.1:8787/v2/`;
- metadata bundle: `2026.08.1`, schema `1.1`, state `ready`;
- context store и result memory: PostgreSQL schema `chat_rag`;
- presentation/interpretation model: `ai-balances-language` через текущий
  OpenAI-compatible vLLM endpoint;
- доменный контракт: fact-only, объём только `тыс. м3`, `%` только для
  производного расчёта, плановые данные запрещены.

## Подтверждённое ядро

DB-backed Golden P0 от 2026-08-06 прошёл `7/7` без failed layers. Защищены:

1. полный баланс на точную дату;
2. разделы `Ресурсы`, `Поступление`, `Распределение` на точную дату;
3. поступление от указанного source business object;
4. распределение в указанный destination business object;
5. распределение в указанный destination GEO object.

Локальный отчёт (каталог `reports/` не публикуется в Git):
`reports/golden/golden-20260806T094232Z-075f2832.md`.

Дополнительно live подтверждены направленное сравнение потока
`Томск -> Сургут` по двум месяцам и двухсторонний баланс потоков между
ГП ТГ Самара и ГП ТГ Казань. Это подтверждённые capability, но они ещё не
включены в утверждённый Golden P0-каталог.

## Проверки качества

| Уровень | Результат | Что доказывает |
|---|---:|---|
| pytest | 226/226 | Локальные component contracts на fake/stub границах |
| Golden P0 | 7/7 | Неприкосновенное DB-backed ядро продукта |
| Acceptance scenarios | 0/12 | Ни один широкий T1–T7 сценарий не прошёл целиком |
| Acceptance checks | 267/308 | Большая часть отдельных инвариантов работает |
| Result `ok` | 41/55 | Фактическая успешность выполненных turn около 74.5% |
| Полностью green turn | 29/55 | Все назначенные semantic checks прошли у 52.7% turn |

Coverage `98.67%` в acceptance-отчёте показывает, что runner умеет проверить
почти все ожидания. Это не Acceptance Rate. Helpfulness Rate текущим runner не
измеряется и не может считаться достигнутым.

Полный локальный отчёт:
`reports/acceptance/acceptance-20260806T091901Z-5f22fb93.md`.

## Работоспособные классы запросов

На текущем checkpoint надёжны или подтверждены focused acceptance:

- полный баланс и иерархические разделы на одну дату;
- exact-day incoming/distribution по canonical business и GEO destination;
- простая смена периода при сохранении одного GEO;
- отдельные maximum/minimum запросы по сохранённому периоду;
- сравнение двух GEO operands за один период;
- помесячное поступление между трансгазами и drill-down выбранного месяца;
- первичная группировка поставок по областям и суммирование сохранённых rows;
- базовая месячная динамика stock;
- отдельные направленные и двухсторонние flow balance запросы.

Факт успешного отдельного turn не гарантирует прохождение следующего
контекстного продолжения: именно последовательности являются текущей зоной
hardening.

## Открытые классы дефектов

1. `compare_periods` может деградировать в entity comparison с потерей одного
   периода.
2. Ссылки на предыдущий месяц, полный balance scope и несоседние operands не
   всегда разрешаются через authoritative handles.
3. Business/GEO homonyms и смена source при сохранении destination работают
   непоследовательно между execution paths.
4. Reverse direction для выбранного исторического operand может использовать
   текущий operand или завершиться binding error.
5. Curated GEO groups работают в начальном запросе, но scope-переходы между
   `регионы`, `московский регион`, Москвой и Подмосковьем нестабильны.
6. Month-end stock ranking и последующие derived comparisons не завершены.
7. Export country scope теряется при межпериодном продолжении.
8. `no_data`, `needs_clarification` и HTTP 422 пока не имеют единой продуктовой
   границы.
9. Compound summary не всегда получает typed semantics; для `flow_balance`
   встречные потоки нельзя складывать, а fallback должен журналировать причину.
10. Restart recovery требует повторного acceptance с `--restart-command`, а
    ожидаемая семантика `turn_in_progress` против `revision_conflict` должна
    быть согласована как API contract.

## Критерий следующего checkpoint

Следующий checkpoint — contextual stabilization, а не добавление новой
аналитики. Он требует:

- P0 Golden `100%` после каждого изменения;
- прохождение согласованной P0-части DB-backed acceptance без query-specific
  веток;
- затем всего current-contract каталога с `Success Rate >= 95%`;
- semantic/helpfulness acceptance `>= 80%` по отдельно определённой методике;
- подтверждённые restart recovery, concurrency/error mapping и typed summary;
- расширение утверждённого Golden P0 с 7 до 20–30 основных операций.

До выполнения этих условий система пригодна для управляемой демонстрации и
разработки, но не для объявления production-ready контекстного чата.
