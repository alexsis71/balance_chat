# Context Chat acceptance runner

`balance_chat.acceptance_runner` исполняет человекочитаемый каталог
[`../acceptance/context_chat_business_scenarios.feature`](../acceptance/context_chat_business_scenarios.feature)
через публичный V2 API. Каждый сценарий получает отдельную сессию, а T1–T7
выполняются последовательно с authoritative `revision` и уникальными
`request_id`.

Runner проверяет не только HTTP status, но и доступные typed признаки:

- operation, metric, aggregate и grain;
- canonical exclusive-end периоды;
- количество operands и роли/имена сущностей;
- formula operator, даты экстремумов и `no_data`/clarification;
- отсутствие `balance_id`/`article_id` и единицы `млн м3` в публичном result;
- idempotent replay и продолжение typed clarification.
- межповоротные entity/period handles, semantic fingerprint и сохранение
  `last_successful` после `error`/`no_data`;
- отсутствие повторного LLM/DB execution по structured audit-событиям.

Формулировка `Тогда`, для которой нет детерминированного evaluator,
попадает в `Automation gaps`. Сценарий получает статус `INCOMPLETE`, а не
ложный `PASS`. Флаг `--strict-coverage` делает любой такой пробел ошибкой
процесса. Сценарии `@contract-gap` по умолчанию не запускаются.

На checkpoint 2026-08-05 все ранее найденные 67 gaps закрыты: для 12 сценариев
`@current-contract` автоматизированы 104 из 104 строк ожиданий (100%), dry-run
создаёт 121 детерминированную проверку и не содержит `Automation gaps`.
Покрытие evaluator-ами не означает, что все live business-сценарии уже
проходят: semantic acceptance считается отдельно и показывает реальные
дефекты interpretation/binding/execution.

## Инвентаризация без backend

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python .\scripts\run_acceptance.py --dry-run --tag P0
```

## DB-backed smoke

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python .\scripts\run_acceptance.py `
  --base-url http://127.0.0.1:8790 `
  --execute-db `
  --scenario BC-01 `
  --max-turns 1
```

Полный текущий контракт:

```powershell
python .\scripts\run_acceptance.py `
  --base-url http://127.0.0.1:8790 `
  --execute-db `
  --strict-coverage `
  --log-file .\logs\balance_chat.jsonl
```

`--log-file` указывает на уже существующий JSONL-журнал запущенного backend,
а не задаёт имя отчёта runner. Если backend использует стандартный
`config.example.json`, параметр можно не указывать: default уже равен
`.\logs\balance_chat.jsonl`. Несуществующий путь отклоняется до долгого
DB-backed прогона. Каталог отчётов задаётся отдельно через `--report-dir`.

Если API защищён, runner читает ключ только из переменной
`AI_BALANCES_API_KEY`. Другое имя задаётся через `--api-key-env`; значение
ключа не попадает в отчёт.

Отчёты создаются в `reports/acceptance/` в JSON и Markdown. В них сохраняются
session/request IDs, compact semantic snapshots, результаты проверок и
неавтоматизированные ожидания; prompts, SQL, raw DB rows, DSN и API key не
сохраняются.

Операционный сценарий рестарта выполняется только при явном
`--restart-command`; в dry-run он учитывается как поддерживаемая capability.
Конкурентная revision, idempotent replay, fingerprints, handles и отсутствие
повторного execution проверяются по API и коррелированным событиям
`turn_completed`/`conversation_turn_committed` из `--log-file`.

## Последний подтверждённый checkpoint

- strict dry-run: 12 сценариев, 104/104 expectation lines, 121/121 checks,
  coverage 100%, gaps 0;
- локальный regression: 188 passed;
- focused DB-backed `BC-05`: 2/2 turn, 13/13 checks, включая canonical aliases,
  официальные display names, повторную группировку без DB retry и `тыс. м3`;
- focused DB-backed `BC-04`: все шесть HTTP turn завершились без server error,
  reverse без связи вернул строгий `no_data`; три semantic assertions всё ещё
  выявляют отдельные product defects выбора homonym/source.
