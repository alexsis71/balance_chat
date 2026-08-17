from __future__ import annotations

from typing import Protocol

from .contracts import AnalysisIntent, ContextContractV2, ContextMutation
from .reducer import ContextReductionError, reduce_intent


class ExecutionAdapter(Protocol):
    def effective_intent(
        self,
        state: ContextContractV2,
        mutation: ContextMutation,
    ) -> AnalysisIntent: ...


class ReducerExecutionAdapter:
    def effective_intent(
        self,
        state: ContextContractV2,
        mutation: ContextMutation,
    ) -> AnalysisIntent:
        if mutation.replace_intent is None and not any(
            field_mutation is not None for _, field_mutation in mutation.patch
        ):
            raise ContextReductionError("empty executable mutation")
        return reduce_intent(state, mutation)
