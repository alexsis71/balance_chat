from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import (
    AnalysisIntent,
    ContextContractV2,
    ContextMutation,
    ContextScope,
    EntityMemoryEntry,
    FieldMutation,
    MutationAction,
    PendingClarification,
    ResultReference,
    TransitionOutcome,
    TurnReference,
    utc_now,
)


class ContextReductionError(ValueError):
    pass


_LIST_FIELDS = {"operands", "periods", "grouping"}
_REFERENCE_ROOTS = {
    "active_dialog_scope",
    "last_attempted_scope",
    "last_successful_scope",
}


def _reference_value(state: ContextContractV2, path: str) -> Any:
    parts = path.split(".")
    if len(parts) != 3 or parts[0] not in _REFERENCE_ROOTS or parts[1] != "intent":
        raise ContextReductionError(f"unsupported context reference: {path}")
    scope = getattr(state, parts[0])
    if scope is None:
        raise ContextReductionError(f"context reference is empty: {path}")
    if parts[2] not in AnalysisIntent.model_fields:
        raise ContextReductionError(f"unknown intent field in reference: {path}")
    return deepcopy(getattr(scope.intent, parts[2]))


def _as_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContextReductionError(f"{field} mutation requires a list value")
    return value


def _apply_field(
    state: ContextContractV2,
    payload: dict[str, Any],
    field: str,
    mutation: FieldMutation,
) -> None:
    action = mutation.action
    current = payload.get(field)
    if action == MutationAction.KEEP:
        return
    if action == MutationAction.CLEAR:
        payload[field] = [] if field in _LIST_FIELDS else None
        return
    if action == MutationAction.REFERENCE:
        payload[field] = _reference_value(state, mutation.value)
        return
    if action == MutationAction.SET:
        payload[field] = deepcopy(mutation.value)
        return
    if field not in _LIST_FIELDS:
        raise ContextReductionError(f"{action.value} is only valid for list fields")
    incoming = _as_list(mutation.value, field)
    existing = _as_list(current or [], field)
    if action == MutationAction.ADD:
        for item in incoming:
            if item not in existing:
                existing.append(deepcopy(item))
        payload[field] = existing
        return
    if action == MutationAction.REMOVE:
        payload[field] = [item for item in existing if item not in incoming]
        return
    raise ContextReductionError(f"unsupported mutation action: {action}")


def reduce_intent(state: ContextContractV2, mutation: ContextMutation) -> AnalysisIntent:
    if mutation.replace_intent is not None:
        payload = mutation.replace_intent.model_dump(mode="python")
    elif state.active_dialog_scope is not None:
        payload = state.active_dialog_scope.intent.model_dump(mode="python")
    else:
        raise ContextReductionError("initial transition requires replace_intent")

    for field, field_mutation in mutation.patch:
        if field_mutation is not None:
            _apply_field(state, payload, field, field_mutation)
    try:
        return AnalysisIntent.model_validate(payload)
    except Exception as exc:
        raise ContextReductionError(f"reduced intent is invalid: {exc}") from exc


def _update_entity_memory(
    state: ContextContractV2,
    intent: AnalysisIntent,
    turn_id: str,
) -> list[EntityMemoryEntry]:
    memory = [item.model_copy(deep=True) for item in state.entity_memory]
    index = {
        (item.role, item.entity.entity_type, item.entity.entity_id): item
        for item in memory
    }
    for operand in intent.operands:
        for reference in operand.entities:
            key = (
                reference.role,
                reference.entity.entity_type,
                reference.entity.entity_id,
            )
            existing = index.get(key)
            if existing:
                existing.last_turn_id = turn_id
                existing.mention_count += 1
            else:
                entry = EntityMemoryEntry(
                    role=reference.role,
                    entity=reference.entity,
                    first_turn_id=turn_id,
                    last_turn_id=turn_id,
                )
                memory.append(entry)
                index[key] = entry
    return memory


def apply_context_transition(
    state: ContextContractV2,
    mutation: ContextMutation,
    outcome: TransitionOutcome,
    *,
    result: ResultReference | None = None,
    clarification_questions: list[dict[str, Any]] | None = None,
    recent_turn_limit: int = 20,
    result_limit: int = 50,
) -> ContextContractV2:
    intent = reduce_intent(state, mutation)
    now = utc_now()
    scope = ContextScope(intent=intent, turn_id=mutation.turn_id, updated_at=now)
    updated = state.model_copy(deep=True)
    updated.revision += 1
    updated.updated_at = now
    updated.active_dialog_scope = scope
    updated.last_attempted_scope = scope.model_copy(deep=True)
    if outcome == TransitionOutcome.SUCCESS:
        updated.last_successful_scope = scope.model_copy(deep=True)
    updated.entity_memory = _update_entity_memory(updated, intent, mutation.turn_id)
    updated.recent_turns.append(
        TurnReference(
            turn_id=mutation.turn_id,
            user_message=mutation.user_message,
            normalized_message=mutation.normalized_message,
            outcome=outcome,
        )
    )
    updated.recent_turns = updated.recent_turns[-recent_turn_limit:]
    if result is not None:
        if result.turn_id != mutation.turn_id or result.status != outcome:
            raise ContextReductionError("result reference must match transition turn and outcome")
        updated.result_references.append(result)
        updated.result_references = updated.result_references[-result_limit:]
    if outcome == TransitionOutcome.CLARIFICATION:
        if not clarification_questions:
            raise ContextReductionError("clarification outcome requires questions")
        updated.pending_clarification = PendingClarification(
            turn_id=mutation.turn_id,
            questions=clarification_questions,
        )
    else:
        updated.pending_clarification = None
    return ContextContractV2.model_validate(updated)
