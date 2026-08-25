from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from ..contracts import AnalysisIntent, ContextContractV2, ContractModel


class SemanticShadowContext(ContractModel):
    current_active_state: dict[str, Any]
    recent_semantic_turns: list[dict[str, Any]] = Field(max_length=4)
    recent_addressable_results: list[dict[str, Any]] = Field(max_length=4)
    current_user_message: str = Field(min_length=1, max_length=4000)
    addressable_result_count: int = Field(default=0, ge=0, le=4)
    directed_relation_count: int = Field(default=0, ge=0, le=4)
    direction_ambiguous: bool = False

    @model_validator(mode="before")
    @classmethod
    def derive_context_invariants(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        results = []
        for raw_result in data.get("recent_addressable_results") or []:
            if not isinstance(raw_result, dict):
                results.append(raw_result)
                continue
            result = dict(raw_result)
            recency = int(result.get("recency") or 0)
            if recency > 0:
                result.setdefault("relative_position", _relative_position(recency))
            results.append(result)
        data["recent_addressable_results"] = results
        data.setdefault("addressable_result_count", min(len(results), 4))
        relation_count, ambiguous = _directed_relation_summary(
            data.get("current_active_state") or {}
        )
        data.setdefault("directed_relation_count", relation_count)
        data.setdefault("direction_ambiguous", ambiguous)
        return data


def build_semantic_shadow_context(
    state: ContextContractV2,
    message: str,
    *,
    max_turns: int = 4,
    max_results: int = 4,
) -> SemanticShadowContext:
    if state.active_dialog_scope is None:
        raise ValueError("semantic shadow context requires active analytical state")
    clean_message = str(message).strip()
    if not clean_message:
        raise ValueError("semantic shadow context requires non-empty message")
    turn_limit = max(1, min(int(max_turns), 4))
    result_limit = max(1, min(int(max_results), 4))
    frames = list(state.conversation_window[-turn_limit:])
    recent_turns = [
        {
            "recency": len(frames) - index,
            "user": frame.user_message,
            "normalized": frame.normalized_message,
            "outcome": frame.outcome.value,
            "state": _intent_summary(frame.intent),
            "result": (
                {
                    "row_count": frame.result.row_count,
                    "fact_count": len(frame.result.facts),
                }
                if frame.result is not None
                else None
            ),
        }
        for index, frame in enumerate(frames)
    ]
    result_frames = [frame for frame in state.conversation_window if frame.result is not None]
    selected_results = result_frames[-result_limit:]
    recent_results = [
        {
            "recency": len(selected_results) - index,
            "relative_position": _relative_position(
                len(selected_results) - index
            ),
            "state": _intent_summary(frame.intent),
            "row_count": frame.result.row_count,
            "fact_count": len(frame.result.facts),
        }
        for index, frame in enumerate(selected_results)
    ]
    return SemanticShadowContext(
        current_active_state=_intent_summary(state.active_dialog_scope.intent),
        recent_semantic_turns=recent_turns,
        recent_addressable_results=recent_results,
        current_user_message=clean_message,
    )


def _relative_position(recency: int) -> str:
    if recency == 1:
        return "most_recent"
    if recency == 2:
        return "previous"
    return "older"


def _directed_relation_summary(state: dict[str, Any]) -> tuple[int, bool]:
    relations = 0
    ambiguous = False
    for operand in state.get("operands") or []:
        if not isinstance(operand, dict):
            continue
        entities = operand.get("entities") or []
        roles = [
            item.get("role")
            for item in entities
            if isinstance(item, dict) and item.get("role")
        ]
        sources = roles.count("source")
        destinations = roles.count("destination")
        balances = roles.count("balance")
        articles = roles.count("article")
        explicit_pair = sources == 1 and destinations == 1
        full_balance_relation = (
            balances == 1
            and articles == 1
            and (sources + destinations) == 1
            and operand.get("metric") in {"incoming", "distribution"}
        )
        if explicit_pair or full_balance_relation:
            relations += 1
        elif sources or destinations:
            ambiguous = True
        if sources > 1 or destinations > 1:
            ambiguous = True
    if relations > 1:
        ambiguous = True
    return relations, ambiguous


def _intent_summary(intent: AnalysisIntent) -> dict[str, Any]:
    return {
        "operation": intent.operation.value,
        "operands": [
            {
                "metric": operand.metric,
                "aggregate_type": operand.aggregate_type,
                "unit": operand.unit,
                "entities": [
                    {
                        "role": entity.role,
                        "type": entity.entity.entity_type,
                        "label": entity.entity.display_name,
                    }
                    for entity in operand.entities
                ],
                "periods": [
                    {
                        "date_from": period.date_from.isoformat(),
                        "date_to": period.date_to.isoformat(),
                        "label": period.label,
                    }
                    for period in operand.periods
                ],
            }
            for operand in intent.operands
        ],
        "periods": [
            {
                "date_from": period.date_from.isoformat(),
                "date_to": period.date_to.isoformat(),
                "label": period.label,
            }
            for period in intent.periods
        ],
        "grouping": [
            {
                "dimension": item.dimension,
                "aggregate_type": item.aggregate_type,
            }
            for item in intent.grouping
        ],
        "grain": intent.grain,
        "comparison": (
            {
                "delta_direction": intent.comparison.delta_direction,
                "percent_base": intent.comparison.percent_base,
            }
            if intent.comparison is not None
            else None
        ),
        "formula": (
            {"operator": intent.formula.operator}
            if intent.formula is not None
            else None
        ),
        "ranking": (
            {
                "direction": intent.ranking.direction,
                "grain": intent.ranking.grain,
                "bucket_aggregate": intent.ranking.bucket_aggregate,
                "limit": intent.ranking.limit,
                "return_dimension": intent.ranking.return_dimension,
            }
            if intent.ranking is not None
            else None
        ),
    }
