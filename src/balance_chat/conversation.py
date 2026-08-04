from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .contracts import (
    AnalysisIntent,
    ContextContractV2,
    ContextMutation,
    OperandEntityRef,
    PeriodRef,
    ResolvedEntityTag,
    ResolvedOperandSnapshot,
    ResolvedPeriodTag,
    ResolvedResultSnapshot,
    ResolvedTurnFrame,
    ResultReference,
    TransitionOutcome,
)


DEFAULT_WINDOW_SIZE = 7


def turn_handle(revision: int) -> str:
    return f"t{int(revision):04d}"


def operand_handle(turn: str, operand_id: str) -> str:
    return f"{turn}.o.{operand_id}"


def entity_handle(reference: OperandEntityRef) -> str:
    return "e_" + _digest(
        reference.role,
        reference.entity.entity_type,
        reference.entity.entity_id,
    )


def period_handle(period: PeriodRef) -> str:
    return "p_" + _digest(period.date_from.isoformat(), period.date_to.isoformat())


def build_turn_frame(
    *,
    revision: int,
    mutation: ContextMutation,
    intent: AnalysisIntent,
    outcome: TransitionOutcome,
    result: ResultReference | None,
) -> ResolvedTurnFrame:
    turn = turn_handle(revision)
    operands: list[ResolvedOperandSnapshot] = []
    entities: list[ResolvedEntityTag] = []
    periods: list[ResolvedPeriodTag] = []
    seen_periods: set[tuple[str, str]] = set()
    for operand in intent.operands:
        owner = operand_handle(turn, operand.operand_id)
        operands.append(ResolvedOperandSnapshot(handle=owner, operand=operand))
        entities.extend(
            ResolvedEntityTag(
                handle=entity_handle(reference),
                operand_handle=owner,
                role=reference.role,
                entity=reference.entity,
            )
            for reference in operand.entities
        )
        _append_period_tags(periods, seen_periods, owner, operand.periods)
    _append_period_tags(periods, seen_periods, turn, intent.periods)
    result_snapshot = None
    if result is not None:
        result_snapshot = ResolvedResultSnapshot(
            handle=f"r_{turn}",
            result_id=result.result_id,
            row_count=result.row_count,
            facts=[_bounded_fact(item) for item in result.facts[:24]],
        )
    return ResolvedTurnFrame(
        turn_handle=turn,
        turn_id=mutation.turn_id,
        revision=revision,
        user_message=mutation.user_message,
        normalized_message=mutation.normalized_message,
        assistant_summary=mutation.assistant_summary,
        outcome=outcome,
        intent=intent,
        operands=operands,
        entities=entities,
        periods=periods,
        result=result_snapshot,
    )


def materialize_window(
    state: ContextContractV2,
    *,
    limit: int = DEFAULT_WINDOW_SIZE,
) -> list[ResolvedTurnFrame]:
    """Return chronological authoritative frames, including an old-session bridge."""
    if state.conversation_window:
        return list(state.conversation_window[-limit:])
    scope = state.active_dialog_scope or state.last_successful_scope
    if scope is None:
        return []
    recent = next(
        (item for item in reversed(state.recent_turns) if item.turn_id == scope.turn_id),
        None,
    )
    result = next(
        (item for item in reversed(state.result_references) if item.turn_id == scope.turn_id),
        None,
    )
    mutation = ContextMutation(
        turn_id=scope.turn_id,
        user_message=recent.user_message if recent else "Восстановленный контекст",
        normalized_message=recent.normalized_message if recent else None,
        replace_intent=scope.intent,
    )
    return [
        build_turn_frame(
            revision=max(1, state.revision),
            mutation=mutation,
            intent=scope.intent,
            outcome=recent.outcome if recent else TransitionOutcome.SUCCESS,
            result=result,
        )
    ]


def model_window_payload(state: ContextContractV2, *, limit: int = DEFAULT_WINDOW_SIZE) -> list[dict[str, Any]]:
    return [frame.model_dump(mode="json") for frame in materialize_window(state, limit=limit)]


def handle_indexes(state: ContextContractV2) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    operands: dict[str, Any] = {}
    entities: dict[str, Any] = {}
    periods: dict[str, Any] = {}
    for frame in materialize_window(state):
        operands.update((item.handle, item.operand) for item in frame.operands)
        entities.update(
            (
                item.handle,
                OperandEntityRef(role=item.role, entity=item.entity),
            )
            for item in frame.entities
        )
        periods.update((item.handle, item.period) for item in frame.periods)
    return operands, entities, periods


def _append_period_tags(
    output: list[ResolvedPeriodTag],
    seen: set[tuple[str, str]],
    owner: str,
    values: Iterable[PeriodRef],
) -> None:
    for period in values:
        key = (owner, period_handle(period))
        if key in seen:
            continue
        seen.add(key)
        output.append(
            ResolvedPeriodTag(
                handle=key[1],
                owner_handle=owner,
                period=period,
            )
        )


def _bounded_fact(value: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    if len(encoded) <= 2000:
        return value
    return {"truncated": True, "sha256": hashlib.sha256(encoded.encode()).hexdigest()}


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
