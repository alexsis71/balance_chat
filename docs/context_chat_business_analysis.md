# Context Chat: инвентаризация запросов и архитектурный gap-анализ

Дата анализа: 2026-08-05.

Связанный acceptance-каталог: [`../acceptance/context_chat_business_scenarios.feature`](../acceptance/context_chat_business_scenarios.feature).

## 1. Результат в одном абзаце

У системы уже есть правильная основа для контекстного чата: canonical metadata
bundle, строгая привязка сущностей, exclusive-end периоды, authoritative
семиповоротный ledger, optimistic revision, idempotency и исполнение без скрытого
fallback. Основной риск теперь находится выше PostgreSQL: текущий
`AnalysisIntent` описывает один простой запрос или одно бинарное сравнение, но
реальный диалог быстро превращается в граф вычислений — максимум месячных сумм,
доля одного показателя от другого, два объекта на два периода, ссылка на
победителя прошлого расчёта. Эти формы пока обслуживаются сочетанием legacy
resolver и специальных веток в `processor.py`. Дальнейшее добавление фразовых
правил будет увеличивать неоднозначность. Нужен единый semantic calculation
graph, в котором entity, metric, period, aggregation, grouping, ranking и
formula являются независимыми типизированными узлами.

## 2. Проверенные источники

### 2.1. V2 runtime

Источник: `logs/balance_chat.jsonl`, размер на момент анализа около 554 КБ.

Наблюдаемая выборка:

| Показатель | Значение |
|---|---:|
| попытки turn (`turn_started`) | 109 |
| завершённые turn (`turn_completed`) | 85 |
| ошибки (`turn_failed`) | 24 |
| созданные сессии | 54 |
| committed frames | 52 |
| invalid interpretation contract | 4 |
| context graph binding failure | 5 |

В журнале найдено шесть сессий с двумя и более запросами. Самые информативные
цепочки:

- 8 turn: Самарская область → лето → сравнение сезонов → max/min → Казань →
  сравнение GEO;
- 8 попыток: Новгород ↔ Москва → min/max → reverse → баланс → route sum;
- 5 turn: Томск/Сургут → сравнение viewpoints → смена баланса → максимум
  поступления;
- 3 turn: баланс Сургута → максимум поступления от Томска → добавление года.

Записи с `request_id`, начинающимся на `accept`, `verify`, `regression` или
`codex`, считаются acceptance/разработческими, а не реальным production
трафиком. UUID сам по себе также не доказывает, что запрос создан конечным
пользователем, поэтому в каталоге используется нейтральный provenance
`observed-v2`.

### 2.2. V1 query traces

Источник: `../pipeline/logs/query_traces`.

| Показатель | Значение |
|---|---:|
| файлов трасс | 2208 |
| уникальных формулировок | 226 |
| status `ok` | 374 |
| status `partial` | 1477 |
| status `no_data` | 188 |
| status `error` | 169 |
| operation `show` | 1505 |
| operation `compare_periods` | 341 |
| operation `aggregate` | 306 |
| operation `compare` | 45 |

Большая доля `partial` и сильные повторы показывают, что это смесь ручных
запросов, regression и многократных прогонов одних формулировок. Статистику
нельзя трактовать как production conversion rate. Она полезна как карта
семантических нагрузок и повторяющихся отказов.

Частые или диагностически важные формулировки:

- «Покажи распределение поставок газа от ГП ТГ Ухта в Ярославскую область по
  месяцам 2025»;
- «Какой процент от минимального запаса газа в ГП ТГ Сургут составляет
  максимальное распределение из ТГ Сургут в ГП ТГ Томск в 2025»;
- «Когда был достигнут максимум транспорта газа между ГП ТГ Новгород и ГП ТГ
  Москва…»;
- «Покажи поставки газа по областям… суммируй данные по областям»;
- «Сравни поставки в Ярославль и Казань за лето 2025»;
- «В каком месяце был максимум поступления в ТГ Томск от ТГ Сургут в 2025».

### 2.3. Датасеты и feedback V1

