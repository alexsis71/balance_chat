from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

from .acceptance_rules import (
    RuleContext,
    catalog_rule,
    evaluate_catalog_rule,
)


_SCENARIO_RE = re.compile(r"^\s*Сценарий:\s*(?P<name>.+?)\s*$")
_QUERY_RE = re.compile(
    r'^\s*(?:Когда|И)\s+T(?P<number>\d+)\s+пользователь\s+(?:спрашивает|уточняет)\s+"(?P<message>.*)"\s*$'
)
_CLARIFICATION_RE = re.compile(
    r'^\s*(?:Когда|И)\s+T(?P<number>\d+)\s+пользователь\s+выбирает\s+вариант\s+"(?P<option>.*)"\s*$'
)
_REPLAY_RE = re.compile(
    r"^\s*(?:Когда|И)\s+T(?P<number>\d+)\s+пользователь\s+повторяет\s+"
    r"(?:(?:победивший|тот\s+же)\s+)?request_id\s+T(?P<source>\d+)\s*$"
)
_RESTART_RE = re.compile(r"^\s*Когда\s+backend\s+перезапускается\s+после\s+T(?P<source>\d+)\s*$")
_CONCURRENT_RE = re.compile(
    r'^\s*Когда\s+два\s+запроса\s+T(?P<number>\d+)\s+с\s+текстом\s+"(?P<message>.*)"\s+'
    r"одновременно\s+отправлены\s+"
    r"с\s+expected_revision\s+(?P<revision>\d+)\s*$"
)
_EXPECTATION_RE = re.compile(r"^\s*(?:Тогда|И)\s+(?P<text>.+?)\s*$")
_TAG_RE = re.compile(r"@([\w-]+)")
_PERIOD_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}),\s*(\d{4}-\d{2}-\d{2})\)")


@dataclass(frozen=True)
class Expectation:
    text: str
    line: int


@dataclass
class Action:
    turn_id: str
    kind: str
    line: int
    message: str | None = None
    option: str | None = None
    source_turn_id: str | None = None
    expected_revision: int | None = None
    expectations: list[Expectation] = field(default_factory=list)


@dataclass
class Scenario:
    scenario_id: str
    name: str
    tags: tuple[str, ...]
    line: int
    actions: list[Action] = field(default_factory=list)


@dataclass(frozen=True)
class Catalog:
    path: str
    scenarios: tuple[Scenario, ...]


class CatalogError(ValueError):
    pass


