from __future__ import annotations

from pathlib import Path

import pytest

from balance_chat.golden_queries import (
    GoldenQueryResult,
    GoldenRunReport,
    GoldenCatalogError,
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


def _contract_response(query, rows: list[dict]) -> dict:
    entities = [{
        "role": item["role"],
        "entity": {
            "entity_id": item["canonical_id"],
            "entity_type": item["entity_type"],
            "display_name": item["canonical_name"],
        },
    } for item in query.entities]
    intent = {
        "operation": query.operation,
        "operands": [{
            "operand_id": "snapshot",
            "metric": query.metric,
            "aggregate_type": "sum",
            "entities": entities,
            "periods": [],
        }],
        "periods": [query.period],
        "grouping": [],
    }
    return {
        "status": "ok",
        "context": {"active": {"intent": intent}},
        "result": {"status": "ok", "rows": rows},
    }


def _rows_for_contract(query) -> list[dict]:
    count = int(query.result["row_count"])
    rows = [
        _row(f"Статья {index}", 4 if index % 2 else 2, float(index))
        for index in range(count)
    ]
    for control in query.controls:
        rows[int(control["row_index"])] = _row(
            str(control["article_name"]),
            int(control["article_indent"]),
            float(control["fact_value"]),
            day=str(control["gas_day"]),
        )
    return rows


def test_gq_001_catalog_is_explicit_and_canonical_metadata_is_valid() -> None:
    catalog = load_golden_catalog(CATALOG)

    assert sorted(item.query_id for item in catalog.queries) == [
        "GQ-001", "GQ-002", "GQ-003", "GQ-004",
        "GQ-005", "GQ-006", "GQ-007", "GQ-008",
    ]
    query = catalog.queries[0]
    assert query.approval_status == "approved"
    assert query.priority == "P0 Core"
    assert query.result_shape == "balance_snapshot"
    assert query.value_mode == "fact"
    assert all(item.passed for item in metadata_contract_checks(catalog, query))


@pytest.mark.parametrize("query_id", ["GQ-002", "GQ-003", "GQ-004"])
def test_section_snapshot_contracts_are_canonical_and_complete(query_id: str) -> None:
    catalog = load_golden_catalog(CATALOG)
    query = next(item for item in catalog.queries if item.query_id == query_id)
    rows = _rows_for_contract(query)

    checks = evaluate_golden_query(
        query,
        _contract_response(query, rows),
        _audit(layer="unified_balance_section", day="2025-06-25"),
        metadata_checks=metadata_contract_checks(catalog, query),
    )

    assert query.result_shape == "section_snapshot"
    assert query.metric == "balance_section"
    assert all(item.passed for item in checks), [item for item in checks if not item.passed]


@pytest.mark.parametrize("query_id", ["GQ-005", "GQ-006", "GQ-007"])
def test_directed_flow_contracts_are_canonical_and_complete(query_id: str) -> None:
    catalog = load_golden_catalog(CATALOG)
    query = next(item for item in catalog.queries if item.query_id == query_id)
    rows = _rows_for_contract(query)

    checks = evaluate_golden_query(
        query,
        _contract_response(query, rows),
        _audit(layer="unified_directed_flow", day="2025-06-25"),
        metadata_checks=metadata_contract_checks(catalog, query),
    )

    assert query.result_shape == "directed_flow_value"
    assert query.metric in {"incoming", "distribution"}
    assert all(item.passed for item in checks), [item for item in checks if not item.passed]


def test_gq_008_requires_two_canonical_directions_and_two_source_executions() -> None:
    catalog = load_golden_catalog(CATALOG)
    query = next(item for item in catalog.queries if item.query_id == "GQ-008")
    entity_refs = [{
        "role": item["role"],
        "entity": {
            "entity_id": item["canonical_id"],
            "entity_type": item["entity_type"],
            "display_name": item["canonical_name"],
        },
    } for item in query.entities]
    intent = {
        "operation": "compare",
        "operands": [
            {
                "operand_id": "direction_1",
                "metric": "distribution",
                "aggregate_type": "sum",
                "entities": entity_refs[:3],
                "periods": [],
            },
            {
                "operand_id": "direction_2",
                "metric": "distribution",
                "aggregate_type": "sum",
                "entities": entity_refs[3:],
                "periods": [],
            },
        ],
        "periods": [query.period],
        "grouping": [],
    }
    facts = [
        {
            "task_id": "task_1", "value": "2512.377", "unit": "тыс. м3",
            "label": "ТГ Томск", "periods": [query.period],
        },
        {
            "task_id": "task_2", "value": "9136.25", "unit": "тыс. м3",
            "label": "ТГ Сургут", "periods": [query.period],
        },
    ]
    actual_tasks = []
    for expected_task in query.execution["required_tasks"]:
        actual_task = dict(expected_task)
        period = actual_task.pop("period")
        actual_task["periods"] = [{**period, "label": None}]
        actual_tasks.append(actual_task)
    audit = {
        "turn_completed": {
            "execution": {
                "layer": "native_deterministic",
                "source_execution_count": 2,
                "tasks": actual_tasks,
            }
        }
    }

    checks = evaluate_golden_query(
        query,
        {
            "status": "ok",
            "context": {"active": {"intent": intent}},
            "result": {"status": "ok", "facts": facts},
        },
        audit,
        metadata_checks=metadata_contract_checks(catalog, query),
    )

    assert query.result_shape == "directed_flow_comparison"
    assert all(item.passed for item in checks), [item for item in checks if not item.passed]


@pytest.mark.parametrize("mode", ["plan", "both"])
def test_golden_catalog_rejects_plan_modes(tmp_path: Path, mode: str) -> None:
    source = CATALOG.read_text(encoding="utf-8").replace(
        'value_mode = "fact"', f'value_mode = "{mode}"', 1
    )
    path = tmp_path / "golden.toml"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(GoldenCatalogError, match="fact-only"):
        load_golden_catalog(path)


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


def test_gq_001_rejects_plan_columns_and_noncanonical_units() -> None:
    catalog = load_golden_catalog(CATALOG)
    query = catalog.queries[0]
    rows = _passing_rows()
    rows[0]["plan_value"] = rows[0]["fact_value"]
    rows[1]["unit"] = "млн м3"

    checks = evaluate_golden_query(
        query,
        _response(rows, "2025-06-25", "2025-06-26"),
        _audit(layer="unified_balance_level", day="2025-06-25"),
    )

    failed = {item.name for item in checks if not item.passed}
    assert "fact-only result" in failed
    assert "canonical public unit" in failed


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