| Источник | Строк | Назначение |
|---|---:|---|
| `data/dataset_sql.jsonl` | 1122 | исходный синтетический SQL IR corpus |
| `data/dataset_sql_aug.jsonl` | 1188 | расширенный corpus с feedback |
| `data/dataset_sql_aug_v2.jsonl` | 1184 | нормализованная V2-вариация corpus |
| `data/dataset_sql_edge_cases.jsonl` | 50 | known/no_data/unsupported edge cases |
| `data/resolved_query_plans.jsonl` | 285 | результаты реальных прогонов resolver |
| `data/user_feedback.jsonl` | 32 | 26 good, 6 bad |

Датасеты в основном standalone и синтетические. Они хорошо покрывают
лексические варианты, но не проверяют наследование состояния на 5–7 turn.
`user_feedback.jsonl` — самый близкий к оценённому пользователем источнику,
однако и он содержит сравнение веток и повторные записи одного запроса.

### 2.4. Metadata bundle

| Каталог | Записей |
|---|---:|
| balances | 54 |
| articles | 4591 |
| geo objects | 210 |
| geo groups | 2 |
| routes | 314 |
| relations | 4256 |
| aliases | 1616 |
| legacy source `article_semantics.jsonl` | 6025 |

Curated GEO groups сейчас: `регионы` и `московский регион`. Вторая группа
содержит Москву и Московскую область; `Подмосковье` является alias Московской
области. Москва в metadata имеет тип региона. Германия имеет тип country.

Есть заметная неоднородность публичных canonical labels: например,
`самарская обл`, `ростовская`, но `ульяновская область` и `краснодарский край`.
Группировка по ID защищает арифметику от дублей, однако для стабильного UI
нужен отдельный официальный `display_name`, не зависящий от исходной строки
legacy article.

### 2.5. История проектных чатов

Agentmemory по релевантным ключам не вернул сохранённых наблюдений. Поэтому в
анализ не включены непроверяемые сведения о других сессиях Codex/ChatGPT.
Использованы только текущая история обсуждения и локальные артефакты репозитория.

## 3. Обобщённый пользовательский опыт

### Что уже работает как продуктовая основа

- Контекст хранится как typed state, а не только как текстовый transcript.
- Последние семь turn имеют stable handles для turn, operand, entity и period.
- Canonical periods не обязаны повторно извлекаться из естественного языка.
- `no_data` и error не должны заменять последний успешный scope.
- Ready metadata registry остаётся источником canonical ID; модель не назначает
  runtime ID.
- Два scalar operand могут исполняться по одному разу и сравниваться после DB.
- Сессия и журнал persist в PostgreSQL, revision защищает от конкурентной
  записи, request_id — от повторного исполнения.
- Result memory отделена от authoritative context: pgvector помогает retrieval,
  но не меняет canonical state.

### Что пользователь воспринимал как непредсказуемость

1. **Потеря контекста.** Короткие продолжения «сравни лето и осень», «в какой
   день был максимум?», «суммируй данные по областям» раньше становились
   standalone и теряли GEO/период.
2. **Разная семантика близких фраз.** «Покажи распределение…» и «какой суммарный
   объём распределения…» могли выбрать разные balances/articles.
3. **Смешение namespace.** `ТГ Нижний Новгород` — business object, а следующий
   `Нижний Новгород` в том же запросе может быть GEO. Простого словаря строк
   недостаточно.
4. **Исчезновение второго operand.** Legacy analyzer исторически умеет один
   основной expression лучше, чем два равноправных объекта или две метрики.
5. **Неверная форма результата.** Запрос «в каком месяце максимум» выполнял
   правильные 12 месячных сумм, но возвращал все строки вместо победившего
   месяца.
6. **Процент без явной формулы.** Composite percentage распознаётся, но V1
   single-step resolver сам фиксирует, что не способен безопасно исполнить его.
7. **Проблемы presentation.** Встречались `operand_1`, внутренние ID,
   технические warnings, неверное `млн м3`, даты с двухзначным годом и summary,
   не называющее конкретную дату экстремума.