def parse_catalog(path: str | Path) -> Catalog:
    source = Path(path)
    pending_tags: tuple[str, ...] = ()
    scenarios: list[Scenario] = []
    current: Scenario | None = None
    current_action: Action | None = None

    for line_number, raw in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith("@"):
            pending_tags = tuple(_TAG_RE.findall(stripped))
            continue

        match = _SCENARIO_RE.match(raw)
        if match:
            identifiers = [tag for tag in pending_tags if re.fullmatch(r"BC-\d{2}", tag)]
            if len(identifiers) != 1:
                raise CatalogError(
                    f"scenario at line {line_number} must have exactly one @BC-NN tag"
                )
            current = Scenario(
                scenario_id=identifiers[0],
                name=match.group("name"),
                tags=pending_tags,
                line=line_number,
            )
            scenarios.append(current)
            current_action = None
            continue

        if current is None:
            continue

        action = _parse_action(raw, line_number)
        if action is not None:
            current.actions.append(action)
            current_action = action
            continue

        match = _EXPECTATION_RE.match(raw)
        if match and current_action is not None:
            current_action.expectations.append(
                Expectation(text=match.group("text"), line=line_number)
            )

    ids = [scenario.scenario_id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise CatalogError("scenario IDs must be unique")
    if not scenarios:
        raise CatalogError("catalog contains no scenarios")
    return Catalog(path=str(source), scenarios=tuple(scenarios))


def _parse_action(raw: str, line_number: int) -> Action | None:
    match = _QUERY_RE.match(raw)
    if match:
        return Action(
            turn_id=f"T{match.group('number')}",
            kind="query",
            line=line_number,
            message=match.group("message"),
        )
    match = _CLARIFICATION_RE.match(raw)
    if match:
        return Action(
            turn_id=f"T{match.group('number')}",
            kind="clarification",
            line=line_number,
            option=match.group("option"),
        )
    match = _REPLAY_RE.match(raw)
    if match:
        return Action(
            turn_id=f"T{match.group('number')}",
            kind="replay",
            line=line_number,
            source_turn_id=f"T{match.group('source')}",
        )
    match = _RESTART_RE.match(raw)
    if match:
        return Action(
            turn_id=f"restart-after-T{match.group('source')}",
            kind="restart",
            line=line_number,
            source_turn_id=f"T{match.group('source')}",
        )
    match = _CONCURRENT_RE.match(raw)
    if match:
        return Action(
            turn_id=f"T{match.group('number')}",
            kind="concurrent",
            line=line_number,
            message=match.group("message"),
            expected_revision=int(match.group("revision")),
        )
    return None


@dataclass(frozen=True)
class CompiledCheck:
    kind: str
    expected: Any
    description: str


def compile_expectation(expectation: Expectation) -> tuple[CompiledCheck, ...]:
    text = expectation.text
    checks: list[CompiledCheck] = []

    for value in re.findall(r'operation\s+"([^"]+)"', text):
        checks.append(CompiledCheck("operation", value, f'operation = "{value}"'))
    for value in re.findall(r'metric\s+"([^"]+)"', text):
        checks.append(CompiledCheck("metric", value, f'metric contains "{value}"'))
    for value in re.findall(r'aggregate\s+"([^"]+)"', text):
        checks.append(CompiledCheck("aggregate", value, f'aggregate contains "{value}"'))
    for value in re.findall(r'rank(?:\s+на)?\s+"([^"]+)"', text):
        checks.append(CompiledCheck("ranking_direction", value, f'ranking direction = "{value}"'))
    for value in re.findall(r'(?:period grain|grain)\s+"([^"]+)"', text):
        checks.append(CompiledCheck("grain", value, f'grain = "{value}"'))
    for date_from, date_to in _PERIOD_RE.findall(text):
        checks.append(
            CompiledCheck(
                "period",
                [date_from, date_to],
                f"period contains [{date_from}, {date_to})",
            )
        )

    for label in re.findall(r'GEO\s+"([^"]+)"', text):
        checks.append(CompiledCheck("entity", {"role": "geo", "label": label}, f'GEO = "{label}"'))
    for role, label in re.findall(r'(source|destination)\s+business\s+"([^"]+)"', text):
        checks.append(
            CompiledCheck("entity", {"role": role, "label": label}, f'{role} business = "{label}"')
        )

    count_match = re.search(r"\b(один|два|две|три|четыре)\b[^.]*?\boperand\b", text, re.IGNORECASE)
    negative_count = re.search(r"\bни\s+один\b[^.]*?\boperand\b", text, re.IGNORECASE)
    if count_match and not negative_count:
        count = {"один": 1, "два": 2, "две": 2, "три": 3, "четыре": 4}[count_match.group(1).lower()]
        checks.append(CompiledCheck("operand_count", count, f"operand count = {count}"))

    if re.search(r"(?:strict\s+)?no_data|возвращает\s+no_data", text, re.IGNORECASE):
        checks.append(CompiledCheck("result_status", "no_data", "result status = no_data"))
    if re.search(r"typed\s+clarification|возвращает[^.]*clarification", text, re.IGNORECASE):
        checks.append(
            CompiledCheck("response_status", "needs_clarification", "response status = needs_clarification")
        )
    if re.search(r"(?:точн\w*\s+gas_day|конкретн\w*\s+дат\w*|дат[ау]\s+(?:максимума|минимума))", text, re.IGNORECASE):
        checks.append(CompiledCheck("has_extremum", True, "result contains extremum date"))
    if re.search(r"вычисляет\s+100\s*\*|формул\w*\s+100\s*\*", text, re.IGNORECASE):
        checks.append(CompiledCheck("formula_operator", "percent_of", "formula operator = percent_of"))
    revision_match = re.search(r"revision\s+(\d+)", text)
    if revision_match:
        checks.append(
            CompiledCheck("revision", int(revision_match.group(1)), f"revision = {revision_match.group(1)}")
        )
    rule = catalog_rule(text)
    if rule is not None:
        checks.append(
            CompiledCheck(
                "catalog_rule",
                rule.rule_id,
                f"catalog semantic rule: {rule.rule_id}",
            )
        )
    return tuple(checks)


@dataclass(frozen=True)
class CheckResult:
    description: str
    passed: bool
    expected: Any
    actual: Any
    source_line: int
    catalog_expectation: bool = True


@dataclass
class ActionResult:
    turn_id: str
    kind: str
    message: str | None
    request_id: str | None = None
    http_status: int | None = None
    response_status: str | None = None
    result_status: str | None = None
    elapsed_ms: int | None = None
    checks: list[CheckResult] = field(default_factory=list)
    unsupported_expectations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict, repr=False)
    response: dict[str, Any] | None = field(default=None, repr=False)
    request_payload: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def failed(self) -> bool:
        return bool(self.error) or any(not check.passed for check in self.checks)

    @property
    def incomplete(self) -> bool:
        return bool(self.unsupported_expectations)


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    tags: tuple[str, ...]
    session_id: str | None = None
    status: str = "pending"
    actions: list[ActionResult] = field(default_factory=list)
    reason: str | None = None


@dataclass
class RunReport:
    run_id: str
    started_at: str
    finished_at: str
    base_url: str
    catalog: str
    execute_db: bool
    dry_run: bool
    health: dict[str, Any]
    scenarios: list[ScenarioResult]
    summary: dict[str, Any]


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: dict[str, Any]
    elapsed_ms: int


