from __future__ import annotations

from pathlib import Path

from balance_chat.acceptance_runner import (
    ActionResult,
    AcceptanceRunner,
    AuditLogReader,
    Catalog,
    CheckResult,
    Expectation,
    HttpResponse,
    RunReport,
    Scenario,
    ScenarioResult,
    compile_expectation,
    main,
    parse_catalog,
    report_to_dict,
    render_markdown,
    select_scenarios,
)
from balance_chat.acceptance_rules import (
    RuleContext,
    catalog_rule_count,
    evaluate_catalog_rule,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "acceptance" / "context_chat_business_scenarios.feature"


def test_business_catalog_has_stable_ids_and_contract_partition() -> None:
    catalog = parse_catalog(CATALOG)

    assert len(catalog.scenarios) == 18
    assert [item.scenario_id for item in catalog.scenarios] == [
        f"BC-{number:02d}" for number in range(1, 19)
    ]
    assert sum("current-contract" in item.tags for item in catalog.scenarios) == 12
    assert sum("contract-gap" in item.tags for item in catalog.scenarios) == 6
    assert sum(len(item.actions) for item in catalog.scenarios) >= 119
    operational = next(item for item in catalog.scenarios if item.scenario_id == "BC-18")
    assert [item.kind for item in operational.actions] == [
        "query", "query", "restart", "query", "concurrent", "replay", "query", "query"
    ]
    assert operational.actions[4].message == "Покажи распределение за июнь 2025"
    reverse = next(item for item in catalog.scenarios if item.scenario_id == "BC-02")
    resilient = next(item for item in catalog.scenarios if item.scenario_id == "BC-14")
    assert [item.turn_id for item in reverse.actions] == [f"T{number}" for number in range(1, 8)]
    assert [item.turn_id for item in resilient.actions] == [f"T{number}" for number in range(1, 8)]
    assert resilient.actions[5].kind == "replay"


def test_default_selection_excludes_explicit_contract_gaps() -> None:
    catalog = parse_catalog(CATALOG)

    selected = select_scenarios(catalog)

    assert len(selected) == 12
    assert all("current-contract" in item.tags for item in selected)
    assert {item.scenario_id for item in select_scenarios(catalog, tags=["P0"])} == {
        "BC-01", "BC-02", "BC-03", "BC-04", "BC-05", "BC-14", "BC-15"
    }


def test_expectation_compiler_extracts_typed_semantics() -> None:
    checks = compile_expectation(
        Expectation(
            'T1 имеет operation "show", metric "distribution" и GEO "Самарская область" '
            "за период [2025-03-01, 2025-06-01)",
            line=10,
        )
    )

    assert [(item.kind, item.expected) for item in checks] == [
        ("operation", "show"),
        ("metric", "distribution"),
        ("period", ["2025-03-01", "2025-06-01"]),
        ("entity", {"role": "geo", "label": "Самарская область"}),
    ]


class FakeClient:
    base_url = "http://acceptance.test"

    def health(self):
        return HttpResponse(200, {"status": "ok"}, 1)

    def create_session(self):
        return HttpResponse(
            201,
            {"status": "ok", "session": {"session_id": "session-1", "revision": 0}},
            1,
        )

    def chat(self, payload):
        return HttpResponse(
            200,
            {
                "status": "ok",
                "session": {"session_id": "session-1", "revision": 1},
                "context": {
                    "active": {
                        "intent": {
                            "operation": "show",
                            "grain": None,
                            "periods": [],
                            "operands": [
                                {
                                    "metric": "distribution",
                                    "aggregate_type": "sum",
                                    "periods": [
                                        {"date_from": "2025-03-01", "date_to": "2025-06-01"}
                                    ],
                                    "entities": [
                                        {
                                            "role": "destination",
                                            "entity": {"display_name": "Самарская область"},
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                },
                "result": {
                    "status": "ok",
                    "operation": "show",
                    "facts": [{"label": "Самарская область", "value": "1", "unit": "тыс. м3"}],
                    "summary": {"title": "Поставки в Самарскую область"},
                },
            },
            5,
        )

    def delete_session(self, session_id):
        return HttpResponse(200, {"status": "ok", "deleted": True}, 1)


def test_runner_executes_catalog_turn_and_checks_semantic_response(tmp_path: Path) -> None:
    feature = tmp_path / "scenario.feature"
    feature.write_text(
        """# language: ru
@BC-01 @P0 @current-contract
Сценарий: smoke
  Когда T1 пользователь спрашивает "Покажи поставки"
  Тогда T1 имеет operation "show", metric "distribution" и GEO "Самарская область"
  И T1 имеет период [2025-03-01, 2025-06-01)
""",
        encoding="utf-8",
    )
    catalog = parse_catalog(feature)
    runner = AcceptanceRunner(FakeClient(), execute_db=True)

    report = runner.run(catalog, catalog.scenarios)

    assert report.summary["passed"] == 1
    assert report.summary["failed"] == 0
    assert report.summary["coverage_rate"] == 100
    assert report.scenarios[0].session_id == "session-1"
    assert report.scenarios[0].actions[0].snapshot["metrics"] == ["distribution"]
    assert "BC-01: smoke" in render_markdown(report)


def test_dry_run_exposes_unautomated_natural_language_expectations() -> None:
    scenario = Scenario(
        scenario_id="BC-99",
        name="coverage",
        tags=("BC-99", "current-contract"),
        line=1,
    )
    from balance_chat.acceptance_runner import Action

    scenario.actions.append(
        Action(
            turn_id="T1",
            kind="query",
            line=2,
            message="query",
            expectations=[Expectation("T1 не выбирает случайную статью", 3)],
        )
    )
    catalog = Catalog("memory.feature", (scenario,))

    report = AcceptanceRunner(FakeClient(), execute_db=False).run(
        catalog, catalog.scenarios, dry_run=True
    )

    assert report.summary["unsupported_expectation_count"] == 1
    assert report.summary["coverage_rate"] == 0


def test_current_contract_catalog_has_no_automation_gaps() -> None:
    catalog = parse_catalog(CATALOG)
    selected = select_scenarios(catalog)

    report = AcceptanceRunner(FakeClient(), execute_db=False).run(
        catalog, selected, dry_run=True
    )

    assert catalog_rule_count() == 68
    assert report.summary["unsupported_expectation_count"] == 0
    assert report.summary["coverage_rate"] == 100
    assert report.summary["automated_expectation_lines"] == 104


def _semantic_response(*, geo: str, metric: str = "distribution") -> dict:
    return {
        "status": "ok",
        "session": {"session_id": "s1", "revision": 1},
        "context": {
            "active": {
                "turn_id": "turn-1",
                "intent": {
                    "operation": "show",
                    "periods": [],
                    "grouping": [],
                    "grain": None,
                    "comparison": None,
                    "formula": None,
                    "ranking": None,
                    "operands": [{
                        "operand_id": "op1",
                        "metric": metric,
                        "aggregate_type": "sum",
                        "periods": [{"date_from": "2025-03-01", "date_to": "2025-06-01"}],
                        "entities": [{
                            "role": "destination",
                            "entity": {
                                "entity_id": f"GEO:{geo}",
                                "entity_type": "geo_object",
                                "display_name": geo,
                            },
                        }],
                    }],
                },
            },
            "last_successful": {"turn_id": "turn-1"},
        },
        "result": {"status": "ok", "facts": []},
    }


def test_inheritance_catalog_rule_rejects_changed_geo() -> None:
    source = _semantic_response(geo="Самарская область")
    inherited = _semantic_response(geo="Самарская область")
    changed = _semantic_response(geo="Казань")
    history = {"T1": {"response": source}}

    passed = evaluate_catalog_rule(
        "inherit_metric_geo_t1",
        RuleContext(current=inherited, history=history, audit={}),
    )
    failed = evaluate_catalog_rule(
        "inherit_metric_geo_t1",
        RuleContext(current=changed, history=history, audit={}),
    )

    assert passed.passed
    assert not failed.passed


def test_audit_reader_correlates_only_requested_structured_events(tmp_path: Path) -> None:
    log = tmp_path / "balance_chat.jsonl"
    log.write_text(
        "\n".join([
            '{"event":"turn_completed","request_id":"r1","result_fingerprint":"sha256:one"}',
            '{"event":"turn_completed","request_id":"r2","result_fingerprint":"sha256:two"}',
            '{"event":"conversation_turn_committed","request_id":"r1","turn_handle":"t0001"}',
        ]) + "\n",
        encoding="utf-8",
    )
    reader = AuditLogReader(log)

    events = reader.for_request("r1")

    assert set(events) == {"turn_completed", "conversation_turn_committed"}
    assert events["turn_completed"]["result_fingerprint"] == "sha256:one"
    assert events["conversation_turn_committed"]["turn_handle"] == "t0001"


def test_report_serialization_normalizes_nested_evaluator_sets() -> None:
    report = RunReport(
        run_id="run",
        started_at="2026-08-05T00:00:00+00:00",
        finished_at="2026-08-05T00:00:01+00:00",
        base_url="http://127.0.0.1:8790",
        catalog="catalog.feature",
        execute_db=True,
        dry_run=False,
        health={},
        scenarios=[
            ScenarioResult(
                scenario_id="BC-13",
                name="fingerprints",
                tags=("current-contract",),
                actions=[
                    ActionResult(
                        turn_id="T2",
                        kind="query",
                        message="same meaning",
                        checks=[
                            CheckResult(
                                description="entity handles",
                                passed=True,
                                expected={"handles"},
                                actual={
                                    "current": {
                                        ("geo:kazan", "t0001.e.geo-kazan"),
                                        ("geo:samara", "t0001.e.geo-samara"),
                                    }
                                },
                                source_line=1,
                            )
                        ],
                    )
                ],
            )
        ],
        summary={},
    )

    payload = report_to_dict(report)
    check = payload["scenarios"][0]["actions"][0]["checks"][0]

    assert check["expected"] == ["handles"]
    assert check["actual"]["current"] == [
        ["geo:kazan", "t0001.e.geo-kazan"],
        ["geo:samara", "t0001.e.geo-samara"],
    ]


def test_live_run_rejects_missing_backend_audit_log(
    tmp_path: Path, capsys
) -> None:
    missing = tmp_path / "missing.jsonl"

    code = main([
        "--catalog",
        str(CATALOG),
        "--scenario",
        "BC-01",
        "--log-file",
        str(missing),
    ])

    assert code == 2
    assert "must point to the existing backend JSONL log" in capsys.readouterr().err
