from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys
import time
import tomllib
from typing import Any, Mapping, Sequence
import uuid

from .acceptance_runner import AcceptanceHttpClient, AuditLogReader
from .domain_invariants import CANONICAL_VOLUME_UNIT, FORBIDDEN_PLAN_FIELDS


PRIORITIES = ("P0 Core", "P1 Analytics", "P2 Advanced")
LAYERS = (
    "interpretation",
    "binding",
    "context",
    "planning",
    "SQL",
    "result_shape",
    "result_data",
)
_ID_RE = re.compile(r"^GQ-\d{3}$")


class GoldenCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class GoldenQuery:
    query_id: str
    approval_status: str
    priority: str
    title: str
    query: str
    result_shape: str
    value_mode: str
    operation: str
    metric: str
    period: dict[str, Any]
    entities: tuple[dict[str, Any], ...]
    canonical: dict[str, Any]
    execution: dict[str, Any]
    result: dict[str, Any]
    controls: tuple[dict[str, Any], ...]
    clarification: dict[str, Any]
    forbidden: dict[str, Any]


@dataclass(frozen=True)
class GoldenCatalog:
    path: Path
    schema_version: str
    metadata_manifest: Path
    approved_metadata_bundle_id: str
    database_fixture: str
    queries: tuple[GoldenQuery, ...]


@dataclass(frozen=True)
class GoldenCheck:
    layer: str
    name: str
    passed: bool
    expected: Any
    actual: Any


@dataclass
class GoldenQueryResult:
    query_id: str
    priority: str
    title: str
    status: str
    request_id: str | None = None
    session_id: str | None = None
    elapsed_ms: int | None = None
    checks: list[GoldenCheck] = field(default_factory=list)
    error: str | None = None

    @property
    def failed_layers(self) -> list[str]:
        return [layer for layer in LAYERS if any(
            check.layer == layer and not check.passed for check in self.checks
        )]


@dataclass(frozen=True)
class GoldenRunReport:
    run_id: str
    started_at: str
    finished_at: str
    base_url: str
    catalog: str
    database_fixture: str
    metadata_bundle_id: str
    results: tuple[GoldenQueryResult, ...]
    summary: dict[str, Any]


def load_golden_catalog(path: str | Path) -> GoldenCatalog:
    source = Path(path).resolve()
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GoldenCatalogError(f"cannot read Golden Query catalog: {exc}") from exc
    required_root = {
        "schema_version", "metadata_manifest", "approved_metadata_bundle_id",
        "database_fixture", "queries",
    }
    missing = sorted(required_root - set(raw))
    if missing:
        raise GoldenCatalogError(f"catalog misses required fields: {missing}")
    queries = tuple(_parse_query(item) for item in raw["queries"])
    ids = [item.query_id for item in queries]
    if not queries or len(ids) != len(set(ids)):
        raise GoldenCatalogError("Golden Query IDs must be present and unique")
    manifest = (source.parent / str(raw["metadata_manifest"])).resolve()
    return GoldenCatalog(
        path=source,
        schema_version=str(raw["schema_version"]),
        metadata_manifest=manifest,
        approved_metadata_bundle_id=str(raw["approved_metadata_bundle_id"]),
        database_fixture=str(raw["database_fixture"]),
        queries=queries,
    )


