# Balance Chat

Новая версия контекстного чата AI Balances. Репозиторий отделяет долговечный
контракт диалога от текущего unified pipeline и позволяет переносить исполнение
поэтапно, не меняя его бизнес-семантику.

## Первый checkpoint

- `ContextContractV2` хранит несколько операндов, периоды, группировки,
  сравнение, память сущностей и ссылки на результаты.
- `ContextMutation` задаёт явные операции `keep`, `set`, `add`, `remove`,
  `clear`, `reference`.
- reducer раздельно хранит активный, последний предпринятый и последний
  успешный scope.
- optimistic store отклоняет конкурентное изменение revision; SQLite-реализация
  восстанавливает контракт после перезапуска.
- distributed reservation не допускает два одновременных turn одной сессии;
  повтор одного `request_id` возвращает сохранённый ответ без повторного LLM/DB.
- PostgreSQL store сохраняет authoritative snapshot и append-only журнал
  мутаций в существующей схеме `chat_rag`.
- unified interpretation contract совмещает нормализацию и контекстные
  directives, не позволяя модели назначать canonical IDs.
- authoritative conversation ledger хранит последние семь turn: исходный и
  нормализованный текст, typed operands, canonical entity/GEO tags,
  exclusive-end периоды, краткий ответ и bounded facts результата. Каждый
  следующий turn активной сессии интерпретируется Qwen по этому окну целиком.
- стабильные handles turn/operand/entity/period позволяют модели ссылаться на
  уже разрешённый контекст без повторного natural-language resolution;
  неизвестный или неоднозначный handle останавливает запрос явно.
- deterministic binder разрешает mentions через ready metadata registry и
  компилирует их в валидный `ContextMutation`.
- entity extraction выполняется business-first/GEO-second: spans после
  `ГП ТГ`/`ТГ` резервируются как business balance, а GEO ищется только в
  оставшемся тексте. Поэтому одинаковое имя может одновременно быть source
  business object и destination GEO без случайной подстановки статьи.
- result memory переиспользует существующий pgvector store, сохраняя только
  успешные deterministic facts после authoritative commit; durable outbox
  повторяет незавершённую запись после рестарта.
- FastAPI/UI V2 показывают revision, active scope и отдельные operands;
  clarification продолжает ту же сессию без скрытого retry.
- compatibility adapter использует существующий `pipeline` и ready metadata
  manifest по явным путям. Неподдерживаемая legacy-форма не упрощается скрыто.

Данные и metadata на этом этапе не дублируются. Источником истины остаётся
`../pipeline/data/metadata/manifest.json`.

## Проверка

```powershell
$env:PYTHONPATH = "src"
C:\Users\alexs\miniforge3\envs\ai_env\python.exe -m pytest -q
```

Бизнес-каталог имеет стабильные идентификаторы `BC-01`…`BC-18` и исполняется
через DB-backed acceptance runner. Команды, semantic assertions, режим
инвентаризации и формат отчётов описаны в
[`docs/context_chat_acceptance_runner.md`](docs/context_chat_acceptance_runner.md).

Неприкосновенное продуктовое ядро отдельно защищается вручную утверждёнными
Golden Queries. Первый контракт `GQ-001`, отличие от широкого acceptance,
структура каталога и блокирующий P0 merge gate описаны в
[`docs/golden_queries.md`](docs/golden_queries.md). Acceptance-сценарии не
становятся Golden Queries автоматически.

## Staging-запуск V2

Перед запуском должны быть явно заданы настройки текущего vLLM endpoint. Ключи
и DSN не хранятся в репозитории. Пример конфигурации использует PostgreSQL
`chat_rag`, ready metadata bundle и pgvector result memory из соседнего
`pipeline`:

```powershell
$env:PYTHONPATH = "src"
$env:PIPELINE_VLLM_BASE_URL = "http://127.0.0.1:18000/v1"
$env:PIPELINE_VLLM_API_KEY = "<vllm-api-key>"
C:\Users\alexs\miniforge3\envs\ai_env\python.exe run_server.py `
  --config config.example.json --host 127.0.0.1 --port 8790
```

UI доступен по `http://127.0.0.1:8790/`, bounded health diagnostics — по
`/api/v2/health`. Интерфейс хранит локальную историю V2-сессий, позволяет
восстанавливать authoritative context/revision, продолжать clarification,
копировать результат и выгружать таблицы в CSV. Техническая debug-панель в UI
намеренно отсутствует.

Структурированный операционный журнал пишется в JSONL-файл
`logs/balance_chat.jsonl` с ротацией 10 МБ и пятью архивами (параметры меняются
в секции `logging` конфигурации). Для каждого turn журналируются request/session
ID, revision, hash/длина исходного и нормализованный запрос, canonical operation, периоды и
operands, bounded interpretation/execution diagnostics, длительность и stack
trace ошибки. API key, DSN, prompts, SQL и raw DB rows не журналируются. Каталог
`logs/` исключён из Git.

Событие `turn_started` содержит исходный текст даже для запроса, завершившегося
до commit. После commit событие `conversation_turn_committed` фиксирует handles,
canonical entity/period tags, ссылку на bounded result и фактический размер
семиповоротного окна. Это даёт полный audit каждого turn без debug-панели UI.

Ответ health также не содержит DSN, API key, prompts, SQL или raw rows.

Архитектура и границы миграции описаны в
[`docs/architecture.md`](docs/architecture.md).

SQL persistence выполняется последовательно: базовый контракт
[`002_context_contract_v2.sql`](migrations/002_context_contract_v2.sql),
reservation/idempotency/outbox
[`003_context_runtime_hardening.sql`](migrations/003_context_runtime_hardening.sql)
и soft-delete audit
[`004_soft_delete_audit.sql`](migrations/004_soft_delete_audit.sql).

Опциональная защита API включается через `api.api_key_env`; `/api/v2/health`
остаётся доступным для readiness probe. Health проверяет context store,
metadata, Qwen, pgvector и соединение с расчётным PostgreSQL.

API factory: `balance_chat.api:create_app`. UI обслуживается этим же
приложением по `/`, endpoints используют префикс `/api/v2`.

Чтобы сохранить legacy UI на порту `8787` и опубликовать новую версию на том
же origin под `/v2/`, сначала запустите V2 backend на `8790`, затем gateway:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
python .\run_gateway.py --config .\config.example.json --host 127.0.0.1 --port 8787
```

После этого legacy UI доступен по `http://127.0.0.1:8787/`, Context Chat V2 —
по `http://127.0.0.1:8787/v2/`, а его health endpoint — по
`http://127.0.0.1:8787/v2/api/v2/health`. Gateway только маршрутизирует HTTP и
не добавляет fallback между legacy и V2 execution.

Результаты staging-проверки и открытые ограничения описаны в
[`docs/context_chat_v2_staging_acceptance.md`](docs/context_chat_v2_staging_acceptance.md).