8. **Уточнение как тупик.** Ранее UI мог показать clarification, но продолжение
   расчёта не доходило до корректного typed continuation.
9. **Reverse как эвристика.** «И наоборот» требует не перестановки слов, а
   выбора другой canonical relation/article; её может не существовать.
10. **Точечные исправления накапливаются.** Поведение улучшается, но всё больше
    зависит от порядка regex fast paths в одном крупном processor.

## 4. Предметная модель, необходимая для тестов

### 4.1. Классы показателей

| Класс | Метрики | Агрегация по периоду | Типичный extremum |
|---|---|---|---|
| аддитивный поток | distribution, incoming, storage_injection, storage_withdrawal, export, production | `sum` | max/min дневного потока или max/min суммы временной корзины |
| состояние | stock | `first`, `last`, min/max snapshot | дата или корзина состояния |
| изменение состояния | stock_change | правило конкретной статьи; не всегда простая сумма | min/max изменения |
| потребление/нужды | consumption, own_needs/losses | зависит от canonical article semantics, обычно flow sum | дневной extremum или period sum |
| производная величина | percent, ratio, delta, percent_change | вычисляется из scalar facts | rank производных результатов |

Критическое различие: «максимум за месяц» может означать максимальное дневное
значение внутри месяца, а «в каком месяце максимум» — максимальную сумму по
месячным корзинам. Одного поля `aggregate_type=max` недостаточно.

### 4.2. Viewpoint направленного потока

Одна физическая передача может наблюдаться в двух балансах:

- `distribution`: balance источника, article/subject назначения;
- `incoming`: balance назначения, article/subject источника.

Это два самостоятельных измерения. Их можно сравнить после отдельных scalar
расчётов, но нельзя склеивать в один context или считать одной строкой metadata.

### 4.3. Пространства сущностей

Минимально нужны независимые типы:

- `business_balance` — ГП ТГ/ПХГ и другой владелец баланса;
- `balance_article` — строка внутри конкретного balance и section/path;
- `geo_object` — город, субъект РФ, страна;
- `geo_group` — curated набор GEO;
- `route`/`business_relation` — направленная canonical связь;
- `result_entity` — победитель, строка, корзина или derived result прошлого
  turn.

Строка `Москва` может существовать в нескольких пространствах. Квалификатор
`ГП ТГ` резервирует business namespace; предлог и позиция сами по себе не
являются достаточным canonical доказательством.

### 4.4. Время

- Все execution periods — `[date_from, date_to)`.
- Март 2025: `[2025-03-01, 2025-04-01)`.
- Весна 2025: `[2025-03-01, 2025-06-01)`.
- Второй квартал 2025: `[2025-04-01, 2025-07-01)`.
- Публичная правая дата показывается inclusive, но contract остаётся exclusive.
- `тот же период`, `второй месяц`, `предыдущий месяц` должны ссылаться на period
  handles, а не восстанавливаться из summary prose.

## 5. Анализ текущей архитектуры

### 5.1. Сильные стороны

1. `ContextContractV2` разделяет active, attempted и successful scopes.
2. `ResolvedTurnFrame` хранит canonical intent, operands, entity tags, periods и
   bounded result facts.
3. `UnifiedInterpreter` выдаёт strict structured output и не имеет права
   назначать IDs.
4. `InterpretationMutationCompiler` валидирует handles и связывает mentions
   через registry.
5. `NativeMultiOperandPlanner` и `NativeExecutor` отделяют interpretation от DB
   execution и не повторяют LLM/MCP/PostgreSQL.
6. Compatibility layer fail-closed: неподдерживаемая форма не упрощается
   скрыто.
7. Persistence, restart recovery, idempotency и revision уже заданы правильно.

### 5.2. Ограничения контракта

