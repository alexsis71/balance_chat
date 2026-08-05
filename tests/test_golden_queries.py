from __future__ import annotations

from pathlib import Path

from balance_chat.golden_queries import (
    GoldenQueryResult,
    GoldenRunReport,
    evaluate_golden_query,
    load_golden_catalog,
    metadata_contract_checks,
    report_to_dict,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "golden" / "golden_queries.toml"


def _intent(date_from: str, date_to: str) -> dict:
    return {
        "operation": "show",
        "operands": [{
            "operand_id": "operand_1",
            "metric": "balance",
            "aggregate_type": "sum",
            "entities": [{
                "role": "balance",
                "entity": {
                    "entity_id": "BAL:2010000039953",
                    "entity_type": "balance",
                    "display_name": "ГП ТГ Москва суточный баланс",
                },
            }],
            "periods": [],
        }],
        "periods": [{"date_from": date_from, "date_to": date_to}],
        "grouping": [],
    }


def _row(name: str, indent: int, fact: float, *, day: str = "2025-06-25") -> dict:
    return {
        "article_name": name,
        "article_scope": name.strip(),
        "article_indent": indent,
        "gas_day": day,
        "plan_value": fact,
        "fact_value": fact,
        "unit": "тыс. м3",
    }


def _passing_rows() -> list[dict]:
    rows = [
        _row("Ресурсы", 0, 323248.059),
        _row("  Поступление", 2, 323248.059),
        _row("    От ТГ Волгоград", 4, 0),
        _row("      Ал. Гай-Острогожск", 6, 0),
        _row("    От ТГ Н.Новгород", 4, 318349.277),
    ]
    rows.extend(_row(f"      Статья {index}", 6, index) for index in range(5, 99))
    rows.append(_row("Распределение", 0, 100))
    return rows


def _response(rows: list[dict], date_from: str, date_to: str) -> dict:
    return {
        "status": "ok",
        "context": {"active": {"intent": _intent(date_from, date_to)}},
        "result": {"status": "ok", "rows": rows},
    }


def _audit(*, layer: str, day: str, with_count: bool = True) -> dict:
    execution = {
        "layer": layer,
        "sql_function": "api.show_balance_day",
        "sql_params": {"balance_id": 2010000039953, "day": day},
    }
    if with_count:
        execution["source_execution_count"] = 1
    return {"turn_completed": {"execution": execution}}


def test_gq_001_catalog_is_explicit_and_canonical_metadata_is_valid() -> None:
    catalog = load_golden_catalog(CATALOG)

    assert [item.query_id for item in catalog.queries] == ["GQ-001"]
    query = catalog.queries[0]
    assert query.approval_status == "approved"
    assert query.priority == "P0 Core"
    assert query.result_shape == "balance_snapshot"
    assert query.value_mode == "both"
    assert all(item.passed for item in metadata_contract_checks(catalog, query))


def test_gq_001_semantic_contract_accepts_complete_balance_snapshot() -> None:
    catalog = load_golden_catalog(CATALOG)
    query = catalog.queries[0]

    checks = evaluate_golden_query(
        query,
        _response(_passing_rows(), "2025-06-25", "2025-06-26"),
        _audit(layer="unified_balance_level", day="2025-06-25"),
        metadata_checks=metadata_contract_checks(catalog, query),
    )

    assert checks
    assert all(item.passed for item in checks), [item for item in checks if not item.passed]


def test_gq_001_exposes_current_year_fallback_and_incomplete_rows_by_layer() -> None:
    catalog = load_golden_catalog(CATALOG)
    query = catalog.queries[0]
    current_rows = [
        {
            "article_name": "Ресурсы",
            "article_scope": "Ресурсы",
            "article_indent": 0,
            "gas_day": "2025-01-01",
            "fact_value": 400013.474,
        },
        {
            "article_name": "  Поступление",
            "article_scope": "Поступление",
            "article_indent": 2,
            "gas_day": "2025-01-01",
            "fact_value": 400013.474,
        },
    ]

    checks = evaluate_golden_query(
        query,
        _response(current_rows, "2025-01-01", "2026-01-01"),
        _audit(layer="unified_strict", day="2025-01-01", with_count=False),
    )
    failed_layers = {item.layer for item in checks if not item.passed}

    assert {"context", "planning", "SQL", "result_shape", "result_data"} <= failed_layers
    assert all(item.passed for item in checks if item.layer == "interpretation")


def test_golden_json_report_exposes_failed_layers() -> None:
    catalog = load_golden_catalog(CATALOG)
    query = catalog.queries[0]
    checks = evaluate_golden_query(
        query,
        _response([], "2025-01-01", "2026-01-01"),
        {"turn_completed": {"execution": {}}},
    )
    item = GoldenQueryResult(
        query_id=query.query_id,
        priority=query.priority,
        title=query.title,
        status="failed",
        checks=checks,
    )
    report = GoldenRunReport(
        run_id="golden-test",
        started_at="2026-08-05T00:00:00Z",
        finished_at="2026-08-05T00:00:01Z",
        base_url="http://test",
        catalog=str(CATALOG),
        database_fixture="fixture",
        metadata_bundle_id="sha256:" + "0" * 64,
        results=(item,),
        summary={"p0_gate_passed": False, "passed": 0, "selected": 1},
    )

    assert report_to_dict(report)["results"][0]["failed_layers"] == item.failed_layers