class AcceptanceHttpClient:
    def __init__(self, base_url: str, *, api_key: str | None = None, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def health(self) -> HttpResponse:
        return self._request("GET", "/api/v2/health")

    def create_session(self) -> HttpResponse:
        return self._request("POST", "/api/v2/chat/sessions", {})

    def chat(self, payload: dict[str, Any]) -> HttpResponse:
        return self._request("POST", "/api/v2/chat", payload)

    def delete_session(self, session_id: str) -> HttpResponse:
        return self._request("DELETE", f"/api/v2/chat/sessions/{session_id}")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> HttpResponse:
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.api_key:
            headers["x-api-key"] = self.api_key
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                body = json.loads(raw) if raw else {}
                status = response.status
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"detail": raw[:2000]}
            status = exc.code
        except (URLError, TimeoutError) as exc:
            raise ConnectionError(f"{method} {path} failed: {exc}") from exc
        return HttpResponse(status, body, int((time.perf_counter() - started) * 1000))


class AuditLogReader:
    """Incrementally indexes structured service events without exposing raw logs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._position = 0
        self._events: dict[str, dict[str, dict[str, Any]]] = {}

    def for_request(self, request_id: str) -> dict[str, Any]:
        self._refresh()
        return dict(self._events.get(request_id) or {})

    def _refresh(self) -> None:
        if not self.path.exists():
            return
        size = self.path.stat().st_size
        if size < self._position:
            self._position = 0
        with self.path.open("r", encoding="utf-8") as stream:
            stream.seek(self._position)
            for raw in stream:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                request_id = str(event.get("request_id") or "")
                event_name = str(event.get("event") or "")
                if request_id and event_name:
                    self._events.setdefault(request_id, {})[event_name] = event
            self._position = stream.tell()


class AcceptanceRunner:
    def __init__(
        self,
        client: AcceptanceHttpClient,
        *,
        execute_db: bool,
        restart_command: str | None = None,
        restart_timeout: float = 180.0,
        keep_sessions: bool = True,
        strict_coverage: bool = False,
        max_turns: int | None = None,
        audit_log: AuditLogReader | None = None,
    ):
        self.client = client
        self.execute_db = execute_db
        self.restart_command = restart_command
        self.restart_timeout = restart_timeout
        self.keep_sessions = keep_sessions
        self.strict_coverage = strict_coverage
        self.max_turns = max_turns
        self.audit_log = audit_log

    def run(self, catalog: Catalog, scenarios: Sequence[Scenario], *, dry_run: bool = False) -> RunReport:
        run_id = f"acceptance-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        started = datetime.now(UTC)
        health: dict[str, Any] = {"status": "not_checked"}
        results: list[ScenarioResult] = []

        if not dry_run:
            health_response = self.client.health()
            health = {
                "http_status": health_response.status_code,
                "status": health_response.body.get("status"),
                "elapsed_ms": health_response.elapsed_ms,
            }
            if health_response.status_code != 200 or health_response.body.get("status") != "ok":
                raise RuntimeError(f"backend health is not ready: {health}")

        for scenario in scenarios:
            if "contract-gap" in scenario.tags:
                results.append(
                    ScenarioResult(
                        scenario_id=scenario.scenario_id,
                        name=scenario.name,
                        tags=scenario.tags,
                        status="skipped",
                        reason="scenario is tagged @contract-gap",
                    )
                )
                continue
            if dry_run:
                results.append(self._dry_run_scenario(scenario))
            else:
                results.append(self._run_scenario(run_id, scenario))

        finished = datetime.now(UTC)
        return RunReport(
            run_id=run_id,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            base_url=self.client.base_url,
            catalog=catalog.path,
            execute_db=self.execute_db,
            dry_run=dry_run,
            health=health,
            scenarios=results,
            summary=_summarize(results),
        )

    def _dry_run_scenario(self, scenario: Scenario) -> ScenarioResult:
        result = ScenarioResult(scenario.scenario_id, scenario.name, scenario.tags, status="dry_run")
        actions = scenario.actions[: self.max_turns] if self.max_turns else scenario.actions
        for action in actions:
            action_result = ActionResult(action.turn_id, action.kind, action.message)
            for expectation in action.expectations:
                if action.kind == "concurrent" and "revision_conflict" in expectation.text:
                    action_result.checks.extend([
                        CheckResult(
                            "exactly one concurrent request commits",
                            True,
                            [200, 409],
                            "not executed",
                            expectation.line,
                        ),
                        CheckResult(
                            "losing concurrent request is rejected as revision_conflict",
                            True,
                            ["revision_conflict"],
                            "not executed",
                            expectation.line,
                        ),
                    ])
                    continue
                if action.kind == "replay" and _is_replay_expectation(expectation.text):
                    action_result.checks.extend([
                        CheckResult(
                            "idempotent replay returns the stored response",
                            True,
                            "identical response",
                            "not executed",
                            expectation.line,
                        ),
                        CheckResult(
                            "idempotent replay does not increment revision",
                            True,
                            "unchanged revision",
                            "not executed",
                            expectation.line,
                        ),
                    ])
                    continue
                checks = compile_expectation(expectation)
                if not checks:
                    action_result.unsupported_expectations.append(
                        {"line": expectation.line, "text": expectation.text}
                    )
                else:
                    action_result.checks.extend(
                        CheckResult(check.description, True, check.expected, "not executed", expectation.line)
                        for check in checks
                    )
            if action.kind == "restart":
                action_result.checks.append(
                    CheckResult(
                        "restart action is supported when --restart-command is supplied",
                        True,
                        "restart capability",
                        "not executed",
                        action.line,
                        False,
                    )
                )
            result.actions.append(action_result)
        return result

    def _run_scenario(self, run_id: str, scenario: Scenario) -> ScenarioResult:
        result = ScenarioResult(scenario.scenario_id, scenario.name, scenario.tags)
        created = self.client.create_session()
        if created.status_code != 201:
            result.status = "failed"
            result.reason = f"session creation returned HTTP {created.status_code}"
            return result
        session_id = str(created.body["session"]["session_id"])
        revision = int(created.body["session"]["revision"])
        result.session_id = session_id
        previous: dict[str, ActionResult] = {}
        actions = scenario.actions[: self.max_turns] if self.max_turns else scenario.actions

        for action in actions:
            # A semantic assertion failure is evidence, not a transport break:
            # continue the same committed session to expose downstream defects.
            # Only an action that could not be executed blocks dependent turns.
            if any(item.error for item in result.actions):
                result.actions.append(
                    ActionResult(
                        action.turn_id,
                        action.kind,
                        action.message,
                        error="blocked by an earlier failed turn",
                    )
                )
                continue
            action_result = self._execute_action(run_id, scenario, action, session_id, revision, previous)
            result.actions.append(action_result)
            previous[action.turn_id] = action_result
            if action_result.response and isinstance(action_result.response.get("session"), dict):
                revision = int(action_result.response["session"]["revision"])

        if any(action.failed for action in result.actions):
            result.status = "failed"
        elif any(action.incomplete for action in result.actions):
            result.status = "incomplete"
        else:
            result.status = "passed"

        if not self.keep_sessions:
            try:
                self.client.delete_session(session_id)
            except ConnectionError:
                pass
        return result

    def _execute_action(
        self,
        run_id: str,
        scenario: Scenario,
        action: Action,
        session_id: str,
        revision: int,
        previous: dict[str, ActionResult],
    ) -> ActionResult:
        if action.kind == "restart":
            return self._restart(action)
        if action.kind == "concurrent":
            return self._execute_concurrent(run_id, scenario, action, session_id, revision)

        request_id = f"{run_id}-{scenario.scenario_id.lower()}-{action.turn_id.lower()}"
        payload: dict[str, Any]
        if action.kind == "replay":
            source = previous.get(action.source_turn_id or "")
            if source is None or source.request_payload is None:
                return ActionResult(action.turn_id, action.kind, None, error="replay source is unavailable")
            payload = dict(source.request_payload)
            request_id = str(payload["request_id"])
        else:
            message = action.message or action.option
            if not message:
                return ActionResult(action.turn_id, action.kind, None, error="turn message is empty")
            payload = {
                "session_id": session_id,
                "expected_revision": revision,
                "message": message,
                "execute_db": self.execute_db,
                "request_id": request_id,
            }
            if action.kind == "clarification":
                pending = _pending_clarification(previous)
                if pending is None:
                    return ActionResult(action.turn_id, action.kind, message, error="pending clarification is unavailable")
                question = _select_question(pending, action.option or "")
                if question is None:
                    return ActionResult(action.turn_id, action.kind, message, error="clarification option is unavailable")
                payload["clarification"] = {
                    "source_turn_id": pending["turn_id"],
                    "clarification_id": question["clarification_id"],
                    "selected_option": action.option,
                }

        try:
            response = self.client.chat(payload)
        except ConnectionError as exc:
            return ActionResult(action.turn_id, action.kind, action.message, request_id=request_id, error=str(exc))

        body = response.body
        action_result = ActionResult(
            turn_id=action.turn_id,
            kind=action.kind,
            message=action.message or action.option,
            request_id=request_id,
            http_status=response.status_code,
            response_status=str(body.get("status") or "") or None,
            result_status=str((body.get("result") or {}).get("status") or "") or None,
            elapsed_ms=response.elapsed_ms,
            response=body,
            request_payload=payload,
            snapshot=_snapshot(body),
            audit=(
                self.audit_log.for_request(request_id)
                if self.audit_log is not None
                else {}
            ),
        )
        action_result.checks.extend(_base_checks(body, response.status_code, action.line))
        if action.kind == "replay":
            source = previous.get(action.source_turn_id or "")
            same_response = bool(source and source.response == body)
            same_revision = bool(
                source
                and source.response
                and (source.response.get("session") or {}).get("revision")
                == (body.get("session") or {}).get("revision")
            )
            replay_expectations = [
                item for item in action.expectations if _is_replay_expectation(item.text)
            ]
            source_line = replay_expectations[0].line if replay_expectations else action.line
            action_result.checks.extend([
                CheckResult(
                    "idempotent replay returns the stored response",
                    same_response,
                    "identical response",
                    "identical response" if same_response else "response changed",
                    source_line,
                    bool(replay_expectations),
                ),
                CheckResult(
                    "idempotent replay does not increment revision",
                    same_revision,
                    "unchanged revision",
                    "unchanged revision" if same_revision else "revision changed",
                    source_line,
                    bool(replay_expectations),
                ),
            ])
        for expectation in action.expectations:
            if action.kind == "replay" and _is_replay_expectation(expectation.text):
                continue
            compiled = compile_expectation(expectation)
            if not compiled:
                action_result.unsupported_expectations.append(
                    {"line": expectation.line, "text": expectation.text}
                )
                continue
            action_result.checks.extend(
                evaluate_check(
                    check,
                    body,
                    expectation.line,
                    history=previous,
                    audit=action_result.audit,
                )
                for check in compiled
            )
        if response.status_code >= 400:
            action_result.error = _http_error(body, response.status_code)
        return action_result

    def _execute_concurrent(
        self,
        run_id: str,
        scenario: Scenario,
        action: Action,
        session_id: str,
        revision: int,
    ) -> ActionResult:
        if not action.message:
            return ActionResult(
                action.turn_id,
                action.kind,
                None,
                error="concurrent action requires explicit query text",
            )
        expected_revision = action.expected_revision if action.expected_revision is not None else revision
        payloads = [
            {
                "session_id": session_id,
                "expected_revision": expected_revision,
                "message": action.message,
                "execute_db": self.execute_db,
                "request_id": f"{run_id}-{scenario.scenario_id.lower()}-{action.turn_id.lower()}-{suffix}",
            }
            for suffix in ("a", "b")
        ]
        started = time.perf_counter()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(self.client.chat, payloads))
        except ConnectionError as exc:
            return ActionResult(action.turn_id, action.kind, action.message, error=str(exc))

        winners = [index for index, response in enumerate(responses) if response.status_code == 200]
        losers = [index for index, response in enumerate(responses) if response.status_code == 409]
        winner_index = winners[0] if winners else 0
        winner = responses[winner_index]
        loser_codes = [
            ((responses[index].body.get("detail") or {}).get("code"))
            for index in losers
        ]
        exactly_one = len(winners) == 1 and len(losers) == 1
        revision_conflict = loser_codes == ["revision_conflict"]
        expectation = next(
            (item for item in action.expectations if "revision_conflict" in item.text),
            None,
        )
        result = ActionResult(
            turn_id=action.turn_id,
            kind=action.kind,
            message=action.message,
            request_id=str(payloads[winner_index]["request_id"]),
            http_status=winner.status_code,
            response_status=str(winner.body.get("status") or "") or None,
            result_status=str((winner.body.get("result") or {}).get("status") or "") or None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            response=winner.body,
            request_payload=payloads[winner_index],
            snapshot={
                **_snapshot(winner.body),
                "concurrent_http_statuses": [item.status_code for item in responses],
                "loser_codes": loser_codes,
            },
            audit=(
                self.audit_log.for_request(str(payloads[winner_index]["request_id"]))
                if self.audit_log is not None
                else {}
            ),
        )
        result.checks.extend(_base_checks(winner.body, winner.status_code, action.line))
        source_line = expectation.line if expectation else action.line
        result.checks.extend([
            CheckResult(
                "exactly one concurrent request commits",
                exactly_one,
                [200, 409],
                sorted(item.status_code for item in responses),
                source_line,
                expectation is not None,
            ),
            CheckResult(
                "losing concurrent request is rejected as revision_conflict",
                revision_conflict,
                ["revision_conflict"],
                loser_codes,
                source_line,
                expectation is not None,
            ),
        ])
        for item in action.expectations:
            if item is expectation:
                continue
            compiled = compile_expectation(item)
            if compiled:
                result.checks.extend(
                    evaluate_check(check, winner.body, item.line, audit=result.audit)
                    for check in compiled
                )
            else:
                result.unsupported_expectations.append({"line": item.line, "text": item.text})
        if not exactly_one or not revision_conflict:
            result.error = "concurrent revision contract failed"
        return result

    def _restart(self, action: Action) -> ActionResult:
        result = ActionResult(action.turn_id, action.kind, None)
        if not self.restart_command:
            result.unsupported_expectations.append(
                {"line": action.line, "text": "restart requires --restart-command"}
            )
            return result
        started = time.perf_counter()
        completed = subprocess.run(self.restart_command, shell=True, check=False)
        if completed.returncode != 0:
            result.error = f"restart command exited with {completed.returncode}"
            return result
        deadline = time.monotonic() + self.restart_timeout
        while time.monotonic() < deadline:
            try:
                health = self.client.health()
                if health.status_code == 200 and health.body.get("status") == "ok":
                    result.http_status = 200
                    result.response_status = "ok"
                    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
                    return result
            except ConnectionError:
                pass
            time.sleep(1)
        result.error = "backend did not become ready before restart timeout"
        return result


def evaluate_check(
    check: CompiledCheck,
    body: dict[str, Any],
    source_line: int,
    *,
    history: Mapping[str, ActionResult] | None = None,
    audit: dict[str, Any] | None = None,
) -> CheckResult:
    intent = _intent(body)
    operands = list(intent.get("operands") or [])
    actual: Any
    passed = False

    if check.kind == "operation":
        actual = intent.get("operation") or (body.get("result") or {}).get("operation")
        passed = actual == check.expected
    elif check.kind == "metric":
        actual = [operand.get("metric") for operand in operands]
        passed = check.expected in actual
    elif check.kind == "aggregate":
        actual = [operand.get("aggregate_type") for operand in operands]
        passed = check.expected in actual
    elif check.kind == "grain":
        actual = intent.get("grain") or (intent.get("ranking") or {}).get("grain")
        passed = actual == check.expected
    elif check.kind == "ranking_direction":
        actual = (intent.get("ranking") or {}).get("direction")
        passed = actual == check.expected
    elif check.kind == "period":
        actual = _periods(intent)
        passed = list(check.expected) in actual
    elif check.kind == "operand_count":
        actual = len(operands)
        passed = actual == check.expected
    elif check.kind == "result_status":
        actual = (body.get("result") or {}).get("status")
        passed = actual == check.expected
    elif check.kind == "response_status":
        actual = body.get("status")
        passed = actual == check.expected
    elif check.kind == "has_extremum":
        facts = (body.get("result") or {}).get("facts") or []
        actual = [fact.get("extremum_at") for fact in facts if fact.get("extremum_at")]
        passed = bool(actual)
    elif check.kind == "formula_operator":
        formula = intent.get("formula") or {}
        actual = formula.get("operator")
        passed = actual == check.expected
    elif check.kind == "revision":
        actual = (body.get("session") or {}).get("revision")
        passed = actual == check.expected
    elif check.kind == "entity":
        expected = check.expected
        entities = _entities(operands)
        actual = [{"role": item.get("role"), "label": _entity_label(item)} for item in entities]
        passed = any(
            _role_matches(
                expected["role"],
                str(item.get("role") or ""),
                _entity_type(item),
                [str(operand.get("metric") or "") for operand in operands],
            )
            and _label_matches(expected["label"], _entity_label(item))
            for item in entities
        )
    elif check.kind == "catalog_rule":
        rule_history = {
            turn_id: {
                "response": result.response or {},
                "snapshot": result.snapshot,
                "audit": result.audit,
            }
            for turn_id, result in (history or {}).items()
        }
        outcome = evaluate_catalog_rule(
            str(check.expected),
            RuleContext(current=body, history=rule_history, audit=audit or {}),
        )
        actual = outcome.actual
        passed = outcome.passed
    else:
        actual = "unsupported check"
    return CheckResult(check.description, passed, check.expected, actual, source_line)


def _base_checks(body: dict[str, Any], status_code: int, line: int) -> list[CheckResult]:
    result = body.get("result") or {}
    public_result = json.dumps(result, ensure_ascii=False)
    return [
        CheckResult("HTTP request succeeded", status_code < 400, "< 400", status_code, line, False),
        CheckResult(
            "result has no execution error",
            result.get("status") != "error",
            "status != error",
            result.get("status"),
            line,
            False,
        ),
        CheckResult(
            "public result hides balance_id/article_id",
            "balance_id" not in public_result and "article_id" not in public_result,
            "IDs absent",
            "IDs present" if ("balance_id" in public_result or "article_id" in public_result) else "IDs absent",
            line,
            False,
        ),
        CheckResult(
            "public result does not expose млн м3",
            not bool(re.search(r"млн\.?\s*м[³3]", public_result, re.IGNORECASE)),
            "тыс. м3",
            "млн м3 found" if re.search(r"млн\.?\s*м[³3]", public_result, re.IGNORECASE) else "ok",
            line,
            False,
        ),
    ]


def _intent(body: dict[str, Any]) -> dict[str, Any]:
    context = body.get("context") or {}
    active = context.get("active") or {}
    return active.get("intent") or {}


def _periods(intent: dict[str, Any]) -> list[list[str]]:
    values: list[list[str]] = []
    candidates: list[dict[str, Any]] = list(intent.get("periods") or [])
    for operand in intent.get("operands") or []:
        candidates.extend(operand.get("periods") or [])
    for period in candidates:
        pair = [str(period.get("date_from")), str(period.get("date_to"))]
        if pair not in values:
            values.append(pair)
    return values


def _entities(operands: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entity for operand in operands for entity in (operand.get("entities") or [])]


def _entity_label(entity: dict[str, Any]) -> str:
    nested = entity.get("entity") if isinstance(entity.get("entity"), dict) else entity
    return str(nested.get("display_name") or nested.get("label") or "")


def _role_matches(expected: str, actual: str, entity_type: str, metrics: list[str]) -> bool:
    if expected == "geo":
        return actual in {"geo", "destination", "source"}
    if expected == "source":
        return actual == "source" or (
            entity_type == "article" and "incoming" in metrics
        ) or (
            entity_type == "balance" and "distribution" in metrics
        )
    if expected == "destination":
        return actual == "destination" or (
            entity_type == "balance" and "incoming" in metrics
        ) or (
            entity_type == "article" and "distribution" in metrics
        )
    return expected == actual


def _entity_type(entity: dict[str, Any]) -> str:
    nested = entity.get("entity") if isinstance(entity.get("entity"), dict) else entity
    return str(nested.get("entity_type") or "")


def _label_matches(expected: str, actual: str) -> bool:
    def normalize(value: str) -> str:
        value = value.lower().replace("ё", "е")
        value = re.sub(r"\b(?:гп|тг)\b", " ", value)
        return re.sub(r"[^a-zа-я0-9]+", " ", value).strip()

    left, right = normalize(expected), normalize(actual)
    return bool(left and right and (left in right or right in left))


def _pending_clarification(previous: dict[str, ActionResult]) -> dict[str, Any] | None:
    for result in reversed(list(previous.values())):
        if not result.response:
            continue
        pending = (result.response.get("context") or {}).get("pending_clarification")
        if isinstance(pending, dict):
            return pending
    return None


def _select_question(pending: dict[str, Any], option: str) -> dict[str, Any] | None:
    for question in pending.get("questions") or []:
        if option in (question.get("options") or []):
            return question
    return None


def _is_replay_expectation(text: str) -> bool:
    return bool(re.search(r"(?:idempotent|сохран[её]нн\w*)\s+replay", text, re.IGNORECASE))


def _snapshot(body: dict[str, Any]) -> dict[str, Any]:
    intent = _intent(body)
    result = body.get("result") or {}
    return {
        "revision": (body.get("session") or {}).get("revision"),
        "operation": intent.get("operation") or result.get("operation"),
        "metrics": [operand.get("metric") for operand in (intent.get("operands") or [])],
        "aggregates": [operand.get("aggregate_type") for operand in (intent.get("operands") or [])],
        "entities": [
            {"role": entity.get("role"), "label": _entity_label(entity)}
            for entity in _entities(intent.get("operands") or [])
        ],
        "periods": _periods(intent),
        "grain": intent.get("grain"),
        "fact_count": len(result.get("facts") or []),
        "summary_title": (result.get("summary") or {}).get("title"),
    }


def _http_error(body: dict[str, Any], status: int) -> str:
    detail = body.get("detail")
    if isinstance(detail, dict):
        return f"HTTP {status}: {detail.get('code') or detail.get('message') or detail}"
    return f"HTTP {status}: {detail or body}"


def _summarize(results: Sequence[ScenarioResult]) -> dict[str, Any]:
    statuses = {name: sum(item.status == name for item in results) for name in (
        "passed", "incomplete", "failed", "skipped", "dry_run"
    )}
    actions = [action for scenario in results for action in scenario.actions]
    checks = [check for action in actions for check in action.checks]
    unsupported = sum(len(action.unsupported_expectations) for action in actions)
    automated_lines = len({
        (scenario.scenario_id, check.source_line)
        for scenario in results
        for action in scenario.actions
        for check in action.checks
        if check.catalog_expectation
    })
    unsupported_lines = len({(scenario.scenario_id, item["line"]) for scenario in results for action in scenario.actions for item in action.unsupported_expectations})
    line_total = automated_lines + unsupported_lines
    executed = statuses["passed"] + statuses["incomplete"] + statuses["failed"]
    semantically_accepted = statuses["passed"]
    return {
        "scenario_count": len(results),
        **statuses,
        "executed": executed,
        "action_count": len(actions),
        "check_count": len(checks),
        "check_passed": sum(check.passed for check in checks),
        "check_failed": sum(not check.passed for check in checks),
        "unsupported_expectation_count": unsupported,
        "automated_expectation_lines": automated_lines,
        "expectation_line_count": line_total,
        "coverage_rate": round(100 * automated_lines / line_total, 2) if line_total else 100.0,
        "semantic_acceptance_rate": round(100 * semantically_accepted / executed, 2) if executed else None,
    }


def report_to_dict(report: RunReport) -> dict[str, Any]:
    value = asdict(report)
    for scenario in value["scenarios"]:
        for action in scenario["actions"]:
            action.pop("audit", None)
            action.pop("response", None)
            action.pop("request_payload", None)
    return value


def render_markdown(report: RunReport) -> str:
    summary = report.summary
    lines = [
        "# Context Chat acceptance report",
        "",
        f"- Run ID: `{report.run_id}`",
        f"- Started: `{report.started_at}`",
        f"- Backend: `{report.base_url}`",
        f"- DB execution: `{str(report.execute_db).lower()}`",
        f"- Catalog: `{report.catalog}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Scenarios | {summary['scenario_count']} |",
        f"| Passed | {summary['passed']} |",
        f"| Incomplete | {summary['incomplete']} |",
        f"| Failed | {summary['failed']} |",
        f"| Skipped | {summary['skipped']} |",
        f"| Semantic acceptance | {_percent(summary['semantic_acceptance_rate'])} |",
        f"| Automated expectation coverage | {_percent(summary['coverage_rate'])} |",
        f"| Checks | {summary['check_passed']}/{summary['check_count']} |",
        "",
        "## Scenarios",
        "",
    ]
    for scenario in report.scenarios:
        lines.extend([
            f"### {scenario.scenario_id}: {scenario.name}",
            "",
            f"Status: **{scenario.status.upper()}**" + (f" — {scenario.reason}" if scenario.reason else ""),
            "",
        ])
        if scenario.session_id:
            lines.extend([f"Session: `{scenario.session_id}`", ""])
        if not scenario.actions:
            continue
        lines.extend(["| Turn | HTTP | Result | Checks | Gaps | Time |", "|---|---:|---|---:|---:|---:|"])
        for action in scenario.actions:
            passed = sum(check.passed for check in action.checks)
            lines.append(
                f"| {action.turn_id} | {action.http_status or '—'} | "
                f"{action.result_status or action.response_status or action.error or '—'} | "
                f"{passed}/{len(action.checks)} | {len(action.unsupported_expectations)} | "
                f"{action.elapsed_ms if action.elapsed_ms is not None else '—'} ms |"
            )
        lines.append("")
        for action in scenario.actions:
            failures = [check for check in action.checks if not check.passed]
            if failures:
                lines.append(f"Failures `{action.turn_id}`:")
                lines.append("")
                for check in failures:
                    lines.append(
                        f"- line {check.source_line}: {check.description}; expected `{check.expected}`, actual `{check.actual}`"
                    )
                lines.append("")
            if action.unsupported_expectations:
                lines.append(f"Automation gaps `{action.turn_id}`:")
                lines.append("")
                for gap in action.unsupported_expectations:
                    lines.append(f"- line {gap['line']}: {gap['text']}")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _percent(value: Any) -> str:
    return "—" if value is None else f"{value:.2f}%"


def select_scenarios(
    catalog: Catalog,
    *,
    scenario_ids: Sequence[str] = (),
    tags: Sequence[str] = (),
    include_contract_gaps: bool = False,
) -> list[Scenario]:
    selected = list(catalog.scenarios)
    if not include_contract_gaps:
        selected = [item for item in selected if "current-contract" in item.tags]
    if scenario_ids:
        requested = set(scenario_ids)
        unknown = requested - {item.scenario_id for item in catalog.scenarios}
        if unknown:
            raise CatalogError(f"unknown scenario IDs: {', '.join(sorted(unknown))}")
        selected = [item for item in selected if item.scenario_id in requested]
    if tags:
        selected = [item for item in selected if all(tag in item.tags for tag in tags)]
    return selected


def _write_reports(report: RunReport, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stem = report.run_id
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    json_path.write_text(
        json.dumps(report_to_dict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Context Chat business acceptance scenarios")
    parser.add_argument(
        "--catalog",
        default="acceptance/context_chat_business_scenarios.feature",
        help="Path to the human-readable business scenario catalog",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8790")
    parser.add_argument("--api-key-env", default="AI_BALANCES_API_KEY")
    parser.add_argument("--scenario", action="append", default=[], help="Run a BC-NN scenario; repeatable")
    parser.add_argument("--tag", action="append", default=[], help="Require a catalog tag; repeatable")
    parser.add_argument("--include-contract-gaps", action="store_true")
    parser.add_argument("--execute-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Parse and measure automation coverage without HTTP")
    parser.add_argument("--max-turns", type=int, help="Limit actions per scenario for a smoke run")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--restart-command", help="Explicit command used only by restart scenarios")
    parser.add_argument("--restart-timeout", type=float, default=180.0)
    parser.add_argument("--delete-sessions", action="store_true")
    parser.add_argument("--strict-coverage", action="store_true", help="Fail if an expectation is not automated")
    parser.add_argument("--report-dir", default="reports/acceptance")
    parser.add_argument(
        "--log-file",
        default="logs/balance_chat.jsonl",
        help="Structured backend log used for handles, fingerprints and no-repeat evidence",
    )
    parser.add_argument("--list", action="store_true", help="List selected scenarios and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        catalog = parse_catalog(args.catalog)
        selected = select_scenarios(
            catalog,
            scenario_ids=args.scenario,
            tags=args.tag,
            include_contract_gaps=args.include_contract_gaps,
        )
    except (OSError, CatalogError) as exc:
        print(f"acceptance catalog error: {exc}", file=sys.stderr)
        return 2

    if args.list:
        for scenario in selected:
            print(f"{scenario.scenario_id}\t{','.join(scenario.tags)}\t{scenario.name}")
        return 0
    if not selected:
        print("no scenarios selected", file=sys.stderr)
        return 2

    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    client = AcceptanceHttpClient(args.base_url, api_key=api_key, timeout=args.timeout)
    runner = AcceptanceRunner(
        client,
        execute_db=args.execute_db,
        restart_command=args.restart_command,
        restart_timeout=args.restart_timeout,
        keep_sessions=not args.delete_sessions,
        strict_coverage=args.strict_coverage,
        max_turns=args.max_turns,
        audit_log=(AuditLogReader(args.log_file) if args.log_file else None),
    )
    try:
        report = runner.run(catalog, selected, dry_run=args.dry_run)
    except (ConnectionError, RuntimeError) as exc:
        print(f"acceptance run failed: {exc}", file=sys.stderr)
        return 2
    json_path, markdown_path = _write_reports(report, Path(args.report_dir))
    print(render_markdown(report))
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    if report.summary["failed"]:
        return 1
    if args.strict_coverage and (report.summary["incomplete"] or report.summary["unsupported_expectation_count"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