| Ограничение | Следствие для кейсов |
|---|---|
| `compare` требует ровно 2 operand | нет N-way и matrix comparison |
| `compare_periods` требует 1 operand и 2 периода | нельзя типизированно выразить 2 GEO × 2 периода |
| `ComparisonSpec` фиксирует только `target - baseline` и percent base baseline | нет произвольной ratio/percent formula |
| grouping разрешён только при operation `group` | нельзя естественно выразить group → rank → percent |
| grouping сейчас поддерживает только `sum` | нет last stock per group, avg, top-N |
| `grain` и `aggregate_type` перегружены | не различаются bucket aggregation и rank aggregation |
| entity type — строка, role set ограничен | business relation и result-derived entity не первоклассны |
| один active intent | сложный диалог требует focus между несколькими прежними result/operand |

### 5.3. Ограничения interpretation

Текущий системный prompt уже содержит полезное предметное ядро, однако runtime
capabilities в `_capabilities()` перечисляют только:

`show`, `aggregate`, `compare`, `compare_periods`, `group`, `distribution`,
`incoming`, `own_needs`, `stock`, `export`, временные grains и `geo_group`.

В списке нет `storage_injection`, `storage_withdrawal`, `stock_change`,
`consumption`, `production`, `flow_balance`, `ratio`, `percent`, `top_n`.
Следовательно, модель получает более узкий contract, чем фактически умеет
metadata/resolver, и не может единообразно интерпретировать весь домен.

Domain hints берут только первые восемь geo groups и routes. Это не candidate
retrieval по текущим mentions и плохо масштабируется на 314 routes, 210 GEO и
4591 articles.

### 5.4. Ограничения orchestration-кода

`processor.py` — около 86 КБ и более двух тысяч строк. В нём одновременно
находятся:

- выбор standalone/contextual пути;
- regex fast paths для периода, reverse, группировки и отдельных сравнений;
- business/GEO tagging;
- native execution orchestration;
- translation legacy envelope;
- result memory;
- summary/presentation sanitization;
- public ranking workaround.

Такое соседство делает порядок проверок частью бизнес-семантики. Новая фраза
может пройти через другой fast path и получить другой balance/article, хотя её
semantic fingerprint совпадает со старой.

### 5.5. Ограничения legacy boundary

V1 resolver остаётся хорошим scalar executor, но его собственный analyzer:

- останавливает composite percentage как multi-step unsupported;
- лучше представляет один expression, чем несколько равноправных operand;
- кодирует ranking временных корзин как `sum + period_grain`, теряя внешний
  `max/min` при переводе envelope;
- может выбирать общий balance, если typed role/context не дошёл до него.

Поэтому compatibility boundary следует использовать для исполнения уже
полностью связанного scalar task, а не как второй независимый источник intent.

### 5.6. Ограничения metadata

- Canonical ID достаточно для арифметики, но canonical label не всегда является
  официальным публичным названием региона.
- Alias должен иметь допустимые entity types/roles; глобальный alias строки
  недостаточен для омонимов business/GEO/article.
- Reverse route требует явного relation edge и direction-bound article, а не
  только похожего имени balance.
- Metric policy должна хранить temporal nature, default period aggregate,
  allowed aggregates, canonical unit и совместимость в formulas.
- `article_semantics.jsonl` остаётся согласованным источником semantics, но
  runtime bundle должен иметь явную provenance до него и curated overrides.

### 5.7. Наблюдаемость

V2 JSONL уже содержит request/session/revision, normalized query, operation,
entity/period tags и outcome. Для будущего evaluation не хватает единого
события с:

- выбранным scenario/turn correlation label;
- context diff before/after;
- semantic graph hash;
- candidate set и причиной выбора без raw prompt;
- decomposition DAG и числом scalar tasks;
- result fingerprint каждого task;
- formula/group/rank provenance;
- expected-vs-actual assertion codes.

## 6. Целевая архитектурная идея

```text
User turn + last 7 typed frames
              │
              ▼
Deterministic evidence extractor
(dates, qualified business spans, exact metadata candidates)
              │
              ▼
Unified Semantic Interpreter (Qwen, strict schema)
              │ mentions + handle refs + requested calculation
              ▼
Canonical Binder / Relation Resolver
              │
              ▼
Semantic Calculation Graph
  Scalar ─ Group ─ Bucket ─ Rank ─ Formula ─ Compare
              │
              ▼
Deterministic DAG Planner + task dedup/cache
              │
              ▼
Scalar compatibility executor (unified_strict)
              │
              ▼
Typed result graph → one presentation LLM call → public envelope
```

