from __future__ import annotations

import json
import logging

from balance_chat.observability import configure_logging, log_event


def test_json_log_contains_structured_event_and_rotates_under_config_root(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    log_path = configure_logging(
        config_path,
        {"path": "logs/test.jsonl", "max_bytes": 1024, "backup_count": 2},
    )

    log_event(
        logging.getLogger("balance_chat.test"),
        logging.INFO,
        "turn_completed",
        request_id="request-1",
        normalized_message="покажи поставки за май 2025",
        periods=[{"date_from": "2025-05-01", "date_to": "2025-06-01"}],
    )

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["event"] == "turn_completed"
    assert records[-1]["request_id"] == "request-1"
    assert records[-1]["normalized_message"] == "покажи поставки за май 2025"
    assert records[-1]["periods"][0]["date_to"] == "2025-06-01"
    assert "timestamp" in records[-1]
    assert "api_key" not in records[-1]

