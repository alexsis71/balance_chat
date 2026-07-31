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
- PostgreSQL store сохраняет authoritative snapshot и append-only журнал
  мутаций в существующей схеме `chat_rag`.
- unified interpretation contract совмещает нормализацию и контекстные
  directives, не позволяя модели назначать canonical IDs.
- deterministic binder разрешает mentions через ready metadata registry и
  компилирует их в валидный `ContextMutation`.
- result memory переиспользует существующий pgvector store, сохраняя только
  успешные deterministic facts и выдавая interpretation bounded retrieval.
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
`/api/v2/health`. Ответ health не содержит DSN, API key, prompts, SQL или raw
rows.

Архитектура и границы миграции описаны в
[`docs/architecture.md`](docs/architecture.md).

SQL этапа persistence: [`migrations/002_context_contract_v2.sql`](migrations/002_context_contract_v2.sql).

API factory: `balance_chat.api:create_app`. UI обслуживается этим же
приложением по `/`, endpoints используют префикс `/api/v2`.

Результаты staging-проверки и открытые ограничения описаны в
[`docs/context_chat_v2_staging_acceptance.md`](docs/context_chat_v2_staging_acceptance.md).