Главная граница: Qwen выбирает смысл и ссылки на bounded handles, registry
назначает canonical entities, deterministic planner строит вычисления, DB
возвращает факты. Ни модель, ни legacy analyzer не должны независимо
переназначать уже canonical entity/period.

## 7. Что изменить

### 7.1. Ввести calculation graph contract

Добавить типизированные узлы:

- `ScalarMeasure`: metric, entities, period, value aggregation, unit;
- `BucketSeries`: scalar source, grain, bucket aggregation;
- `Rank`: source series, max/min/top/bottom, limit, return dimension;
- `Group`: source rows, canonical dimension, aggregation;
- `Formula`: operands и operator (`ratio`, `percent_of`, `delta`,
  `percent_change`, при необходимости `sum`/`difference`);
- `CompareSet`: ordered references, baseline semantics;
- `ResultReferenceNode`: bounded fact/result handle предыдущего turn.

`AnalysisIntent` можно оставить фасадом для простых запросов, но после binding
он должен компилироваться в calculation graph. Это позволяет внедрять изменение
поэтапно, не ломая scalar execution.

### 7.2. Разделить две агрегации extremum

Вместо перегруженного `aggregate_type` хранить:

- `value_aggregation`: что делать с исходными днями (`sum`, `last`, `avg`);
- `bucket_grain`: day/month/quarter/year;
- `rank_operation`: max/min/top/bottom;
- `rank_by`: value;
- `return_dimension`: gas_day/month/quarter/entity.

Пример «в каком месяце максимум поступления»:

`incoming daily → bucket(month, sum) → rank(max, 1) → return month + value`.

### 7.3. Сделать formulas первоклассными

Формула процента обязана фиксировать:

- numerator handle;
- denominator handle;
- operator и scale 100;
- unit compatibility rule;
- zero-denominator policy;
- временное выравнивание;
- provenance scalar facts.

Нельзя восстанавливать numerator/denominator из порядка строк summary.

### 7.4. Добавить focus graph контекста

Семиповоротный ledger сохраняется, но active context следует дополнить:

- ordered focus stack из operand/result handles;
- named sets для «оба», «эти регионы», «лидеры»;
- result-derived handles для winner, bucket и group row;
- explicit antecedent resolution result;
- scope selection reason (`explicit`, `handle`, `unique-active`, `clarified`).

Это позволит сменить GEO без уничтожения старого и затем сравнить несоседние
объекты.

### 7.5. Упростить interpretation pipeline

1. Оставить deterministic extractor как поставщика evidence, но не как набор
   готовых мутаций по отдельным фразам.
2. Для каждого contextual turn вызывать один unified interpretation contract.
3. Передавать все семь typed frames, focus graph, exact candidates и bounded
   domain capabilities.
4. Получать calculation draft с handles/mentions, но без canonical ID.
5. Любую неоднозначность разрешать через typed clarification.
6. Fast path оставить только для полностью доказуемых операций: exact period
   handle, exact clarification option, idempotent replay, revision rejection.

### 7.6. Разделить `processor.py`

Предлагаемые компоненты:

- `TurnRouter` — revision/idempotency/clarification boundary;
- `SemanticEvidenceExtractor` — spans, dates, exact candidates;
- `ContextFocusResolver` — handle/antecedent selection;
- `CalculationGraphCompiler` — strict canonical graph;
- `ExecutionDagPlanner` — scalar task decomposition и dedup;
- `ResultGraphComposer` — group/rank/formula/compare;
- `PublicResultPresenter` — columns, units, dates, warning policy;
- `TurnAuditRecorder` — structured logging.

После выделения компонентов `PipelineV2TurnProcessor` остаётся orchestration
facade, а не владельцем каждой эвристики.

### 7.7. Расширить metadata ontology

Добавить или материализовать в bundle:

- metric definitions с `measure_kind=flow|state|change|derived`;
- default/allowed aggregations и canonical unit;
- role-scoped aliases;
- official GEO display names отдельно от matching name;
- directed business relations с source/destination balance и статьями каждого
  viewpoint;
