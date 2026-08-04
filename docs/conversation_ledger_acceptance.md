# Seven-turn conversation ledger acceptance

## Назначение

Проверка подтверждает, что Context Chat сохраняет смысл диалога в течение семи
последовательных пользовательских запросов, не подменяет контекст общим балансом
и не использует скрытый fallback.

Authoritative state находится в PostgreSQL snapshot
`chat_rag.context_sessions_v2`. В `conversation_window` сохраняются typed turn,
operand, canonical entity/GEO и exclusive-end period tags. Qwen получает это
окно целиком; deterministic compiler валидирует возвращённый intent graph до
обращения к execution layer.

## Acceptance chain

1. Поставки в Самарскую область весной 2025.
2. Те же поставки летом того же года.
3. Сравнение весны и лета.
4. Максимальный суточный объём за лето.
5. Минимальный объём за тот же период.
6. Смена GEO на Казань с сохранением периода.
7. Сравнение Самарской области и Казани за лето.

Проверяются revision `1..7`, canonical GEO каждого operand, exclusive-end
периоды, операция и отсутствие silent context reset. Отдельно backend
перезапускается, session snapshot читается снова, после чего выполняется восьмой
follow-up; окно должно остаться длиной семь с вытеснением самого старого turn.

## Логирование

- `turn_started` содержит request/session ID, revision и исходный текст;
- `interpretation_contract_resolved` содержит размер входного окна и handles,
  использованные моделью;
- `conversation_turn_committed` содержит исходный и нормализованный текст,
  canonical entity/period tags, result handle и размер окна;
- `turn_completed` содержит итоговую operation, operands, execution/summary
  diagnostics и длительность;
- `turn_failed` фиксирует тип и код ошибки со stack trace.

API keys, DSN, system prompt, SQL и raw PostgreSQL rows в журнал не попадают.

Фактические результаты staging-прогона дополняются после запуска focused и
полного regression, live DB-backed цепочки и restart recovery.