def _parse_query(raw: Mapping[str, Any]) -> GoldenQuery:
    required = {
        "id", "approval_status", "priority", "title", "query", "result_shape",
        "value_mode", "operation", "metric", "period", "entities", "canonical",
        "execution", "result", "controls", "clarification", "forbidden",
    }
    missing = sorted(required - set(raw))
    if missing:
        raise GoldenCatalogError(f"Golden Query misses required fields: {missing}")
    query_id = str(raw["id"])
    if not _ID_RE.fullmatch(query_id):
        raise GoldenCatalogError(f"invalid Golden Query ID: {query_id}")
    priority = str(raw["priority"])
    if priority not in PRIORITIES:
        raise GoldenCatalogError(f"{query_id} has invalid priority: {priority}")
    if raw["approval_status"] not in {"approved", "proposed"}:
        raise GoldenCatalogError(f"{query_id} has invalid approval_status")
    if raw["value_mode"] != "fact":
        raise GoldenCatalogError(
            f"{query_id} has invalid value_mode: AI Balances is fact-only"
        )
    result = dict(raw["result"])
    if result.get("unit") != CANONICAL_VOLUME_UNIT:
        raise GoldenCatalogError(
            f"{query_id} must use canonical unit {CANONICAL_VOLUME_UNIT!r}"
        )
    required_columns = {
        str(item).casefold() for item in result.get("required_columns", [])
    }
    if required_columns & FORBIDDEN_PLAN_FIELDS:
        raise GoldenCatalogError(f"{query_id} cannot require plan fields")
    period = dict(raw["period"])
    if set(period) != {"date_from", "date_to"} or period["date_from"] >= period["date_to"]:
        raise GoldenCatalogError(f"{query_id} must define exclusive-end period")
    return GoldenQuery(
        query_id=query_id,
        approval_status=str(raw["approval_status"]),
        priority=priority,
        title=str(raw["title"]),
        query=str(raw["query"]),
        result_shape=str(raw["result_shape"]),
        value_mode=str(raw["value_mode"]),
        operation=str(raw["operation"]),
        metric=str(raw["metric"]),
        period=period,
        entities=tuple(dict(item) for item in raw["entities"]),
        canonical=dict(raw["canonical"]),
        execution=dict(raw["execution"]),
        result=result,
        controls=tuple(dict(item) for item in raw["controls"]),
        clarification=dict(raw["clarification"]),
        forbidden=dict(raw["forbidden"]),
    )


def metadata_contract_checks(catalog: GoldenCatalog, query: GoldenQuery) -> list[GoldenCheck]:
    manifest = json.loads(catalog.metadata_manifest.read_text(encoding="utf-8"))
    checks = [_check(
        "binding", "metadata bundle is the approved fixture",
        catalog.approved_metadata_bundle_id, manifest.get("bundle_id"),
    )]
    base = catalog.metadata_manifest.parent
    balances = _jsonl_by_id(base / manifest["catalogs"]["balances"], "balance_id")
    articles = _jsonl_by_id(base / manifest["catalogs"]["articles"], "article_id")
    for entity in query.entities:
        if entity.get("entity_type") != "balance":
            continue
        raw_id = _numeric_id(entity.get("canonical_id"))
        actual = balances.get(raw_id)
        checks.append(_check(
            "binding", f"canonical balance {entity.get('canonical_id')} exists",
            {"canonical_name": entity.get("canonical_name")},
            {"canonical_name": actual.get("canonical_name")} if actual else None,
        ))
    expected_balance = _numeric_id((query.canonical.get("balance_ids") or [None])[0])
    for control in query.controls:
        article_id = _numeric_id(control.get("canonical_article_id"))
        actual = articles.get(article_id)
        expected = {
            "balance_id": expected_balance,
            "canonical_name": control.get("article_name"),
        }
        observed = ({
            "balance_id": actual.get("balance_id"),
            "canonical_name": actual.get("canonical_name"),
        } if actual else None)
        checks.append(_check(
            "binding", f"control article {control.get('canonical_article_id')} belongs to balance",
            expected, observed,
        ))
    return checks