- reverse relation link или explicit absence;
- formula compatibility matrix;
- provenance до `article_semantics.jsonl` и curated override.

Изменение не должно эвристически исправлять orphan/ambiguous metadata; конфликт
остаётся build-time report и требует curated override.

### 7.8. Зафиксировать presentation contract

- Суточные балансы показывают `тыс. м3`.
- ISO/exclusive dates остаются в diagnostics, UI показывает `DD.MM.YYYY` и
  inclusive period end.
- Таблица использует бизнес-имена столбцов, не `operand_1` и не IDs.
- Extremum всегда содержит конкретную dimension (дата/месяц/GEO).
- Formula показывает numerator, denominator, result и percent base.
- Технические warnings пишутся в log, но не в пользовательский ответ.
- Presentation LLM вызывается один раз над готовым result graph и не меняет
  факты.

## 8. Приоритет изменений по acceptance-каталогу

| Приоритет | Изменение | Какие сценарии разблокирует |
|---|---|---|
| P0 | Calculation graph + formulas | проценты собственных нужд/запаса/закачки, derived comparisons |
| P0 | Bucket aggregation + rank contract | максимум месяца, top/bottom регионов |
| P0 | Relation/viewpoint model | incoming vs distribution, reverse route |
| P0 | Focus graph и result handles | non-adjacent references, «победитель», «оба» |
| P0 | 2×2 и N-way decomposition | GEO × periods, cross-balance matrix |
| P0 | Полный capability ontology для interpreter | storage, stock_change, consumption, formulas |
| P1 | Official GEO presentation fields | стабильные региональные группировки |
| P1 | Processor decomposition | снижение риска новых phrase-specific regressions |
| P1 | Evaluation audit event | автоматический разбор результатов feature-файла |

## 9. Как использовать feature-файл на следующем этапе

1. Реализовать лёгкий runner, читающий сценарии и последовательные строки
   `Когда Tn пользователь спрашивает`.
2. Создавать одну новую session на сценарий, передавать актуальную revision и
   уникальный request_id на каждый turn.
3. Сохранять sanitized response и authoritative context snapshot после turn.
4. Сопоставлять `Тогда/И` с typed assertions, а не с текстом LLM summary.
5. Для `@current-contract` требовать pass; `@contract-gap` сначала фиксировать
   как expected gap, затем снимать тег после реализации.
6. Проверять DB call count: один вызов на scalar task, ноль повторных вызовов
   при result reuse/idempotent replay.
7. Писать отдельный Markdown report: scenario, turn, expected, actual,
   request_id, trace reference, context diff и error code.

Абсолютные числовые значения не зашиты в файл намеренно: данные staging могут
обновляться. Проверяются canonical выбор, арифметическая формула, относительные
отношения, число строк, даты, единицы и provenance. При необходимости стабильных
чисел следует создать отдельную замороженную DB fixture с версией данных.

## 10. Риски следующего этапа

- Нельзя одновременно использовать legacy analyzer и calculation graph как два
  равноправных источника canonical intent: расхождения будут повторяться.
- Нельзя считать summary источником контекста; только typed result/intent graph.
- Нельзя хранить полный raw result в семиповоротном prompt: это увеличит latency,
  токены и риск утечки; нужны bounded facts и handles.
- Нельзя складывать state metric `stock` как flow.
- Нельзя вычислять процент при несовпадающих единицах, периодах или нулевом
  denominator без явной политики.
- Нельзя автоматически выводить reverse relation из похожих имён.
- Нельзя нормализовать official GEO labels только капитализацией legacy строки.
- Нельзя считать текущие 2208 V1 traces независимыми пользовательскими
  наблюдениями: большинство — повторные regression-прогоны.

## 11. Граница этого этапа

На этом этапе созданы только аналитические артефакты. Production/runtime код,
metadata bundle, PostgreSQL API и tests не менялись. Полный regression не
запускался: он относится к следующему этапу, где feature-каталог станет
исполняемым acceptance-набором.
