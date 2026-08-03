from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        fields = getattr(record, "event_fields", None)
        if isinstance(fields, Mapping):
            payload.update(fields)
        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            payload["exception"] = {
                "type": exc_type.__name__ if exc_type else "Exception",
                "message": str(exc_value),
                "traceback": self.formatException(record.exc_info),
            }
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def configure_logging(config_path: Path, section: Mapping[str, Any] | None) -> Path:
    settings = dict(section or {})
    raw_path = Path(str(settings.get("path") or "logs/balance_chat.jsonl"))
    log_path = raw_path if raw_path.is_absolute() else (config_path.parent / raw_path)
    log_path = log_path.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level_name = str(settings.get("level") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = JsonLogFormatter()
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max(1, int(settings.get("max_bytes") or 10_485_760)),
        backupCount=max(1, int(settings.get("backup_count") or 5)),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    logger = logging.getLogger("balance_chat")
    old_handlers = list(logger.handlers)
    logger.handlers.clear()
    for handler in old_handlers:
        try:
            handler.close()
        except Exception:
            pass
    logger.addHandler(file_handler)
    logger.setLevel(level)
    logger.propagate = False
    log_event(
        logger,
        logging.INFO,
        "logging_configured",
        log_path=str(log_path),
        configured_level=level_name,
    )
    return log_path


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    logger.log(
        level,
        event,
        extra={"event": event, "event_fields": fields},
        exc_info=exc_info,
    )