def evaluate_golden_query(
    query: GoldenQuery,
    response: Mapping[str, Any],
    audit: Mapping[str, Any],
    *,
    metadata_checks: Sequence[GoldenCheck] = (),
) -> list[GoldenCheck]:
    checks = list(metadata_checks)
    response_status = str(response.get("status") or "")
    if response_status == "needs_clarification":
        allowed = bool(query.clarification.get("allowed"))
        clarification_context = response.get("context") if isinstance(response.get("context"), Mapping) else {}
        checks.append(GoldenCheck(
            "interpretation", "clarification policy", allowed,
            {"allowed": allowed, "questions": query.clarification.get("questions") or []},
            clarification_context.get("pending_clarification"),
        ))
        return checks
    checks.append(GoldenCheck(
        "interpretation", "unexpected clarification is absent", True,
        "not needs_clarification", response_status,
    ))
    context = response.get("context") if isinstance(response.get("context"), Mapping) else {}
    active = context.get("active") if isinstance(context.get("active"), Mapping) else {}
    intent = active.get("intent") if isinstance(active.get("intent"), Mapping) else {}
    result = response.get("result") if isinstance(response.get("result"), Mapping) else {}
    rows = [item for item in (result.get("rows") or []) if isinstance(item, Mapping)]
    operands = [item for item in (intent.get("operands") or []) if isinstance(item, Mapping)]
    entities = [entity for operand in operands for entity in (operand.get("entities") or [])]
    periods = _intent_periods(intent, operands)
    completed = audit.get("turn_completed") if isinstance(audit.get("turn_completed"), Mapping) else {}
    execution = completed.get("execution") if isinstance(completed.get("execution"), Mapping) else {}

    metrics = sorted({str(item.get("metric")) for item in operands if item.get("metric")})
    actual_metric: Any = metrics[0] if len(metrics) == 1 else metrics
    checks.extend([
        _check("interpretation", "operation", query.operation, intent.get("operation")),
        _check("interpretation", "metric", query.metric, actual_metric),
        _check("interpretation", "result shape", query.result_shape, _infer_result_shape(intent)),
        _check("context", "exclusive-end period", query.period, periods[0] if len(periods) == 1 else periods),
    ])

    actual_entities = [{
        "role": item.get("role"),
        "entity_type": (item.get("entity") or {}).get("entity_type"),
        "canonical_id": (item.get("entity") or {}).get("entity_id"),
        "canonical_name": (item.get("entity") or {}).get("display_name"),
    } for item in entities]
    expected_entities = [{key: item.get(key) for key in (
        "role", "entity_type", "canonical_id", "canonical_name"
    )} for item in query.entities]
    checks.append(_check("binding", "semantic roles and canonical entities", expected_entities, actual_entities))
    canonical_actual = {
        "balance_ids": sorted(item["canonical_id"] for item in actual_entities if item["entity_type"] == "balance"),
        "article_ids": sorted(item["canonical_id"] for item in actual_entities if item["entity_type"] == "article"),
        "geo_ids": sorted(item["canonical_id"] for item in actual_entities if item["entity_type"] in {"geo_object", "geo_group"}),
        "relation_ids": sorted(item["canonical_id"] for item in actual_entities if item["entity_type"] == "route"),
    }
    for key in ("balance_ids", "article_ids", "geo_ids", "relation_ids"):
        checks.append(_check(
            "binding", f"canonical {key}", sorted(query.canonical.get(key) or []), canonical_actual[key],
        ))

    forbidden = query.forbidden
    if forbidden.get("article_level"):
        article_refs = [item for item in actual_entities if item["role"] == "article"]
        checks.append(_check("binding", "full balance has no article binding", [], article_refs))
    forbidden_balances = set(forbidden.get("canonical_balance_ids") or [])
    checks.append(GoldenCheck(
        "binding", "forbidden balances are not selected",
        not forbidden_balances.intersection(canonical_actual["balance_ids"]),
        sorted(forbidden_balances), canonical_actual["balance_ids"],
    ))
    forbidden_periods = [dict(date_from=item[0], date_to=item[1]) for item in forbidden.get("periods", [])]
    checks.append(GoldenCheck(
        "context", "forbidden periods are not selected",
        not any(item in forbidden_periods for item in periods), forbidden_periods, periods,
    ))

    allowed_layers = query.execution.get("allowed_layers") or []
    checks.append(GoldenCheck(
        "planning", "execution layer", execution.get("layer") in allowed_layers,
        allowed_layers, execution.get("layer"),
    ))
    required_count = query.execution.get("source_execution_count")
    if required_count is not None:
        checks.append(_check(
            "planning", "source execution count", required_count,
            execution.get("source_execution_count"),
        ))
    checks.append(_check(
        "SQL", "SQL function", query.execution.get("sql_function"),
        execution.get("sql_function"),
    ))
    actual_params = execution.get("sql_params") if isinstance(execution.get("sql_params"), Mapping) else {}
    for key, expected in (query.execution.get("required_params") or {}).items():
        checks.append(_check("SQL", f"SQL parameter {key}", expected, actual_params.get(key)))
    forbidden_params = query.execution.get("forbidden_params") or []
    checks.append(GoldenCheck(
        "SQL", "forbidden article parameters are absent",
        not any(key in actual_params for key in forbidden_params), forbidden_params,
        sorted(actual_params),
    ))

    expected_result = query.result
    checks.extend([
        _check("result_shape", "result status", expected_result.get("status"), result.get("status")),
        GoldenCheck(
            "result_shape", "full table row count",
            len(rows) >= int(expected_result.get("min_rows", 0)),
            {"min_rows": expected_result.get("min_rows")}, len(rows),
        ),
    ])
    columns = sorted({str(key) for row in rows for key in row})
    required_columns = list(expected_result.get("required_columns") or [])
    checks.append(GoldenCheck(
        "result_shape", "required result columns",
        set(required_columns).issubset(columns), required_columns, columns,
    ))
    checks.append(_fact_only_check(columns))
    if expected_result.get("hierarchy_required"):
        indents = [item.get("article_indent") for item in rows if item.get("article_indent") is not None]
        checks.append(GoldenCheck(
            "result_shape", "hierarchy levels are preserved",
            bool(indents) and len(set(indents)) >= 3 and indents[0] == 0,
            "at least three levels starting at 0", sorted(set(indents)) if indents else [],
        ))
    sections = {_normalized_name(item.get("article_scope") or item.get("article_name")) for item in rows}
    required_sections = list(expected_result.get("required_sections") or [])
    checks.append(GoldenCheck(
        "result_shape", "required balance sections",
        set(required_sections).issubset(sections), required_sections, sorted(sections),
    ))
    expected_unit = expected_result.get("unit")
    units = {str(item.get("unit")) for item in rows if item.get("unit") not in (None, "")}
    if result.get("unit") not in (None, ""):
        units.add(str(result.get("unit")))
    fact_rows = [item for item in rows if item.get("fact_value") is not None]
    unit_contract_holds = (
        expected_unit == CANONICAL_VOLUME_UNIT
        and bool(fact_rows)
        and all(item.get("unit") == CANONICAL_VOLUME_UNIT for item in fact_rows)
        and units == {CANONICAL_VOLUME_UNIT}
    )
    checks.append(GoldenCheck(
        "result_shape", "canonical public unit", unit_contract_holds,
        CANONICAL_VOLUME_UNIT, sorted(units),
    ))
    if expected_result.get("source_order_required"):
        ordered = all(
            0 <= int(control["row_index"]) < len(rows)
            and _normalized_name(rows[int(control["row_index"])].get("article_name"))
            == control.get("article_name")
            for control in query.controls
        )
        checks.append(GoldenCheck(
            "result_shape", "source row order", ordered,
            [{"index": item["row_index"], "article": item["article_name"]} for item in query.controls],
            [{"index": item["row_index"], "article": _normalized_name(rows[int(item["row_index"])].get("article_name"))}
             for item in query.controls if 0 <= int(item["row_index"]) < len(rows)],
        ))
    if forbidden.get("scalar_result"):
        checks.append(GoldenCheck(
            "result_shape", "result is not scalar", len(rows) > 1,
            "more than one row", len(rows),
        ))

    for control in query.controls:
        checks.extend(_control_checks(control, rows))
    return checks


