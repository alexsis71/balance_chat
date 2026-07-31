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
- compatibility adapter использует существующий `pipeline` и ready metadata
  manifest по явным путям. Неподдерживаемая legacy-форма не упрощается скрыто.

Данные и metadata на этом этапе не дублируются. Источником истины остаётся
`../pipeline/data/metadata/manifest.json`.

## Проверка

```powershell
$env:PYTHONPATH = "src"
C:\Users\alexs\miniforge3\envs\ai_env\python.exe -m pytest -q
```

Архитектура и границы миграции описаны в
[`docs/architecture.md`](docs/architecture.md).