def run_golden_queries(
    catalog: GoldenCatalog,
    selected: Sequence[GoldenQuery],
    *,
    base_url: str,
    log_file: str | Path,
    api_key: str | None = None,
    timeout: float = 120.0,
    keep_sessions: bool = False,
) -> GoldenRunReport:
    client = AcceptanceHttpClient(base_url, api_key=api_key, timeout=timeout)
    health = client.health()
    if health.status_code != 200 or health.body.get("status") != "ok":
        raise RuntimeError(f"backend health is not ready: HTTP {health.status_code}")
    audit_reader = AuditLogReader(log_file)
    started = datetime.now(UTC)
    results: list[GoldenQueryResult] = []
    for query in selected:
        request_id = f"golden-{query.query_id.lower()}-{uuid.uuid4()}"
        created = client.create_session()
        session_id = (created.body.get("session") or {}).get("session_id")
        item = GoldenQueryResult(
            query_id=query.query_id, priority=query.priority, title=query.title,
            status="failed", request_id=request_id, session_id=session_id,
        )
        if created.status_code != 201 or not session_id:
            item.error = f"session creation returned HTTP {created.status_code}"
            results.append(item)
            continue
        try:
            response = client.chat({
                "session_id": session_id,
                "expected_revision": 0,
                "request_id": request_id,
                "message": query.query,
                "execute_db": True,
            })
            item.elapsed_ms = response.elapsed_ms
            time.sleep(0.05)
            checks = evaluate_golden_query(
                query, response.body, audit_reader.for_request(request_id),
                metadata_checks=metadata_contract_checks(catalog, query),
            )
            item.checks.extend(checks)
            item.status = "passed" if response.status_code == 200 and all(
                check.passed for check in checks
            ) else "failed"
            if response.status_code != 200:
                item.error = f"chat returned HTTP {response.status_code}"
        except Exception as exc:
            item.error = f"{type(exc).__name__}: {exc}"
        finally:
            if not keep_sessions:
                client.delete_session(session_id)
        results.append(item)
    finished = datetime.now(UTC)
    passed = sum(item.status == "passed" for item in results)
    p0 = [item for item in results if item.priority == "P0 Core"]
    summary = {
        "selected": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "p0_passed": sum(item.status == "passed" for item in p0),
        "p0_total": len(p0),
        "p0_gate_passed": bool(p0) and all(item.status == "passed" for item in p0),
        "failed_layers": sorted({layer for item in results for layer in item.failed_layers}),
    }
    return GoldenRunReport(
        run_id=f"golden-{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
        started_at=started.isoformat(), finished_at=finished.isoformat(),
        base_url=base_url, catalog=str(catalog.path),
        database_fixture=catalog.database_fixture,
        metadata_bundle_id=catalog.approved_metadata_bundle_id,
        results=tuple(results), summary=summary,
    )


def _check(layer: str, name: str, expected: Any, actual: Any) -> GoldenCheck:
    return GoldenCheck(layer, name, expected == actual, expected, actual)


def _jsonl_by_id(path: Path, key: str) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        result[int(item[key])] = item
    return result


def _numeric_id(value: Any) -> int | None:
    match = re.search(r"(\d+)$", str(value or ""))
    return int(match.group(1)) if match else None


def _intent_periods(intent: Mapping[str, Any], operands: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    values = intent.get("periods") or []
    if not values and len(operands) == 1:
        values = operands[0].get("periods") or []
    return [{"date_from": item.get("date_from"), "date_to": item.get("date_to")} for item in values]


def _infer_result_shape(intent: Mapping[str, Any]) -> str | None:
    operands = intent.get("operands") or []
    if intent.get("operation") == "show" and len(operands) == 1:
        operand = operands[0]
        entities = operand.get("entities") or []
        if operand.get("metric") == "balance" and any(
            item.get("role") == "balance" for item in entities
        ) and not any(item.get("role") == "article" for item in entities):
            return "balance_snapshot"
    return None


def _normalized_name(value: Any) -> str:
    return str(value or "").strip()


def _fact_only_check(columns: Sequence[str]) -> GoldenCheck:
    lowered = {item.casefold() for item in columns}
    has_plan = bool(lowered & FORBIDDEN_PLAN_FIELDS)
    has_fact = bool(lowered & {"fact", "fact_value"})
    actual = "fact" if has_fact and not has_plan else "plan_present" if has_plan else "none"
    return _check("result_shape", "fact-only result", "fact", actual)


def _control_checks(control: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[GoldenCheck]:
    index = int(control["row_index"])
    row = rows[index] if 0 <= index < len(rows) else {}
    prefix = f"control row {index} ({control.get('canonical_article_id')})"
    checks = [
        _check("result_data", f"{prefix} article", control.get("article_name"), _normalized_name(row.get("article_name"))),
        _check("result_data", f"{prefix} indent", control.get("article_indent"), row.get("article_indent")),
        _check("result_data", f"{prefix} gas day", control.get("gas_day"), row.get("gas_day")),
    ]
    expected = control.get("fact_value")
    actual = row.get("fact_value")
    try:
        delta = abs(Decimal(str(expected)) - Decimal(str(actual)))
        passed = delta <= Decimal(str(control.get("tolerance", 0)))
    except (InvalidOperation, TypeError):
        passed = False
    checks.append(GoldenCheck(
        "result_data", f"{prefix} fact value", passed, expected, actual,
    ))
    return checks


def report_to_dict(report: GoldenRunReport) -> dict[str, Any]:
    payload = asdict(report)
    for raw, item in zip(payload["results"], report.results, strict=True):
        raw["failed_layers"] = item.failed_layers
    return payload


def render_markdown(report: GoldenRunReport) -> str:
    lines = [
        "# Golden Queries report", "",
        f"- Run: `{report.run_id}`",
        f"- Database fixture: `{report.database_fixture}`",
        f"- P0 gate: **{'PASS' if report.summary['p0_gate_passed'] else 'FAIL'}**",
        f"- Passed: {report.summary['passed']}/{report.summary['selected']}", "",
    ]
    for item in report.results:
        lines.extend([
            f"## {item.query_id}: {item.title}", "",
            f"Status: **{item.status.upper()}**. Failed layers: "
            f"{', '.join(item.failed_layers) or 'none'}.", "",
            "| Layer | Check | Status | Expected | Actual |",
            "|---|---|---:|---|---|",
        ])
        for check in item.checks:
            lines.append(
                f"| {check.layer} | {check.name} | {'PASS' if check.passed else 'FAIL'} | "
                f"`{_brief(check.expected)}` | `{_brief(check.actual)}` |"
            )
        if item.error:
            lines.extend(["", f"Error: `{item.error}`"])
        lines.append("")
    return "\n".join(lines)


def _brief(value: Any, limit: int = 180) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _write_reports(report: GoldenRunReport, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stem = report.run_id
    json_path = directory / f"{stem}.json"
    md_path = directory / f"{stem}.md"
    json_path.write_text(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report) + "\n", encoding="utf-8")
    return json_path, md_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run approved AI Balances Golden Queries")
    parser.add_argument("--catalog", default="golden/golden_queries.toml")
    parser.add_argument("--base-url", default="http://127.0.0.1:8790")
    parser.add_argument("--log-file", default="logs/balance_chat.jsonl")
    parser.add_argument("--report-dir", default="reports/golden")
    parser.add_argument("--query", action="append", dest="query_ids")
    parser.add_argument("--priority", choices=PRIORITIES)
    parser.add_argument("--api-key-env", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--keep-sessions", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        catalog = load_golden_catalog(args.catalog)
        selected = [item for item in catalog.queries if item.approval_status == "approved"]
        if args.query_ids:
            selected = [item for item in selected if item.query_id in set(args.query_ids)]
        if args.priority:
            selected = [item for item in selected if item.priority == args.priority]
        if not selected:
            raise GoldenCatalogError("no approved Golden Queries selected")
        for item in selected:
            checks = metadata_contract_checks(catalog, item)
            if not all(check.passed for check in checks):
                raise GoldenCatalogError(f"{item.query_id} canonical metadata validation failed")
        if args.validate_only:
            print(f"validated {len(selected)} Golden Queries: {', '.join(item.query_id for item in selected)}")
            return 0
        log_file = Path(args.log_file)
        if not log_file.exists():
            raise GoldenCatalogError("--log-file must point to the backend structured JSONL log")
        api_key = None
        if args.api_key_env:
            import os
            api_key = os.getenv(args.api_key_env)
        report = run_golden_queries(
            catalog, selected, base_url=args.base_url, log_file=log_file,
            api_key=api_key, timeout=args.timeout, keep_sessions=args.keep_sessions,
        )
        json_path, md_path = _write_reports(report, Path(args.report_dir))
        print(f"Golden Queries: {report.summary['passed']}/{report.summary['selected']} passed")
        print(f"P0 gate: {'PASS' if report.summary['p0_gate_passed'] else 'FAIL'}")
        print(f"Failed layers: {', '.join(report.summary['failed_layers']) or 'none'}")
        print(f"Reports: {json_path} {md_path}")
        return 0 if report.summary["p0_gate_passed"] and not report.summary["failed"] else 1
    except (GoldenCatalogError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"golden query error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
